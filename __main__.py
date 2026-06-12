"""
__main__.py - CLI 入口

用法：
  qwen-coder                         # 交互 REPL
  qwen-coder "帮我写一个快速排序"     # 一次性执行
  qwen-coder --plan                  # 只读计划模式
  qwen-coder --yolo                  # 跳过所有确认
  qwen-coder --resume                # 恢复上次会话
  qwen-coder --resume <session_id>   # 恢复指定会话

环境变量：
  ANTHROPIC_API_KEY     → 使用 Claude（优先）
  OPENAI_API_KEY        → 使用 OpenAI 兼容接口
  OPENAI_BASE_URL       → 自定义 base_url（配合 OPENAI_API_KEY）
  MODEL_NAME            → 覆盖默认模型名
  MODEL_CONTEXT_WINDOW  → 覆盖上下文窗口大小（token 数，默认 32768）
"""

import argparse
import asyncio
import os
import sys

from rich.console import Console
from rich.table import Table

console = Console()


# ── 参数解析 ──────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="qwen-coder",
        description="AI 编程助手（LangGraph + Qwen/Claude）",
    )

    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="一次性执行的 prompt（省略则进入交互 REPL）",
    )

    # 权限模式（互斥）
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--plan", action="store_true",
        help="只读计划模式，禁止写文件和执行命令",
    )
    mode_group.add_argument(
        "--yolo", action="store_true",
        help="跳过所有确认（bypassPermissions）",
    )
    mode_group.add_argument(
        "--accept-edits", action="store_true",
        help="自动批准文件编辑，执行命令仍需确认",
    )
    mode_group.add_argument(
        "--dont-ask", action="store_true",
        help="自动拒绝所有需确认操作（CI 场景）",
    )

    # 会话管理
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--resume", nargs="?", const="__latest__", metavar="SESSION_ID",
        help="恢复会话（省略 SESSION_ID 则恢复最近一次）",
    )
    resume_group.add_argument(
        "--list-sessions", action="store_true",
        help="列出历史会话",
    )

    # 模型
    parser.add_argument(
        "--model", "-m",
        default=None,
        help="模型名称（默认从环境变量 MODEL_NAME 读取）",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="OpenAI 兼容接口的 base URL（覆盖 OPENAI_BASE_URL）",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API Key（覆盖环境变量）",
    )

    # 限制
    parser.add_argument(
        "--max-turns", type=int, default=100,
        help="最大 Agent 轮次（默认 100）",
    )

    return parser.parse_args()


# ── 权限模式解析 ──────────────────────────────────────────────────────────────

def _resolve_permission_mode(args: argparse.Namespace) -> str:
    if args.plan:
        return "plan"
    if args.yolo:
        return "bypassPermissions"
    if args.accept_edits:
        return "acceptEdits"
    if args.dont_ask:
        return "dontAsk"
    return "default"


# ── 模型初始化 ────────────────────────────────────────────────────────────────

def _build_model(args: argparse.Namespace):
    """
    优先级：
      1. ANTHROPIC_API_KEY → ChatAnthropic
      2. OPENAI_API_KEY 或 --base-url → ChatOpenAI
    model 名称：--model > MODEL_NAME 环境变量 > 各后端默认值
    """
    model_name = args.model or os.environ.get("MODEL_NAME")
    api_key    = args.api_key
    base_url   = args.base_url or os.environ.get("OPENAI_BASE_URL")

    anthropic_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    openai_key    = api_key or os.environ.get("OPENAI_API_KEY")

    if anthropic_key and not base_url:
        try:
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(
                model=model_name or "claude-sonnet-4-6",
                api_key=anthropic_key,
                streaming=True,
            )
        except ImportError:
            console.print("[yellow]langchain-anthropic 未安装，尝试 OpenAI 兼容接口[/yellow]")

    if openai_key or base_url:
        try:
            from langchain_openai import ChatOpenAI
            kwargs: dict = {
                "model":    model_name or "qwen2.5-coder-32b-instruct",
                "streaming": True,
            }
            if openai_key:
                kwargs["api_key"] = openai_key
            if base_url:
                kwargs["base_url"] = base_url
            return ChatOpenAI(**kwargs)
        except ImportError:
            console.print("[red]langchain-openai 未安装，请运行：pip install langchain-openai[/red]")
            sys.exit(1)

    console.print(
        "[red]未找到 API Key。请设置 ANTHROPIC_API_KEY 或 OPENAI_API_KEY。[/red]"
    )
    sys.exit(1)


# ── 会话列表展示 ──────────────────────────────────────────────────────────────

def _print_sessions() -> None:
    from features.session import list_sessions
    sessions = list_sessions()
    if not sessions:
        console.print("[dim]暂无历史会话[/dim]")
        return

    table = Table(title="历史会话", show_lines=False)
    table.add_column("ID",           style="cyan",  no_wrap=True)
    table.add_column("开始时间",     style="dim")
    table.add_column("最后更新",     style="dim")
    table.add_column("标题",         style="white")

    for s in sessions[:20]:
        table.add_row(
            s.get("id", "")[:30],
            (s.get("startTime", "")[:16]).replace("T", " "),
            (s.get("last_updated", "")[:16]).replace("T", " "),
            s.get("title") or "[dim]（无标题）[/dim]",
        )

    console.print(table)


# ── 会话 ID 解析 ──────────────────────────────────────────────────────────────

def _resolve_session_id(resume_arg: str | None) -> str:
    from features.session import get_latest_session_id, new_session_id

    if resume_arg is None:
        sid = new_session_id()
        console.print(f"[dim]新会话：{sid}[/dim]")
        return sid

    if resume_arg == "__latest__":
        sid = get_latest_session_id()
        if sid is None:
            console.print("[yellow]没有历史会话，创建新会话[/yellow]")
            return new_session_id()
        console.print(f"[dim]恢复会话：{sid}[/dim]")
        return sid

    # 指定了具体 session_id
    from features.session import load_session
    if load_session(resume_arg):
        console.print(f"[dim]恢复会话：{resume_arg}[/dim]")
        return resume_arg

    console.print(f"[yellow]会话 {resume_arg} 不存在，创建新会话[/yellow]")
    return new_session_id()


# ── 生成会话标题（首轮结束后异步执行）────────────────────────────────────────

async def _save_title(session_id: str, model, graph, config: dict) -> None:
    """取第一条 Human 消息生成会话标题，写入 session.json。"""
    try:
        state = (await graph.aget_state(config)).values
        messages = state.get("messages", [])
        from langchain_core.messages import HumanMessage as HM
        first_human = next(
            (m.content for m in messages if isinstance(m, HM)
             and isinstance(m.content, str)),
            None,
        )
        if not first_human:
            return
        from graph.prompt import generate_session_title
        from features.session import update_title
        title = await generate_session_title(first_human, model)
        update_title(session_id, title)
    except Exception:
        pass


# ── 主入口 ────────────────────────────────────────────────────────────────────

async def _main() -> None:
    args = _parse_args()

    # --list-sessions
    if args.list_sessions:
        _print_sessions()
        return

    permission_mode = _resolve_permission_mode(args)
    model           = _build_model(args)

    session_id      = _resolve_session_id(args.resume)

    from graph.agent import build_graph, make_initial_state
    from features.session import get_checkpointer, make_thread_config, restore_session, save_session
    from interfaces.ui import run_once, run_repl

    config      = make_thread_config(session_id)
    checkpointer = get_checkpointer()   # MemorySaver，无需 async with
    builder     = build_graph(model, max_turns=args.max_turns)
    app         = builder.compile(checkpointer=checkpointer)

    if args.resume is not None:
        # 恢复旧会话：从 JSONL 注入消息历史
        await restore_session(app, config, session_id)
    else:
        # 新会话：写入初始 state
        await app.aupdate_state(config, make_initial_state(permission_mode))

    save_session(session_id)

    try:
        if args.prompt:
            await run_once(app, config, model, args.prompt, session_id)
        else:
            await run_repl(app, config, model, session_id)
    finally:
        await _save_title(session_id, model, app, config)
        from features.memory import increment_dream_session, trigger_dream
        increment_dream_session()
        await trigger_dream(model)


def main() -> None:
    """setuptools entry_point 调用此函数。"""
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
