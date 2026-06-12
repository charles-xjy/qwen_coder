import os
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.types import interrupt

from core.state import AgentState

_CONTEXT_WINDOW = int(os.getenv("MODEL_CONTEXT_WINDOW", "32768"))
_SAFETY_MARGIN = 2000
_EFFECTIVE_WINDOW = _CONTEXT_WINDOW - _SAFETY_MARGIN

_SNIP_THRESHOLD = 0.60
_WARN_THRESHOLD = 0.75
_COMPRESS_THRESHOLD = 0.85

_QWEN_CACHE_TTL_S = 300   # Qwen 计费缓存 TTL = 5 分钟；超过即视为缓存已失效
_MC_KEEP_RECENT = 5       # mic 保留最近 N 条可压缩工具结果
_SNIP_KEEP_RECENT = 6     # llm snip 永不压缩的最近 N 条消息
_SNIP_CHECK_INTERVAL = 20 # 每积累 N 条 non-system 消息触发一次 snip 检查

_SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell", "web_fetch"}


def _usage_ratio(state: AgentState) -> float:
    tokens = state.get("last_input_token_count", 0)
    if not tokens:
        return 0.0
    return tokens / _EFFECTIVE_WINDOW


def _build_tool_call_map(messages: list) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", []) or []:
                mapping[tc["id"]] = tc["name"]
    return mapping


# ── Time-based Micro-compact ──────────────────────────────────────────────────

def maybe_time_based_mic(messages: list, last_api_time: float) -> list | None:
    """
    如果距上次 API 调用超过 _QWEN_CACHE_TTL_S（5分钟），说明 Qwen 计费缓存已失效，
    清空旧工具结果内容（保留最近 _MC_KEEP_RECENT 条），减少重写量。

    返回修改后的消息列表，或 None（未触发）。
    仅修改本地副本，不写入 state。
    """
    idle = time.time() - last_api_time
    if idle <= _QWEN_CACHE_TTL_S or last_api_time == 0:
        return None

    tool_call_map = _build_tool_call_map(messages)
    # 收集所有可压缩工具结果的 tool_use_id（按出现顺序）
    compactable_ids: list[str] = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", []) or []:
                if tc["name"] in _SNIPPABLE_TOOLS:
                    compactable_ids.append(tc["id"])

    if not compactable_ids:
        return None

    keep_set = set(compactable_ids[-_MC_KEEP_RECENT:])
    clear_set = set(compactable_ids) - keep_set

    if not clear_set:
        return None

    result = []
    for msg in messages:
        if isinstance(msg, ToolMessage):
            tool_name = tool_call_map.get(msg.tool_call_id, "")
            if msg.tool_call_id in clear_set and tool_name in _SNIPPABLE_TOOLS:
                result.append(msg.model_copy(update={"content": "[缓存已失效，旧结果已清理]"}))
                continue
        result.append(msg)

    return result


# ── LLM-driven Snip ───────────────────────────────────────────────────────────

_SNIP_PROMPT = """\
以下是一段AI编程助手的对话历史（每条消息前有序号）。

请分析是否存在冗余或可折叠的部分。冗余的判断标准：
- 文件读取/搜索结果：后续已基于其做出决策，原始结果不再需要
- 探索性操作：执行了但对后续无影响（如发现文件不存在、方向被放弃）
- 重复讨论：多轮来回但结论只有一个，中间过程可折叠
- 已完成的子任务：过程细节不再需要，只需保留结论

如果不需要压缩，输出：
<snip_decision>{"needed": false}</snip_decision>

如果需要压缩，输出要压缩的序号范围和摘要：
<snip_decision>{"needed": true, "start": <序号>, "end": <序号>}</snip_decision>
<summary>
[对这段历史的详细摘要，保留继续工作所必需的关键信息：用户请求、关键决策、文件变更、错误及修复]
</summary>

规则：
- start/end 是下方消息列表里的序号（从 0 开始）
- 被压缩的范围内必须有足够的冗余；不要压缩最近的消息
- 只输出上述标签块，不要其他内容

对话历史：
"""


def _format_history_for_snip(messages: list) -> str:
    tool_call_map = _build_tool_call_map(messages)
    lines: list[str] = []
    for idx, msg in enumerate(messages):
        if isinstance(msg, SystemMessage):
            continue
        if isinstance(msg, HumanMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            lines.append(f"[{idx}][用户]: {content[:600]}")
        elif isinstance(msg, AIMessage):
            content = msg.content if isinstance(msg.content, str) else ""
            tcs = getattr(msg, "tool_calls", []) or []
            if tcs:
                tools_str = ", ".join(tc["name"] for tc in tcs)
                content = f"{content}\n[调用工具: {tools_str}]".strip()
            if content:
                lines.append(f"[{idx}][助手]: {content[:500]}")
        elif isinstance(msg, ToolMessage):
            tool_name = tool_call_map.get(msg.tool_call_id, "tool")
            raw = msg.content if isinstance(msg.content, str) else str(msg.content)
            lines.append(f"[{idx}][{tool_name}结果]: {raw[:400]}")
    return "\n\n".join(lines)


def _parse_snip_response(text: str) -> tuple[bool, int, int, str]:
    """
    解析 LLM 输出，返回 (needed, start, end, summary)。
    """
    import json
    import re

    decision_match = re.search(r"<snip_decision>([\s\S]*?)</snip_decision>", text)
    if not decision_match:
        return False, 0, 0, ""

    try:
        decision = json.loads(decision_match.group(1).strip())
    except Exception:
        return False, 0, 0, ""

    if not decision.get("needed", False):
        return False, 0, 0, ""

    start = int(decision.get("start", -1))
    end = int(decision.get("end", -1))
    if start < 0 or end < 0 or end < start:
        return False, 0, 0, ""

    summary_match = re.search(r"<summary>([\s\S]*?)</summary>", text)
    summary = summary_match.group(1).strip() if summary_match else ""
    if not summary:
        return False, 0, 0, ""

    return True, start, end, summary


async def llm_snip_messages(
    messages: list,
    model: Any,
) -> tuple[list, bool]:
    """
    LLM 驱动的局部 snip：LLM 自行判断哪段历史冗余，只压缩那段，其余消息原封不动。

    例如 30 条消息中 LLM 认为第 5-13 条冗余，则把这 9 条替换为 1 条摘要消息，
    其余 21 条保持不变。

    触发条件：non-system 消息数 > _SNIP_CHECK_INTERVAL（由调用方保证）。
    返回 (完整新消息列表, did_snip)。
    """
    non_system = [m for m in messages if not isinstance(m, SystemMessage)]
    if len(non_system) <= _SNIP_CHECK_INTERVAL:
        return messages, False

    history_text = _format_history_for_snip(non_system)
    if not history_text:
        return messages, False

    try:
        response = await model.ainvoke([HumanMessage(content=_SNIP_PROMPT + history_text)])
        raw = response.content.strip() if isinstance(response.content, str) else ""
        needed, start, end, summary = _parse_snip_response(raw)

        if not needed:
            return messages, False

        # 边界校验：不允许压缩最后 _SNIP_KEEP_RECENT 条
        end = min(end, len(non_system) - _SNIP_KEEP_RECENT - 1)
        if end < start:
            return messages, False

        summary_msg = HumanMessage(
            content=(
                f"[对话历史摘要（原第 {start}–{end} 条消息已折叠）]\n\n"
                f"{summary}"
            )
        )

        # 重建消息列表：保留 system 消息，替换 non_system[start:end+1] 为摘要
        new_non_system: list = non_system[:start] + [summary_msg] + non_system[end + 1:]
        result: list = [m for m in messages if isinstance(m, SystemMessage)] + new_non_system
        return result, True

    except Exception:
        return messages, False


# ── LLM 全量摘要（用于 85%+ 用户确认的 compress 节点）─────────────────────────

_FULL_COMPACT_PROMPT = """\
你的任务是为以下完整对话历史创建一份详细摘要，供后续对话在不丢失上下文的情况下继续工作。
摘要需要准确捕捉所有技术细节、代码变更和架构决策。

在输出最终摘要之前，先用 <analysis> 标签包裹你的分析过程：
1. 按时间顺序分析每条消息，识别用户请求、关键决策、文件变更、错误及修复
2. 确认所有技术细节完整

摘要必须包含以下部分（输出在 <summary> 标签内）：
1. 主要请求与意图：用户的所有明确需求和意图
2. 关键技术要点：涉及的技术、框架、方案
3. 文件与代码：检查/修改/新增的文件，含关键代码片段和函数签名
4. 错误与修复：遇到的问题及解决方式，包括用户反馈
5. 问题解决过程：已解决的问题和进行中的调试
6. 所有用户消息：列出全部用户消息（非工具结果）
7. 待续事项：明确被要求做但尚未完成的任务
8. 当前工作：摘要请求前正在进行的具体工作（含文件名和代码片段）
9. 下一步（可选）：与最近工作直接相关的下一步，需包含对话原文引用

只输出 <analysis> 和 <summary> 两个块，不要其他内容。

完整对话历史：
"""


async def _llm_summarize(messages: list, model: Any) -> list:
    """
    全量摘要，用于 85%+ 用户确认场景。
    对齐 cc-haha compactConversation：所有消息 → 1 条摘要 message，无 messagesToKeep。
    """
    non_system = [m for m in messages if not isinstance(m, SystemMessage)]
    if len(non_system) < 4:
        return messages

    history_text = _format_history_for_snip(non_system)
    if not history_text:
        return messages

    try:
        response = await model.ainvoke([HumanMessage(content=_FULL_COMPACT_PROMPT + history_text)])
        raw = response.content.strip() if isinstance(response.content, str) else ""
        summary = _extract_summary(raw)
        if not summary:
            return messages
    except Exception:
        return messages

    summary_msg = HumanMessage(
        content=(
            "以下是本次会话的完整对话摘要，原始消息已压缩以释放上下文空间。\n\n"
            f"{summary}\n\n"
            "请从上述摘要描述的状态继续工作，不要重复已完成的内容，也不要询问已知信息。"
        )
    )
    removes = [RemoveMessage(id=m.id) for m in non_system if getattr(m, "id", None)]
    return removes + [summary_msg]


# ── 路由 & 节点工厂 ────────────────────────────────────────────────────────────

def make_token_router():
    def router(state: AgentState) -> str:
        ratio = _usage_ratio(state)
        if ratio >= _WARN_THRESHOLD:
            return "warn"
        return "agent"
    return router


def create_warn_node():
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
    return "compress" if state.get("compress_choice") == "compress" else "agent"


def create_compress_node(model: Any):
    async def compress_node(state: AgentState) -> dict:
        messages = state["messages"]
        ratio = _usage_ratio(state)
        choice = state.get("compress_choice", "")

        if choice == "compress" and ratio >= _COMPRESS_THRESHOLD:
            new_messages = await _llm_summarize(messages, model)
            print(f"\033[32m[压缩] LLM 摘要完成，消息从 {len(messages)} 条压缩为 {len(new_messages)} 条\033[0m")
        else:
            new_messages = list(messages)

        return {
            "messages": new_messages,
            "compress_choice": "",
        }

    return compress_node
