import os
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.types import interrupt

from state import AgentState

_CONTEXT_WINDOW = int(os.getenv("MODEL_CONTEXT_WINDOW", "32768"))
_SAFETY_MARGIN = 2000
_EFFECTIVE_WINDOW = _CONTEXT_WINDOW - _SAFETY_MARGIN

_SNIP_THRESHOLD = 0.60
_WARN_THRESHOLD = 0.75
_COMPRESS_THRESHOLD = 0.85

_MICROCOMPACT_IDLE_S = 300
_KEEP_RECENT = 3

_SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell", "web_fetch"}

_SNIP_PLACEHOLDER = "[内容已压缩 — 如需查看请重新调用工具]"
_MICROCOMPACT_PLACEHOLDER = "[旧结果已清理]"


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


def _has_snippable_messages(messages: list) -> bool:
    tool_call_map = _build_tool_call_map(messages)
    tool_msg_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    if len(tool_msg_indices) <= _KEEP_RECENT:
        return False

    for i in tool_msg_indices[: -_KEEP_RECENT]:
        msg = messages[i]
        tool_name = tool_call_map.get(msg.tool_call_id, "")
        if tool_name in _SNIPPABLE_TOOLS:
            return True
    return False


def _snip_messages(messages: list) -> list:
    """
    Replace old snippable ToolMessage contents with a placeholder.
    """
    tool_call_map = _build_tool_call_map(messages)
    tool_msg_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    to_snip = set(tool_msg_indices[: -_KEEP_RECENT]) if len(tool_msg_indices) > _KEEP_RECENT else set()

    new_messages = []
    for i, msg in enumerate(messages):
        if i in to_snip and isinstance(msg, ToolMessage):
            tool_name = tool_call_map.get(msg.tool_call_id, "")
            if tool_name in _SNIPPABLE_TOOLS:
                new_messages.append(msg.model_copy(update={"content": _SNIP_PLACEHOLDER}))
                continue
        new_messages.append(msg)

    return new_messages


def _micro_compact_messages(messages: list) -> list:
    """
    More aggressive cleanup than snipping: clear all old ToolMessage contents.
    """
    tool_msg_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    to_clear = set(tool_msg_indices[: -_KEEP_RECENT]) if len(tool_msg_indices) > _KEEP_RECENT else set()

    return [
        m.model_copy(update={"content": _MICROCOMPACT_PLACEHOLDER})
        if (i in to_clear and isinstance(m, ToolMessage))
        else m
        for i, m in enumerate(messages)
    ]


async def _llm_summarize(messages: list, model: Any) -> list:
    """
    Summarize early history into a structured compact form.
    """
    if len(messages) <= 6:
        return messages

    keep_count = 4
    to_compress = messages[:-keep_count]
    to_keep = messages[-keep_count:]

    history_text = []
    for msg in to_compress:
        if isinstance(msg, SystemMessage):
            continue
        role = "用户" if isinstance(msg, HumanMessage) else "助手" if isinstance(msg, AIMessage) else "工具结果"
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        history_text.append(f"[{role}]: {content[:500]}")

    summary_prompt = (
        "请将以下对话历史压缩为结构化摘要，保留继续工作所需的关键信息：\n\n"
        + "\n".join(history_text)
        + "\n\n请输出以下结构：\n"
        "1. 任务背景\n"
        "2. 已完成工作\n"
        "3. 关键决策\n"
        "4. 当前状态\n"
        "5. 待续事项\n"
        "输出要简洁，不要超过 600 字。\n"
    )

    try:
        response = await model.ainvoke([HumanMessage(content=summary_prompt)])
        summary_text = response.content if isinstance(response.content, str) else str(response.content)
    except Exception:
        return _snip_messages(messages)

    summary_msg = HumanMessage(
        content=f"[对话历史摘要]\n\n{summary_text}\n\n[以上为压缩摘要，原始对话已截断]"
    )
    removes = [RemoveMessage(id=m.id) for m in to_compress if not isinstance(m, SystemMessage)]
    return removes + [summary_msg] + list(to_keep)


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
        idle = time.time() - (state.get("last_api_call_time") or time.time())

        if choice == "compress" and ratio >= _COMPRESS_THRESHOLD:
            new_messages = await _llm_summarize(messages, model)
            print(f"\033[32m[压缩] LLM 摘要完成，消息从 {len(messages)} 条压缩为 {len(new_messages)} 条\033[0m")
        elif idle > _MICROCOMPACT_IDLE_S:
            new_messages = _micro_compact_messages(messages)
            print(f"\033[33m[压缩] Micro-compact：清理了旧 ToolMessage（空闲 {idle/60:.1f} 分钟）\033[0m")
        else:
            new_messages = list(messages)

        return {
            "messages": new_messages,
            "compress_choice": "",
        }

    return compress_node
