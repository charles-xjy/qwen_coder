"""
prompt.py - 系统 Prompt 构建

每轮 Agent 调用前重新生成 system prompt，注入：
  1. 核心身份与行为规则
  2. 运行时环境（OS、shell、cwd、日期）
  3. 权限模式说明（随 state.permission_mode 动态变化）
  4. 相关记忆（sideQuery 异步检索，按用户消息选取）
  5. Skills 目录（可用 /command 列表）

主入口：
  build_system_prompt(state, model, user_message) -> str   （async）
  build_system_prompt_sync(state) -> str                   （无记忆注入，用于非 async 场景）
"""

import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from skills import build_skill_catalog
from memory import get_memories_for_prompt, load_memory_index
from state import AgentState

# ── 核心身份 ──────────────────────────────────────────────────────────────────

_IDENTITY = """\
你是一个强大的 AI 编程助手，运行在命令行环境中。你能够：
- 读写文件、搜索代码库、执行 shell 命令
- 分析问题、制定计划、独立完成复杂编程任务
- 在沙箱中安全运行和测试代码
- 启动子 Agent 并行处理独立子任务

你的工作风格：
- 直接行动，不在确认已知信息上浪费时间
- 修改文件前先用 read_file 读取，确保看到最新内容
- 优先并行调用工具以提高效率
- 简洁输出，不重复已做的事，不解释显而易见的内容\
"""

# ── 权限模式说明 ──────────────────────────────────────────────────────────────

_PERMISSION_DESCRIPTIONS: dict[str, str] = {
    "default": (
        "【权限模式：默认】\n"
        "- 读取操作（read_file / list_files / grep_search）：无需确认\n"
        "- 写入/修改文件：无需确认\n"
        "- 危险 shell 命令（rm -rf / sudo 等）：需要用户确认\n"
        "- 网络操作、进程管理：需要用户确认"
    ),
    "plan": (
        "【权限模式：计划模式】\n"
        "- 当前处于只读模式，禁止任何写入和修改操作\n"
        "- 可以：read_file / list_files / grep_search / web_search\n"
        "- 禁止：write_file / edit_file / run_shell / sandbox_exec\n"
        "- 你的职责是分析现状、制定详细的实施方案，等待用户确认后再执行\n"
        "- 完成分析后调用 exit_plan_mode 工具退出此模式"
    ),
    "acceptEdits": (
        "【权限模式：自动接受编辑】\n"
        "- 文件读写操作：自动执行，无需确认\n"
        "- 危险 shell 命令：仍需用户确认\n"
        "- 适合批量编辑场景"
    ),
    "bypassPermissions": (
        "【权限模式：绕过权限】\n"
        "- 所有操作自动执行，无需任何确认\n"
        "- 谨慎使用，不可逆操作会直接执行"
    ),
    "dontAsk": (
        "【权限模式：不询问】\n"
        "- 读写操作自动执行\n"
        "- 危险命令会被阻止（不询问，直接拒绝）"
    ),
}


def _get_permission_section(mode: str) -> str:
    return _PERMISSION_DESCRIPTIONS.get(mode, _PERMISSION_DESCRIPTIONS["default"])


# ── 环境信息 ──────────────────────────────────────────────────────────────────

def _get_env_section() -> str:
    system = platform.system()
    if system == "Windows":
        os_name = f"Windows {platform.release()}"
        shell = os.environ.get("COMSPEC", "cmd.exe")
    elif system == "Darwin":
        os_name = f"macOS {platform.mac_ver()[0]}"
        shell = os.environ.get("SHELL", "/bin/zsh")
    else:
        os_name = f"Linux ({platform.version()[:40]})"
        shell = os.environ.get("SHELL", "/bin/bash")

    cwd = os.getcwd()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    python_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    return (
        f"【运行环境】\n"
        f"- 操作系统：{os_name}\n"
        f"- Shell：{shell}\n"
        f"- 工作目录：{cwd}\n"
        f"- 当前时间：{now}\n"
        f"- Python：{python_ver}"
    )


# ── Git 上下文 ───────────────────────────────────────────────────────────────

def _get_git_context() -> str:
    """获取当前 git 状态：分支、最近 3 条 commit、工作区变更摘要。"""
    try:
        def _run(cmd: list[str]) -> str:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            return r.stdout.strip() if r.returncode == 0 else ""

        branch  = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        if not branch:
            return ""

        log     = _run(["git", "log", "--oneline", "-3"])
        status  = _run(["git", "status", "--short"])

        lines = [f"【Git】分支：{branch}"]
        if log:
            lines.append("最近提交：\n" + "\n".join(f"  {l}" for l in log.splitlines()))
        if status:
            lines.append("工作区变更：\n" + "\n".join(f"  {l}" for l in status.splitlines()[:20]))
        return "\n".join(lines)
    except Exception:
        return ""


# ── CLAUDE.md ────────────────────────────────────────────────────────────────

def _load_claude_md() -> str:
    """按优先级加载 CLAUDE.md：项目根 > 父目录（最多向上 3 层）> ~/.claude/CLAUDE.md。"""
    candidates: list[Path] = []

    cwd = Path.cwd()
    for p in [cwd, *cwd.parents[:3]]:
        candidates.append(p / "CLAUDE.md")

    candidates.append(Path.home() / ".claude" / "CLAUDE.md")

    for path in candidates:
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    return f"【项目说明 ({path})】\n{content[:8000]}"
            except Exception:
                continue
    return ""


# ── 工具使用规则 ──────────────────────────────────────────────────────────────

_TOOL_RULES = """\
【工具使用规则】
- 编辑文件前必须先调用 read_file 读取最新内容
- 对同一目标可并行调用多个读取工具（list_files + grep_search + read_file）
- run_shell 输出超长时会自动截断，如需完整输出请重定向到文件
- sandbox_exec：代码执行在隔离沙箱中，不影响本机环境
- web_search / web_fetch：仅在用户明确需要网络信息时使用
- agent 工具：将独立子任务委托给子 Agent，避免污染当前上下文\
"""

# ── Plan 模式补充说明 ─────────────────────────────────────────────────────────

_PLAN_MODE_EXTRA = """\
【当前任务】
在退出计划模式前，请输出完整的实施方案，包含：
1. 现状分析（相关文件、当前结构）
2. 分步实施计划（每步操作、涉及文件）
3. 潜在风险与注意事项
方案确认后，调用 exit_plan_mode 工具，用户将审核并决定是否继续执行。\
"""


# ── 主构建函数（async，含 sideQuery 记忆注入）────────────────────────────────

async def build_system_prompt(
    state: AgentState,
    model: Any,
    user_message: str = "",
) -> tuple[str, set, int]:
    """
    构建完整 system prompt。
    返回 (prompt_str, newly_surfaced_filenames, bytes_added)。
    """
    sections: list[str] = []

    sections.append(_IDENTITY)
    sections.append(_get_env_section())

    git_ctx = _get_git_context()
    if git_ctx:
        sections.append(git_ctx)

    claude_md = _load_claude_md()
    if claude_md:
        sections.append(claude_md)

    mode = state.get("permission_mode", "default")
    sections.append(_get_permission_section(mode))
    if mode == "plan":
        sections.append(_PLAN_MODE_EXTRA)

    sections.append(_TOOL_RULES)

    # sideQuery：让小模型从索引中选出相关记忆，注入完整内容
    already_surfaced: set[str] = state.get("surfaced_memories", set()) or set()
    session_bytes: int = state.get("session_memory_bytes", 0) or 0

    if user_message.strip():
        mem_section, newly_surfaced, bytes_added = await get_memories_for_prompt(
            query=user_message,
            model=model,
            already_surfaced=already_surfaced,
            session_bytes_used=session_bytes,
        )
        if mem_section:
            sections.append(mem_section)
    else:
        newly_surfaced = set()
        bytes_added = 0

    skill_catalog = build_skill_catalog()
    if skill_catalog:
        sections.append(skill_catalog)

    return "\n\n".join(sections), newly_surfaced, bytes_added


def build_system_prompt_sync(state: AgentState) -> str:
    """
    不含记忆注入的同步版本，用于子 Agent 或无法 await 的场景。
    """
    sections: list[str] = []

    sections.append(_IDENTITY)
    sections.append(_get_env_section())

    git_ctx = _get_git_context()
    if git_ctx:
        sections.append(git_ctx)

    claude_md = _load_claude_md()
    if claude_md:
        sections.append(claude_md)

    mode = state.get("permission_mode", "default")
    sections.append(_get_permission_section(mode))
    if mode == "plan":
        sections.append(_PLAN_MODE_EXTRA)

    sections.append(_TOOL_RULES)

    skill_catalog = build_skill_catalog()
    if skill_catalog:
        sections.append(skill_catalog)

    return "\n\n".join(sections)


# ── 会话标题生成 ──────────────────────────────────────────────────────────────

async def generate_session_title(first_user_message: str, model: Any) -> str:
    """
    根据第一条用户消息，用 LLM 生成简短的会话标题（≤20 字）。
    用于 session.py 的元数据存储，方便后续 --resume 时识别。
    生成失败时返回截断的原始消息。
    """
    if not first_user_message.strip():
        return "新会话"

    prompt = (
        f"请为以下用户消息生成一个简短的会话标题（不超过20个字，直接输出标题，不加引号）：\n\n"
        f"{first_user_message[:300]}"
    )
    try:
        from langchain_core.messages import HumanMessage
        response = await model.ainvoke([HumanMessage(content=prompt)])
        title = response.content.strip()
        return title[:40] if title else first_user_message[:40]
    except Exception:
        return first_user_message[:40]
