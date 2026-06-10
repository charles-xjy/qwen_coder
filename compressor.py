"""
compressor.py - 4 级上下文压缩

级别与触发条件：
  1. 预算截断    tool result > 15KB 时截断（在 tools.py execute_tool 中处理，此处不重复）
  2. Snipping    使用率 > 60%：把旧的可截断 ToolMessage 替换为占位符，保留最近 3 条
  3. Micro-compact  空闲 > 5 分钟：清空所有旧 ToolMessage
  4. LLM 摘要   使用率 > 85% 且用户确认：调用 LLM 压缩早期对话为结构化摘要

LangGraph 节点：
  make_token_router()   → 路由函数，决定 tools 节点后去 warn 还是 agent
  create_warn_node()    → interrupt() 询问用户是否立即压缩
  create_compress_node()→ 执行 snipping / micro-compact / LLM 摘要
  route_after_warn()    → warn 节点后的路由
"""

import os
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.types import interrupt

from state import AgentState

# ── 配置 ──────────────────────────────────────────────────────────────────────

# 模型上下文窗口大小（token），从环境变量读取，默认 32768（Qwen2.5）
_CONTEXT_WINDOW   = int(os.getenv("MODEL_CONTEXT_WINDOW", "32768"))
_SAFETY_MARGIN    = 2000   # 预留给下一轮回复的 token
_EFFECTIVE_WINDOW = _CONTEXT_WINDOW - _SAFETY_MARGIN

# 各级触发阈值
_SNIP_THRESHOLD       = 0.60   # Snipping
_WARN_THRESHOLD       = 0.75   # 弹警告
_COMPRESS_THRESHOLD   = 0.85   # LLM 摘要

_MICROCOMPACT_IDLE_S  = 300    # 5 分钟空闲触发 micro-compact
_KEEP_RECENT          = 3      # Snipping 保留最近 N 条 ToolMessage

# 可截断的工具（读操作结果，可按需重新获取）
_SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell", "web_fetch"}

_SNIP_PLACEHOLDER        = "[内容已压缩 — 如需查看请重新调用工具]"
_MICROCOMPACT_PLACEHOLDER = "[旧结果已清理]"


# ── 使用率计算 ────────────────────────────────────────────────────────────────

def _usage_ratio(state: AgentState) -> float:
    tokens = state.get("last_input_token_count", 0)
    if not tokens:
        return 0.0
    return tokens / _EFFECTIVE_WINDOW


# ── 工具名称反查 ──────────────────────────────────────────────────────────────

def _build_tool_call_map(messages: list) -> dict[str, str]:
    """
    遍历消息历史，建立 tool_call_id → tool_name 映射。
    用于判断一条 ToolMessage 是由哪个工具产生的。
    """
    mapping: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", []) or []:
                mapping[tc["id"]] = tc["name"]
    return mapping


# ── Snipping（级别 2）────────────────────────────────────────────────────────

def _snip_messages(messages: list) -> list:
    """
    找出所有 ToolMessage，保留最近 KEEP_RECENT 条不动，
    对更早的可截断工具结果替换为占位符。
    返回新的消息列表（不修改原列表）。
    """
    tool_call_map = _build_tool_call_map(messages)

    # 收集所有 ToolMessage 的索引
    tool_msg_indices = [
        i for i, m in enumerate(messages) if isinstance(m, ToolMessage)
    ]

    # 最近 KEEP_RECENT 条不动
    to_snip = set(tool_msg_indices[: -_KEEP_RECENT]) if len(tool_msg_indices) > _KEEP_RECENT else set()

    new_messages = []
    for i, msg in enumerate(messages):
        if i in to_snip and isinstance(msg, ToolMessage):
            tool_name = tool_call_map.get(msg.tool_call_id, "")
            if tool_name in _SNIPPABLE_TOOLS:
                # 保留原 id，add_messages reducer 识别到相同 id 会 update-in-place
                new_messages.append(msg.model_copy(update={"content": _SNIP_PLACEHOLDER}))
                continue
        new_messages.append(msg)

    return new_messages


# ── Micro-compact（级别 3）───────────────────────────────────────────────────

def _micro_compact_messages(messages: list) -> list:
    """
    空闲超时后调用，清空所有旧 ToolMessage（保留最近 KEEP_RECENT 条）。
    比 snipping 更激进，不区分工具类型。
    """
    tool_msg_indices = [
        i for i, m in enumerate(messages) if isinstance(m, ToolMessage)
    ]
    to_clear = set(tool_msg_indices[: -_KEEP_RECENT]) if len(tool_msg_indices) > _KEEP_RECENT else set()

    return [
        m.model_copy(update={"content": _MICROCOMPACT_PLACEHOLDER})
        if (i in to_clear and isinstance(m, ToolMessage))
        else m
        for i, m in enumerate(messages)
    ]


# ── LLM 摘要压缩（级别 4）────────────────────────────────────────────────────

async def _llm_summarize(messages: list, model: Any) -> list:
    """
    用 LLM 把早期对话段压缩成结构化摘要，返回压缩后的消息列表。
    保留最后 4 条消息（当前用户输入 + 近期上下文）不压缩。
    """
    if len(messages) <= 6:
        return messages

    # 分段：待压缩部分 + 保留部分
    keep_count  = 4
    to_compress = messages[:-keep_count]
    to_keep     = messages[-keep_count:]

    # 把待压缩消息转成纯文本，发给 LLM
    history_text = []
    for msg in to_compress:
        if isinstance(msg, SystemMessage):
            continue
        role = "用户" if isinstance(msg, HumanMessage) else \
               "助手" if isinstance(msg, AIMessage) else "工具结果"
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        history_text.append(f"[{role}]: {content[:500]}")

    summary_prompt = (
        "请将以下对话历史压缩为结构化摘要，保留继续工作所需的关键信息：\n\n"
        + "\n".join(history_text)
        + "\n\n请输出以下结构：\n"
        "1. **任务背景**：用户的核心需求和目标\n"
        "2. **已完成工作**：已实现的功能、修改的文件及原因\n"
        "3. **关键决策**：重要的技术决策和设计选择\n"
        "4. **当前状态**：任务进行到哪个阶段\n"
        "5. **待续事项**：尚未完成的任务或下一步计划\n"
        "输出要简洁，不要超过 600 字。"
    )

    try:
        response = await model.ainvoke([HumanMessage(content=summary_prompt)])
        summary_text = response.content if isinstance(response.content, str) else str(response.content)
    except Exception as e:
        # 压缩失败时回退到 snipping
        return _snip_messages(messages)

    summary_msg = HumanMessage(
        content=f"[对话历史摘要]\n\n{summary_text}\n\n[以上为压缩摘要，原始对话已截断]"
    )
    # RemoveMessage 删除旧消息，add_messages reducer 会处理
    removes = [RemoveMessage(id=m.id) for m in to_compress if not isinstance(m, SystemMessage)]
    return removes + [summary_msg] + list(to_keep)


# ── LangGraph 节点与路由 ──────────────────────────────────────────────────────

def make_token_router():
    """
    返回路由函数，在 tools 节点之后调用。
    使用率 > WARN_THRESHOLD → "warn"，否则 → "agent"
    （snipping 和 micro-compact 在 compress 节点内部触发，不需要独立路由）
    """
    def router(state: AgentState) -> str:
        ratio = _usage_ratio(state)
        if ratio >= _WARN_THRESHOLD:
            return "warn"
        return "agent"
    return router


def create_warn_node():
    """
    返回 warn 节点函数。
    interrupt() 展示使用率，询问用户选择：立即压缩 or 跳过。
    用户选择写入 state["compress_choice"]。
    """
    def warn_node(state: AgentState) -> dict:
        ratio = _usage_ratio(state)
        pct = int(ratio * 100)

        choice = interrupt(
            f"⚠️  上下文使用率已达 {pct}%（{state.get('last_input_token_count', 0):,} / {_EFFECTIVE_WINDOW:,} tokens）\n"
            f"建议立即压缩，释放空间以继续工作。"
        )
        return {"compress_choice": choice}

    return warn_node


def route_after_warn(state: AgentState) -> str:
    """warn 节点后的路由：用户选 compress → compress 节点，否则 → agent。"""
    return "compress" if state.get("compress_choice") == "compress" else "agent"


def create_compress_node(model: Any):
    """
    返回 compress 节点函数，执行实际压缩。

    逻辑：
      - compress_choice == "compress" 且使用率 > 85% → LLM 摘要
      - 空闲 > 5 分钟 → micro-compact
      - 否则 → snipping
    """
    async def compress_node(state: AgentState) -> dict:
        messages = state["messages"]
        ratio    = _usage_ratio(state)
        choice   = state.get("compress_choice", "")
        idle     = time.time() - (state.get("last_api_call_time") or time.time())

        if choice == "compress" and ratio >= _COMPRESS_THRESHOLD:
            # 级别 4：LLM 摘要
            new_messages = await _llm_summarize(messages, model)
            print(f"\033[32m[压缩] LLM 摘要完成，消息从 {len(messages)} 条压缩为 {len(new_messages)} 条\033[0m")
        elif idle > _MICROCOMPACT_IDLE_S:
            # 级别 3：micro-compact
            new_messages = _micro_compact_messages(messages)
            print(f"\033[33m[压缩] Micro-compact：清理了旧 ToolMessage（空闲 {idle/60:.1f} 分钟）\033[0m")
        else:
            # 级别 2：snipping
            new_messages = _snip_messages(messages)
            print(f"\033[33m[压缩] Snipping：替换了旧 ToolMessage 为占位符\033[0m")

        return {
            "messages":      new_messages,
            "compress_choice": "",   # 重置
        }

    return compress_node
