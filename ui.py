"""
ui.py - Rich 终端 UI

职责：
  - 流式 token 输出（LangGraph astream_events v2）
  - 工具调用展示：图标 + 参数摘要
  - edit_file diff 高亮（红行删除，绿行新增）
  - 等待时 Spinner 动画
  - REPL 循环，含内置命令：/clear /cost /compact /memory /skills /help
  - interrupt() 响应：权限确认、压缩询问

主入口：
  run_repl(graph, config, model)           — 交互模式
  run_once(graph, config, model, prompt)   — 一次性执行模式
"""

import difflib
import json
import sys
import time
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.types import Command
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from skills import list_user_invocable_skills

console = Console(highlight=False)

# ── 工具图标 ──────────────────────────────────────────────────────────────────

_TOOL_ICONS: dict[str, str] = {
    "read_file":       "📖",
    "write_file":      "✏️ ",
    "edit_file":       "🔧",
    "list_files":      "📁",
    "grep_search":     "🔍",
    "run_shell":       "⚡",
    "web_fetch":       "🌐",
    "web_search":      "🌐",
    "enter_plan_mode": "📋",
    "exit_plan_mode":  "✅",
    "agent":           "🤖",
    "skill":           "⭐",
    "tool_search":     "🔑",
}

# ── 参数摘要（单行，用于工具调用展示）────────────────────────────────────────

def _arg_summary(tool_name: str, args: dict) -> str:
    if tool_name in ("read_file", "write_file"):
        path = args.get("path", "")
        offset = args.get("offset")
        limit  = args.get("limit")
        suffix = f":{offset}-{offset+limit}" if offset else ""
        return path + suffix
    if tool_name == "edit_file":
        return args.get("path", "")
    if tool_name == "list_files":
        return args.get("path", ".") + "/" + args.get("pattern", "*")
    if tool_name == "grep_search":
        return f"{args.get('pattern', '')}  in {args.get('path', '.')}"
    if tool_name == "run_shell":
        cmd = args.get("command", "")
        return cmd[:100] + ("…" if len(cmd) > 100 else "")
    if tool_name == "web_fetch":
        return args.get("url", "")[:80]
    if tool_name == "agent":
        return f"[{args.get('agent_type', 'general')}] {args.get('description', '')[:60]}"
    if tool_name == "skill":
        return args.get("name", "")
    return json.dumps(args, ensure_ascii=False)[:80]


# ── 工具调用展示 ──────────────────────────────────────────────────────────────

def show_tool_call(tool_name: str, args: dict) -> None:
    icon    = _TOOL_ICONS.get(tool_name, "🔧")
    summary = _arg_summary(tool_name, args)
    console.print(f"  {icon} [bold cyan]{tool_name}[/bold cyan]  [dim]{escape(summary)}[/dim]")


# ── edit_file diff 展示 ───────────────────────────────────────────────────────

def show_diff(path: str, old_string: str, new_string: str) -> None:
    old_lines = old_string.splitlines(keepends=True)
    new_lines = new_string.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{path}", tofile=f"b/{path}",
        n=3,
    ))
    if not diff:
        return

    text = Text()
    for line in diff:
        line_r = line.rstrip("\n")
        if line_r.startswith("+++") or line_r.startswith("---"):
            text.append(line_r + "\n", style="bold")
        elif line_r.startswith("+"):
            text.append(line_r + "\n", style="green")
        elif line_r.startswith("-"):
            text.append(line_r + "\n", style="red")
        elif line_r.startswith("@@"):
            text.append(line_r + "\n", style="cyan")
        else:
            text.append(line_r + "\n", style="dim")

    console.print(Panel(text, title=f"[bold]{escape(path)}[/bold]", border_style="dim"))


# ── Token 费用展示 ────────────────────────────────────────────────────────────

def show_cost(state: dict) -> None:
    inp  = state.get("total_input_tokens",  0)
    out  = state.get("total_output_tokens", 0)
    cost = (inp * 3 + out * 15) / 1_000_000   # 粗估，单位 USD
    console.print(
        f"  [dim]输入 {inp:,} tokens  输出 {out:,} tokens  "
        f"≈ ${cost:.4f}[/dim]"
    )


# ── 内置命令处理 ──────────────────────────────────────────────────────────────

async def _cmd_clear(graph: Any, config: dict) -> None:
    """清空对话历史（保留 state 其他字段）。"""
    from langgraph.graph.message import RemoveMessage
    state = (await graph.aget_state(config)).values
    removes = [RemoveMessage(id=m.id) for m in state.get("messages", [])]
    if removes:
        await graph.update_state(config, {"messages": removes})
    console.clear()
    console.print("[dim]对话历史已清空[/dim]")


async def _cmd_cost(graph: Any, config: dict) -> None:
    state = (await graph.aget_state(config)).values
    show_cost(state)


async def _cmd_compact(graph: Any, config: dict, model: Any) -> None:
    """强制触发 LLM 摘要压缩，无需等待使用率阈值。"""
    state = (await graph.aget_state(config)).values
    messages = state.get("messages", [])
    if len(messages) <= 6:
        console.print("[dim]消息太少，无需压缩[/dim]")
        return
    with console.status("[cyan]正在压缩对话历史…[/cyan]"):
        from compressor import _llm_summarize
        new_messages = await _llm_summarize(messages, model)
    await graph.update_state(config, {"messages": new_messages, "compress_choice": ""})
    console.print(f"[green]压缩完成：{len(messages)} 条 → {len(new_messages)} 条[/green]")


async def _cmd_memory(graph: Any, config: dict) -> None:
    from memory import list_memories
    headers = list_memories()
    if not headers:
        console.print("[dim]暂无记忆[/dim]")
        return
    console.print(f"[bold]记忆列表[/bold]（共 {len(headers)} 条）")
    for h in headers:
        console.print(f"  [cyan]{h.name}[/cyan] [{h.type}]  {h.description}")


def _cmd_skills() -> None:
    skills = list_user_invocable_skills()
    if not skills:
        console.print("[dim]暂无可用 Skills[/dim]")
        return
    console.print(f"[bold]可用 Skills[/bold]（共 {len(skills)} 个）")
    for s in skills:
        console.print(f"  [cyan]/{s.name}[/cyan]  {s.description}")


def _cmd_help() -> None:
    console.print(
        "[bold]内置命令[/bold]\n"
        "  [cyan]/clear[/cyan]    清空对话历史\n"
        "  [cyan]/cost[/cyan]     显示 token 用量和费用\n"
        "  [cyan]/compact[/cyan]  立即压缩对话历史\n"
        "  [cyan]/memory[/cyan]   查看所有记忆\n"
        "  [cyan]/skills[/cyan]   查看可用 Skills\n"
        "  [cyan]/resume[/cyan]   切换到历史会话\n"
        "  [cyan]/help[/cyan]     显示此帮助\n"
        "  [cyan]/exit[/cyan]     退出"
    )


async def _cmd_resume(graph: Any, config: dict) -> "tuple[dict, str] | None":
    """
    显示历史会话列表，方向键选择后恢复到 graph。
    返回 (new_config, new_session_id)，取消返回 None。
    """
    from session import list_sessions, restore_session, make_thread_config

    sessions = list_sessions()
    if not sessions:
        console.print("[dim]暂无历史会话[/dim]")
        return None

    # 构建选项列表（最多显示 15 条）
    display = sessions[:15]
    labels: list[str] = []
    for s in display:
        updated = (s.get("last_updated", "")[:16]).replace("T", " ")
        title   = (s.get("title") or "(无标题)")[:30]
        sid     = s.get("id", "")[-12:]   # 只显示 ID 后缀
        labels.append(f"{updated}  {title:<30}  …{sid}")
    labels.append("[取消]")

    idx = _arrow_select(labels, default=0)
    if idx >= len(display):
        console.print("[dim]已取消[/dim]")
        return None

    selected     = display[idx]
    new_sid      = selected["id"]
    new_config   = make_thread_config(new_sid)

    with console.status("[cyan]恢复会话中…[/cyan]"):
        await restore_session(graph, new_config, new_sid)

    title = selected.get("title") or new_sid
    console.print(f"[green]已切换到：{title}[/green]")
    return new_config, new_sid


async def handle_builtin_command(
    inp: str,
    graph: Any,
    config: dict,
    model: Any,
) -> "bool | tuple[dict, str]":
    """
    处理 /xxx 内置命令。
    返回 True  — 已处理，继续 REPL。
    返回 False — 不是内置命令，发给 Agent。
    返回 (new_config, new_session_id) — 会话已切换。
    """
    cmd = inp.strip().lower()
    if cmd == "/clear":
        await _cmd_clear(graph, config)
        return True
    if cmd == "/cost":
        await _cmd_cost(graph, config)
        return True
    if cmd == "/compact":
        await _cmd_compact(graph, config, model)
        return True
    if cmd == "/memory":
        await _cmd_memory(graph, config)
        return True
    if cmd == "/skills":
        _cmd_skills()
        return True
    if cmd in ("/help", "/?"):
        _cmd_help()
        return True
    if cmd == "/resume":
        result = await _cmd_resume(graph, config)
        return result if result is not None else True
    return False


# ── 核心：带流式输出的单轮执行 ───────────────────────────────────────────────

async def stream_turn(
    graph: Any,
    config: dict,
    input_data: Any,        # dict（新消息）或 Command（resume）
    session_id: str = "",
) -> None:
    """
    执行一轮 Agent 调用，处理：
      - LLM token 流式打印
      - 工具调用展示（图标 + 参数）
      - edit_file diff 渲染
      - interrupt() 弹出（权限确认、压缩询问）
    """
    _pending_tool: dict = {}   # 暂存当前工具调用的参数，用于 diff 渲染
    _in_llm = False            # 是否正在打印 LLM token
    _spinner_live: Live | None = None

    def _stop_spinner():
        nonlocal _spinner_live
        if _spinner_live:
            _spinner_live.stop()
            _spinner_live = None

    def _start_spinner(msg: str = "思考中…"):
        nonlocal _spinner_live
        _stop_spinner()
        _spinner_live = Live(Spinner("dots", text=f"[dim]{msg}[/dim]"), console=console, transient=True)
        _spinner_live.start()

    _start_spinner()

    try:
        async for event in graph.astream_events(input_data, config, version="v2"):
            etype = event.get("event", "")
            name  = event.get("name", "")
            data  = event.get("data", {})

            # LLM 开始输出
            if etype == "on_chat_model_start":
                _stop_spinner()

            # LLM token 流
            elif etype == "on_chat_model_stream":
                _in_llm = True
                chunk = data.get("chunk")
                text  = getattr(chunk, "content", "") if chunk else ""
                if isinstance(text, str) and text:
                    console.print(text, end="", markup=False)

            # LLM 完成一次输出
            elif etype == "on_chat_model_end":
                if _in_llm:
                    console.print()   # 换行
                    _in_llm = False
                _start_spinner("执行工具…")

            # 工具开始
            elif etype == "on_tool_start":
                _stop_spinner()
                args = data.get("input", {}) or {}
                if not isinstance(args, dict):
                    try:
                        args = json.loads(str(args))
                    except Exception:
                        args = {}
                show_tool_call(name, args)
                # 暂存 edit_file 参数，等结束时渲染 diff
                if name == "edit_file":
                    _pending_tool = {"path": args.get("path", ""),
                                     "old":  args.get("old_string", ""),
                                     "new":  args.get("new_string", "")}

            # 工具结束
            elif etype == "on_tool_end":
                result = str(data.get("output", ""))
                # edit_file：渲染 diff
                if name == "edit_file" and _pending_tool:
                    show_diff(
                        _pending_tool["path"],
                        _pending_tool["old"],
                        _pending_tool["new"],
                    )
                    _pending_tool = {}
                # 错误时显示红色摘要
                elif result.startswith("Error:"):
                    console.print(f"  [red]{escape(result[:200])}[/red]")
                _start_spinner()

            # 节点切换（进入 agent 节点时重置 spinner 文字）
            elif etype == "on_chain_start" and name == "agent":
                _start_spinner("思考中…")

    finally:
        _stop_spinner()

    # ── 处理 interrupt()（权限确认 / 压缩询问）───────────────────────────────
    await _handle_interrupts(graph, config, session_id)


async def _handle_interrupts(graph: Any, config: dict, session_id: str = "") -> None:
    """检查图是否因 interrupt() 暂停，弹出选择前先存档，然后 resume。"""
    from session import save_turn

    while True:
        snap = await graph.aget_state(config)
        if not snap.tasks:
            break

        interrupts = []
        for task in snap.tasks:
            interrupts.extend(getattr(task, "interrupts", []))
        if not interrupts:
            break

        # ── interrupt 弹出前先存档 ────────────────────────────────────────
        # 此时已包含用户消息 + AI 的工具调用请求，崩溃也不丢
        if session_id:
            await save_turn(graph, config, session_id)

        for intr in interrupts:
            msg = intr.value if hasattr(intr, "value") else str(intr)
            console.print(f"\n[yellow]{escape(str(msg))}[/yellow]")

            msg_lower = str(msg).lower()
            if "压缩" in msg_lower or "compress" in msg_lower or "上下文" in msg_lower:
                idx = _arrow_select(["立即压缩", "跳过"])
                answer = "compress" if idx == 0 else "skip"
            else:
                idx = _arrow_select(["允许", "拒绝", "跳过（取消此轮）"])
                if idx == 0:
                    answer = "yes"
                elif idx == 1:
                    answer = "no"
                else:
                    console.print("[dim]已跳过[/dim]")
                    return

        await stream_turn(graph, config, Command(resume=answer), session_id)
        break


# ── 上下键选择 ────────────────────────────────────────────────────────────────

def _arrow_select(options: list[str], default: int = 0) -> int:
    """
    终端上下键选择菜单，返回选中项的 index。
    Windows 用 msvcrt，其他平台用 tty/termios。
    Ctrl+C 返回最后一项（视为取消）。
    """
    import sys
    current = default

    def _render(first: bool = False) -> None:
        if not first:
            # 上移 len(options) 行，清掉之前的渲染
            sys.stdout.write(f"\033[{len(options)}A")
        for i, opt in enumerate(options):
            if i == current:
                sys.stdout.write(f"\r  \033[36m> {opt}\033[0m\n")
            else:
                sys.stdout.write(f"\r    {opt}\n")
        sys.stdout.flush()

    _render(first=True)

    try:
        if sys.platform == "win32":
            import msvcrt
            while True:
                key = msvcrt.getch()
                if key == b"\xe0":           # 方向键前缀
                    key2 = msvcrt.getch()
                    if key2 == b"H":         # 上
                        current = (current - 1) % len(options)
                        _render()
                    elif key2 == b"P":       # 下
                        current = (current + 1) % len(options)
                        _render()
                elif key == b"\r":           # Enter 确认
                    sys.stdout.write("\n")
                    return current
                elif key == b"\x03":         # Ctrl+C
                    raise KeyboardInterrupt
        else:
            import tty, termios
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                while True:
                    ch = sys.stdin.read(1)
                    if ch == "\x1b":
                        sys.stdin.read(1)    # [
                        arrow = sys.stdin.read(1)
                        if arrow == "A":     # 上
                            current = (current - 1) % len(options)
                            _render()
                        elif arrow == "B":   # 下
                            current = (current + 1) % len(options)
                            _render()
                    elif ch in ("\r", "\n"):
                        sys.stdout.write("\n")
                        return current
                    elif ch == "\x03":
                        raise KeyboardInterrupt
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return len(options) - 1   # 默认选最后一项（取消）


# ── 普通文本输入 ──────────────────────────────────────────────────────────────

def _prompt_input(prompt_str: str = "") -> str:
    try:
        return input(prompt_str)
    except EOFError:
        return "/exit"
    except KeyboardInterrupt:
        raise   # 交给上层处理


# ── REPL 主循环 ───────────────────────────────────────────────────────────────

async def run_repl(graph: Any, config: dict, model: Any, session_id: str = "") -> None:
    """
    交互式 REPL。
    /exit 或 Ctrl+C 退出。每轮结束后自动保存消息到 JSONL。
    """
    from session import save_turn

    console.print(
        Panel(
            "[bold cyan]qwen-coder[/bold cyan]  AI 编程助手\n"
            "[dim]/exit 退出  /help 帮助  Ctrl+C 中断当前操作[/dim]",
            border_style="cyan",
        )
    )

    while True:
        try:
            user_input = _prompt_input("\n[你] ").strip()
        except KeyboardInterrupt:
            console.print("\n[dim]已中断（输入 /exit 退出）[/dim]")
            continue

        if not user_input:
            continue

        if user_input.lower() in ("/exit", "/quit", "exit", "quit", "q"):
            console.print("[dim]再见[/dim]")
            break

        # 内置命令
        if user_input.startswith("/"):
            result = await handle_builtin_command(user_input, graph, config, model)
            if isinstance(result, tuple):
                config, session_id = result   # 会话已切换，更新本地变量
            if result is not False:
                continue

        # 发给 Agent
        try:
            await stream_turn(
                graph,
                config,
                {"messages": [HumanMessage(content=user_input)]},
                session_id,
            )
        except KeyboardInterrupt:
            console.print("\n[yellow]已中断[/yellow]")

        # 每轮结束后存档（补充：覆盖 interrupt 前存的，包含完整结果）
        if session_id:
            await save_turn(graph, config, session_id)


# ── 一次性执行模式 ────────────────────────────────────────────────────────────

async def run_once(graph: Any, config: dict, model: Any, prompt: str, session_id: str = "") -> None:
    """
    非交互式：执行一次 prompt，输出完整结果后退出。
    用于 `qwen-coder "帮我写一个快速排序"` 这种命令行调用。
    """
    from session import save_turn
    try:
        await stream_turn(
            graph,
            config,
            {"messages": [HumanMessage(content=prompt)]},
            session_id,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/yellow]")

    if session_id:
        await save_turn(graph, config, session_id)

    # 打印 token 统计
    state = (await graph.aget_state(config)).values
    show_cost(state)
