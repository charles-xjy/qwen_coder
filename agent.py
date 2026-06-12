"""
agent.py - LangGraph StateGraph 主图

节点：
  agent    → 构建 system prompt，调用 LLM
  tools    → 权限检查，执行工具调用，处理 plan_mode 切换
  warn     → interrupt() 询问用户是否压缩上下文
  compress → 执行 snipping / micro-compact / LLM 摘要

路由：
  agent → END（无 tool_calls 或达到最大轮次）
  agent → tools（有 tool_calls）
  tools → warn（使用率 > 75%）
  tools → agent（使用率 < 75%）
  warn  → compress（用户选"compress"）
  warn  → agent（用户跳过）
  compress → agent
"""

import asyncio
import json
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from compressor import _has_snippable_messages, _snip_messages, create_compress_node, create_warn_node, make_token_router, route_after_warn
from memory import auto_save_memory
from prompt import build_system_prompt
from state import AgentState
from subagent import handle_agent_tool
from tools import check_permission, execute_tool, get_active_tool_definitions


# ── 工具 Schema → LangChain 格式 ─────────────────────────────────────────────

def _to_lc_tools(schemas: list[dict]) -> list[dict]:
    """将 tool schema 列表转为 LangChain bind_tools 接受的格式。"""
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for s in schemas
    ]


# ── 工具参数摘要（用于权限确认提示）────────────────────────────────────────

def _summarize_args(tool_name: str, args: dict) -> str:
    """把工具参数压缩成一行，用于 interrupt() 展示。"""
    if tool_name in ("read_file", "write_file", "edit_file"):
        return args.get("path", "")
    if tool_name == "run_shell":
        cmd = args.get("command", "")
        return cmd[:120] + ("..." if len(cmd) > 120 else "")
    if tool_name == "grep_search":
        return f"pattern={args.get('pattern', '')}  path={args.get('path', '.')}"
    return json.dumps(args, ensure_ascii=False)[:100]


# ── 辅助：取最后一条 Human 消息文本 ─────────────────────────────────────────

def _last_human_text(messages: list) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return msg.content if isinstance(msg.content, str) else ""
    return ""


def _recent_tool_names(messages: list, last_n_turns: int = 3) -> list[str]:
    """提取最近 N 轮 AIMessage 中调用过的工具名（去重），用于 sideQuery recentTools 过滤。"""
    seen: set[str] = set()
    turns = 0
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            turns += 1
            if turns > last_n_turns:
                break
            for tc in getattr(msg, "tool_calls", []) or []:
                name = tc.get("name", "")
                if name:
                    seen.add(name)
    return list(seen)


def _apply_history_cache_markers(messages: list) -> list:
    """
    给对话历史打显式缓存标记（cache_control: ephemeral），完全对齐Claude Code官方实现。

    缓存机制：
    - 全局总共2个缓存标记：1个在system prompt稳定段结尾（prompt.py中实现），1个在messages最后一条
    - 每个标记对应一个固定缓存槽位，覆盖从prompt开头到标记位置的全部内容，新快照自动覆盖旧槽位
    - 20是官方回溯查找窗口大小：新请求从标记位置往回找20个block，找到上一轮匹配的标记就命中缓存
    - Mycro逐轮淘汰：单标记让无用KV page立即释放，多标记反而浪费（官方注释：claude.ts L3146）

    标记策略：
    - 直接打在 messages 最后一条消息上（对齐官方 markerIndex = messages.length - 1）
    - 最后一条即为本轮用户输入，当前请求缓存它，下一轮作为前缀命中
    """
    if not messages:
        return list(messages)

    last_idx = len(messages) - 1

    def _mark(msg):
        """给消息打缓存标记，仅修改最后一个content block，避免破坏原有结构"""
        content = msg.content
        if isinstance(content, str):
            return msg.model_copy(update={"content": [
                {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
            ]})
        if isinstance(content, list) and content:
            return msg.model_copy(update={"content":
                                              content[:-1] + [{**content[-1], "cache_control": {"type": "ephemeral"}}]
                                          })
        return msg

    return [_mark(msg) if i == last_idx else msg for i, msg in enumerate(messages)]


def build_graph(model: Any, max_turns: int = 100):
    """
    构建并返回 LangGraph StateGraph（未编译）。
    调用方负责传入 checkpointer 后编译：
        builder = build_graph(model)
        app = builder.compile(checkpointer=checkpointer)
    """

    # ── agent 节点 ────────────────────────────────────────────────────────────

    async def agent_node(state: AgentState) -> dict:
        # 达到最大轮次：强制结束
        turns = state.get("current_turns", 0)
        if turns >= max_turns:
            return {
                "messages": [AIMessage(content=f"[已达最大轮次限制 {max_turns}，停止执行]")],
            }

        # 每轮重新绑定工具（支持 deferred 工具激活后热更新）
        tool_schemas = get_active_tool_definitions()
        lc_tools = _to_lc_tools(tool_schemas)
        bound_model = model.bind_tools(lc_tools) if lc_tools else model

        # 构建 system prompt（sideQuery 选出相关记忆注入）
        user_text = _last_human_text(state["messages"])
        recent_tools = _recent_tool_names(state["messages"])
        prompt, newly_surfaced, bytes_added = await build_system_prompt(
            state, model, user_text, recent_tools
        )

        # system message 不进 state，只在调用时临时拼接
        # 支持显式缓存的 provider：给对话历史打缓存标记（仅1个，对齐Claude Code实现）
        from prompt import _supports_explicit_cache
        history = list(state["messages"])
        if _has_snippable_messages(history):
            history = _snip_messages(history)
        history = _apply_history_cache_markers(history) if _supports_explicit_cache(model) else history
        messages_for_llm = [SystemMessage(content=prompt)] + history

        # 调用 LLM
        response = await bound_model.ainvoke(messages_for_llm)

        # 最终回复时（无 tool_calls）后台分析记忆，不阻塞主循环
        if not (getattr(response, "tool_calls", None) or []):
            response_text = response.content if isinstance(response.content, str) else ""
            if user_text and response_text:
                asyncio.create_task(auto_save_memory(user_text, response_text, model))

        # 提取 token 用量（LangChain usage_metadata 兼容多种后端）
        usage = getattr(response, "usage_metadata", None) or {}
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)

        return {
            "messages": [response],
            "current_turns": turns + 1,
            "last_api_call_time": time.time(),
            "last_input_token_count": input_tokens,
            "total_input_tokens": (state.get("total_input_tokens") or 0) + input_tokens,
            "total_output_tokens": (state.get("total_output_tokens") or 0) + output_tokens,
            "surfaced_memories": (state.get("surfaced_memories") or set()) | newly_surfaced,
            "session_memory_bytes": (state.get("session_memory_bytes") or 0) + bytes_added,
        }

    # ── tools 节点 ───────────────────────────────────────────────────────────

    async def tools_node(state: AgentState) -> dict:
        messages = state["messages"]
        permission_mode = state.get("permission_mode", "default")
        confirmed_paths = state.get("confirmed_paths", set()) or set()

        # 取最后一条 AIMessage 的 tool_calls
        last_ai = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )
        tool_calls = getattr(last_ai, "tool_calls", None) or [] if last_ai else []

        tool_messages: list[ToolMessage] = []
        new_confirmed: set[str] = set()
        plan_mode_change: str | None = None  # "__enter_plan_mode__" | "__exit_plan_mode__"

        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("args", {})
            if not isinstance(args, dict):
                args = {}
            tc_id = tc.get("id", "")

            # ── 权限检查 ─────────────────────────────────────────────────────
            perm = check_permission(name, args, permission_mode, confirmed_paths)

            if perm == "deny":
                result = (
                    f"Error: 权限被拒绝。当前模式（{permission_mode}）"
                    f"不允许执行 '{name}'。"
                )

            elif perm == "confirm":
                summary = _summarize_args(name, args)
                answer = interrupt(
                    f"Agent 请求执行工具：{name}\n"
                    f"参数：{summary}\n\n"
                    f"输入 yes 允许，no 拒绝"
                )
                if str(answer).strip().lower() in ("yes", "y", "allow", "approve", "1"):
                    # 记录已确认路径，下次同路径无需再确认
                    path_key = args.get("path", args.get("file_path", ""))
                    if path_key:
                        new_confirmed.add(path_key)
                    result = await _run_tool(name, args, state)
                else:
                    result = f"用户拒绝了工具调用：{name}"

            else:  # allow
                result = await _run_tool(name, args, state)

            # ── plan_mode 切换检测 ─────────────────────────────────────────
            if result in ("__enter_plan_mode__", "__exit_plan_mode__"):
                plan_mode_change = result
                result = (
                    "已进入计划模式（只读）" if result == "__enter_plan_mode__"
                    else "已退出计划模式"
                )

            tool_messages.append(ToolMessage(
                content=str(result),
                tool_call_id=tc_id,
            ))

        # ── 组装 state 更新 ──────────────────────────────────────────────────
        updates: dict = {"messages": tool_messages}

        if new_confirmed:
            updates["confirmed_paths"] = confirmed_paths | new_confirmed

        if plan_mode_change == "__enter_plan_mode__":
            updates["pre_plan_mode"] = permission_mode
            updates["permission_mode"] = "plan"
        elif plan_mode_change == "__exit_plan_mode__":
            prev = state.get("pre_plan_mode", "default")
            updates["permission_mode"] = prev
            updates["pre_plan_mode"] = ""

        return updates

    # ── 辅助：执行单个工具（agent 工具特殊处理）────────────────────────────

    async def _run_tool(name: str, args: dict, state: AgentState) -> str:
        if name == "agent":
            return await handle_agent_tool(
                agent_type=args.get("type", "general"),
                prompt=args.get("prompt", ""),
                description=args.get("description", ""),
                skills=args.get("skills") or [],
                model=model,
                parent_permission_mode=state.get("permission_mode", "default"),
            )
        return await execute_tool(name, args)

    # ── agent 节点后的路由 ────────────────────────────────────────────────────

    def route_after_agent(state: AgentState) -> str:
        messages = state.get("messages") or []
        if not messages:
            return END

        last = messages[-1]
        if not isinstance(last, AIMessage):
            return END

        tool_calls = getattr(last, "tool_calls", None) or []
        if not tool_calls:
            return END

        # 达到最大轮次也终止（agent_node 已写入终止消息，下次不会再有 tool_calls）
        if state.get("current_turns", 0) >= max_turns:
            return END

        return "tools"

    # ── 组装 StateGraph ───────────────────────────────────────────────────────

    token_router = make_token_router()
    warn_node = create_warn_node()
    compress_node = create_compress_node(model)

    builder = StateGraph(AgentState)

    builder.add_node("agent", agent_node)
    builder.add_node("tools", tools_node)
    builder.add_node("warn", warn_node)
    builder.add_node("compress", compress_node)

    builder.set_entry_point("agent")

    builder.add_conditional_edges(
        "agent",
        route_after_agent,
        {"tools": "tools", END: END},
    )
    builder.add_conditional_edges(
        "tools",
        token_router,
        {"warn": "warn", "agent": "agent"},
    )
    builder.add_conditional_edges(
        "warn",
        route_after_warn,
        {"compress": "compress", "agent": "agent"},
    )
    builder.add_edge("compress", "agent")

    return builder


# ── 初始 State ────────────────────────────────────────────────────────────────

def make_initial_state(permission_mode: str = "default") -> dict:
    """生成初始 AgentState，供第一次 ainvoke/astream 使用。"""
    return {
        "messages": [],
        "permission_mode": permission_mode,
        "pre_plan_mode": "",
        "confirmed_paths": set(),
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "last_input_token_count": 0,
        "last_api_call_time": 0.0,
        "current_turns": 0,
        "compress_choice": "",
        "surfaced_memories": set(),
        "session_memory_bytes": 0,
    }
