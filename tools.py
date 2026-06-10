"""
工具系统：10 个核心工具 + 2 个 deferred 工具
- 工具定义（JSON schema，供 LLM binding）
- 处理函数（实际执行逻辑）
- 权限检查（5 种模式）
- deferred 工具机制（按需激活）
- execute_tool() 统一调度入口
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

# ── 工作目录 ──────────────────────────────────────────────────────────────────

WORKDIR = Path.cwd()

# ── 文件 mtime 状态（跨工具调用，用于 read-before-edit 校验和新鲜度检查）────

_read_file_state: dict[str, float] = {}  # path → mtime

# ── Deferred 工具激活集合 ─────────────────────────────────────────────────────

_activated_tools: set[str] = set()

# ── 危险命令正则（run_shell 权限检查用）──────────────────────────────────────

_DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bsudo\b",
    r"\bdd\s+if=",
    r"\bmkfs\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bchmod\s+777\b",
    r">\s*/dev/",
    r"\bkill\s+-9\b",
    r"\bpkill\b",
    r"\bformat\b",
    r"\bdel\s+/[Ff]\b",
    r"\btaskkill\b",
    r"\brd\s+/[Ss]\b",
    r"\bnpx\s+--yes\b",
    r"\bcurl\b.*\|\s*bash",
]

_DANGEROUS_RE = [re.compile(p) for p in _DANGEROUS_PATTERNS]


def _is_dangerous(command: str) -> bool:
    return any(r.search(command) for r in _DANGEROUS_RE)


# ── settings.json 规则加载 ────────────────────────────────────────────────────

def _load_permission_rules() -> dict:
    """从 ~/.claude/settings.json 和 .claude/settings.json 加载权限规则，项目级优先。"""
    rules = {"allow": [], "deny": []}
    for base in [Path.home() / ".claude", Path(".claude")]:
        path = base / "settings.json"
        if path.exists():
            try:
                data = json.loads(path.read_text())
                perms = data.get("permissions", {})
                rules["allow"].extend(perms.get("allow", []))
                rules["deny"].extend(perms.get("deny", []))
            except Exception:
                pass
    return rules


def _match_rule(rule: str, tool_name: str, args: dict) -> bool:
    """检查工具调用是否匹配一条规则，格式：tool_name 或 tool_name(path_pattern)。"""
    if "(" in rule:
        name, rest = rule.split("(", 1)
        pattern = rest.rstrip(")")
        if name.strip() != tool_name:
            return False
        path_arg = args.get("path", args.get("file_path", ""))
        return Path(path_arg).match(pattern) if path_arg else False
    return rule.strip() == tool_name


# ── 权限检查 ──────────────────────────────────────────────────────────────────

# 各工具的危险等级
# "safe"    → 任何模式下均可自动执行
# "write"   → acceptEdits/bypassPermissions 自动执行，default 需确认，plan 拒绝
# "exec"    → bypassPermissions 自动执行，其余需确认或拒绝
_TOOL_LEVELS: dict[str, str] = {
    "read_file":       "safe",
    "list_files":      "safe",
    "grep_search":     "safe",
    "web_fetch":       "safe",
    "save_memory":     "safe",
    "write_file":      "write",
    "edit_file":       "write",
    "run_shell":       "exec",
    "enter_plan_mode": "safe",
    "exit_plan_mode":  "safe",
    "agent":           "safe",
    "skill":           "safe",
    "tool_search":     "safe",
}


def check_permission(
    tool_name: str,
    args: dict,
    permission_mode: str,
    confirmed_paths: set[str],
) -> str:
    """
    返回值：
      "allow"   → 直接执行
      "deny"    → 拒绝，返回错误信息给 LLM
      "confirm" → 需要 interrupt() 询问用户
    """
    # settings.json 显式 deny 优先
    rules = _load_permission_rules()
    for rule in rules["deny"]:
        if _match_rule(rule, tool_name, args):
            return "deny"

    # settings.json 显式 allow
    for rule in rules["allow"]:
        if _match_rule(rule, tool_name, args):
            return "allow"

    level = _TOOL_LEVELS.get(tool_name, "exec")

    if permission_mode == "bypassPermissions":
        return "allow"

    if permission_mode == "dontAsk":
        return "deny" if level in ("write", "exec") else "allow"

    if permission_mode == "plan":
        if level in ("write", "exec"):
            return "deny"
        return "allow"

    if permission_mode == "acceptEdits":
        if level == "exec":
            # 危险命令仍需确认
            if tool_name == "run_shell" and _is_dangerous(args.get("command", "")):
                return "confirm"
            return "allow"
        return "allow"

    # default 模式
    if level == "safe":
        return "allow"

    # write/exec：已确认过的路径跳过
    path_key = args.get("path", args.get("file_path", ""))
    if path_key and path_key in confirmed_paths:
        return "allow"

    return "confirm"


# ── 工具实现 ──────────────────────────────────────────────────────────────────

def _abs(path: str) -> Path:
    """将相对路径解析为 WORKDIR 下的绝对路径，并校验不越界。"""
    p = (WORKDIR / path).resolve()
    if not str(p).startswith(str(WORKDIR.resolve())):
        raise ValueError(f"路径越界：{path}")
    return p


async def _handle_read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
    try:
        p = _abs(path)
        _read_file_state[str(p)] = p.stat().st_mtime
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[offset: offset + limit]
        numbered = "\n".join(f"{offset + i + 1}\t{line}" for i, line in enumerate(selected))
        total = len(lines)
        header = f"[文件: {path} | 共 {total} 行 | 显示 {offset+1}-{offset+len(selected)}]\n"
        return header + numbered
    except Exception as e:
        return f"Error: {e}"


async def _handle_write_file(path: str, content: str) -> str:
    try:
        p = _abs(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        _read_file_state[str(p)] = p.stat().st_mtime
        return f"已写入 {path}（{len(content)} 字节）"
    except Exception as e:
        return f"Error: {e}"


async def _handle_edit_file(path: str, old_string: str, new_string: str) -> str:
    try:
        p = _abs(path)

        # read-before-edit 检查
        recorded = _read_file_state.get(str(p))
        current_mtime = p.stat().st_mtime if p.exists() else None
        if recorded is None:
            return "Error: 编辑前必须先用 read_file 读取文件。"
        if current_mtime and abs(current_mtime - recorded) > 0.01:
            return "Warning: 文件在上次读取后已被外部修改，请重新 read_file 确认内容。"

        # 规范化引号（处理智能引号）
        def normalize(s: str) -> str:
            return s.replace("'", "'").replace("'", "'") \
                    .replace(""", '"').replace(""", '"')

        content = p.read_text(encoding="utf-8", errors="replace")
        old_norm = normalize(old_string)
        content_norm = normalize(content)

        count = content_norm.count(old_norm)
        if count == 0:
            return "Error: 未找到指定文本，请用 read_file 确认内容后重试。"
        if count > 1:
            return f"Error: 找到 {count} 处匹配，请提供更多上下文使其唯一。"

        new_content = content_norm.replace(old_norm, new_string, 1)
        p.write_text(new_content, encoding="utf-8")
        _read_file_state[str(p)] = p.stat().st_mtime

        # 生成简单 diff 摘要
        old_lines = old_string.splitlines()
        new_lines = new_string.splitlines()
        diff_lines = [f"- {l}" for l in old_lines] + [f"+ {l}" for l in new_lines]
        return f"已编辑 {path}\n" + "\n".join(diff_lines[:20])
    except Exception as e:
        return f"Error: {e}"


async def _handle_list_files(path: str = ".", pattern: str = "**/*") -> str:
    try:
        p = _abs(path)
        entries = sorted(p.glob(pattern))
        lines = []
        for entry in entries[:200]:
            rel = entry.relative_to(WORKDIR)
            suffix = "/" if entry.is_dir() else ""
            lines.append(str(rel) + suffix)
        result = "\n".join(lines)
        if len(entries) > 200:
            result += f"\n...（共 {len(entries)} 项，只显示前 200）"
        return result or "（空目录）"
    except Exception as e:
        return f"Error: {e}"


async def _handle_grep_search(
    pattern: str,
    path: str = ".",
    include: str = "",
    ignore_case: bool = False,
) -> str:
    try:
        target = _abs(path)
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)

        include_pat = re.compile(include) if include else None
        results = []

        for f in sorted(target.rglob("*")):
            if not f.is_file():
                continue
            if include_pat and not include_pat.search(f.name):
                continue
            try:
                for i, line in enumerate(
                    f.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if regex.search(line):
                        rel = f.relative_to(WORKDIR)
                        results.append(f"{rel}:{i}: {line.rstrip()}")
                        if len(results) >= 500:
                            break
            except Exception:
                continue
            if len(results) >= 500:
                break

        if not results:
            return "未找到匹配项。"
        output = "\n".join(results[:500])
        if len(results) == 500:
            output += "\n...（结果超过 500 条，已截断）"
        return output
    except re.error as e:
        return f"Error: 无效正则表达式 — {e}"
    except Exception as e:
        return f"Error: {e}"


async def _handle_web_fetch(url: str, prompt: str = "") -> str:
    try:
        import urllib.request
        from html.parser import HTMLParser

        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.texts: list[str] = []
                self._skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style"):
                    self._skip = True

            def handle_endtag(self, tag):
                if tag in ("script", "style"):
                    self._skip = False

            def handle_data(self, data):
                if not self._skip:
                    text = data.strip()
                    if text:
                        self.texts.append(text)

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        parser = TextExtractor()
        parser.feed(raw)
        content = "\n".join(parser.texts)[:50000]
        return content if content else "（页面无可提取文本）"
    except Exception as e:
        return f"Error: {e}"



async def _handle_save_memory(
    name: str,
    description: str,
    mem_type: str,
    content: str,
) -> str:
    """保存一条长期记忆。"""
    try:
        from memory import save_memory
        path = save_memory(name, description, mem_type, content)
        return f"记忆已保存：{path.name}"
    except Exception as e:
        return f"Error: {e}"


async def _handle_run_shell(command: str, timeout: int = 30) -> str:
    """通过 OpenSandbox 在沙箱中执行命令，详见 sandbox.py。"""
    try:
        from sandbox import get_sandbox
        return await get_sandbox().run(command, timeout=timeout)
    except Exception as e:
        return f"Error: {e}"


async def _handle_enter_plan_mode() -> str:
    # 实际模式切换由 agent.py 的 tools 节点处理（需要修改 state）
    return "__enter_plan_mode__"


async def _handle_exit_plan_mode() -> str:
    return "__exit_plan_mode__"



# ── 工具 schema 定义（供 LLM binding）────────────────────────────────────────

_CORE_TOOLS: list[dict] = [
    {
        "name": "read_file",
        "description": (
            "读取文件内容，带行号。编辑文件前必须先调用此工具。"
            "支持 offset/limit 分页读取大文件。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path":   {"type": "string", "description": "相对于工作目录的文件路径"},
                "offset": {"type": "integer", "description": "起始行（0-based），默认 0"},
                "limit":  {"type": "integer", "description": "读取行数，默认 2000"},
            },
            "required": ["path"],
        },
        "deferred": False,
    },
    {
        "name": "write_file",
        "description": "创建或完整覆盖文件。路径相对于工作目录，父目录不存在时自动创建。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "文件内容"},
            },
            "required": ["path", "content"],
        },
        "deferred": False,
    },
    {
        "name": "edit_file",
        "description": (
            "通过精确字符串替换修改文件。old_string 必须在文件中唯一出现。"
            "调用前必须已用 read_file 读取过该文件。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path":       {"type": "string", "description": "文件路径"},
                "old_string": {"type": "string", "description": "要替换的原始文本（必须唯一）"},
                "new_string": {"type": "string", "description": "替换后的新文本"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        "deferred": False,
    },
    {
        "name": "list_files",
        "description": "列出目录下的文件和子目录，支持 glob 模式过滤。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "目录路径，默认当前目录"},
                "pattern": {"type": "string", "description": "glob 模式，默认 **/*"},
            },
            "required": [],
        },
        "deferred": False,
    },
    {
        "name": "grep_search",
        "description": "在文件中搜索正则表达式，返回匹配行及文件名和行号。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern":     {"type": "string", "description": "正则表达式"},
                "path":        {"type": "string", "description": "搜索目录，默认当前目录"},
                "include":     {"type": "string", "description": "文件名过滤正则，如 \\.py$"},
                "ignore_case": {"type": "boolean", "description": "是否忽略大小写"},
            },
            "required": ["pattern"],
        },
        "deferred": False,
    },
    {
        "name": "run_shell",
        "description": (
            "在隔离沙箱（OpenSandbox）中执行 shell 命令，返回 stdout + stderr。"
            "沙箱内已包含当前项目文件（自动增量同步）。"
            "不会影响宿主机环境。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 30"},
            },
            "required": ["command"],
        },
        "deferred": False,
    },
    {
        "name": "web_fetch",
        "description": "获取网页内容，剥离 HTML 标签后返回纯文本。",
        "input_schema": {
            "type": "object",
            "properties": {
                "url":    {"type": "string", "description": "目标 URL"},
                "prompt": {"type": "string", "description": "（可选）提取方向提示"},
            },
            "required": ["url"],
        },
        "deferred": False,
    },
    {
        "name": "save_memory",
        "description": (
            "保存一条长期记忆到文件系统，供未来会话使用。"
            "适合保存：用户偏好（user）、工作方式反馈（feedback）、项目决策（project）、外部资源位置（reference）。"
            "不要保存可从代码直接读取的内容。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":        {"type": "string", "description": "记忆名称（简短，用于索引展示）"},
                "description": {"type": "string", "description": "一句话描述这条记忆的内容"},
                "mem_type":    {"type": "string", "enum": ["user", "feedback", "project", "reference"],
                                "description": "记忆类型"},
                "content":     {"type": "string", "description": "记忆正文（Markdown 格式）"},
            },
            "required": ["name", "description", "mem_type", "content"],
        },
        "deferred": False,
    },
    {
        "name": "enter_plan_mode",
        "description": (
            "切换到只读规划模式（plan）。此后所有写操作和 shell 执行均被拒绝，"
            "适合分析代码、设计方案阶段。用 exit_plan_mode 恢复。"
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "deferred": False,
    },
    {
        "name": "exit_plan_mode",
        "description": "退出只读规划模式，恢复进入前的权限模式。",
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "deferred": False,
    },
    {
        "name": "agent",
        "description": (
            "启动一个独立子 Agent 执行任务，子 Agent 有独立的 context window，"
            "结果以字符串返回。适合代码探索、方案分析等需要大量工具调用但不想污染主 context 的任务。\n"
            "类型：\n"
            "  explore  — 只读（read_file/list_files/grep_search），适合搜索代码\n"
            "  plan     — 只读，输出实现方案\n"
            "  general  — 全工具（除 agent 自身），适合独立子任务\n"
            "  自定义名称 — 读取 .claude/agents/<name>.md 的配置"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type":        {"type": "string", "description": "子 Agent 类型"},
                "description": {"type": "string", "description": "任务简述（用于日志展示）"},
                "prompt":      {"type": "string", "description": "发给子 Agent 的完整指令"},
            },
            "required": ["type", "prompt"],
        },
        "deferred": False,
    },
]

# deferred 工具：默认不加载，通过 tool_search 激活
_DEFERRED_TOOLS: list[dict] = [
    {
        "name": "skill",
        "description": "调用已注册的 Skill 模板，注入专项知识或执行预定义工作流。",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill 名称"},
                "args": {"type": "string", "description": "传给 Skill 的参数"},
            },
            "required": ["name"],
        },
        "deferred": True,
    },
    {
        "name": "tool_search",
        "description": "搜索并激活 deferred 工具。当需要使用未加载的工具时调用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "工具名称或功能描述"},
            },
            "required": ["query"],
        },
        "deferred": True,
    },
]

_ALL_TOOLS = _CORE_TOOLS + _DEFERRED_TOOLS
_TOOL_MAP = {t["name"]: t for t in _ALL_TOOLS}


def get_active_tool_definitions() -> list[dict]:
    """返回当前激活的工具 schema 列表（非 deferred 或已激活的）。"""
    return [
        {k: v for k, v in t.items() if k != "deferred"}
        for t in _ALL_TOOLS
        if not t["deferred"] or t["name"] in _activated_tools
    ]


async def _handle_tool_search(query: str) -> str:
    """激活匹配的 deferred 工具，返回工具描述。"""
    results = []
    for t in _DEFERRED_TOOLS:
        if query.lower() in t["name"].lower() or query.lower() in t["description"].lower():
            _activated_tools.add(t["name"])
            results.append(f"已激活工具：{t['name']} — {t['description']}")
    if not results:
        names = [t["name"] for t in _DEFERRED_TOOLS]
        return f"未找到匹配工具。可用的 deferred 工具：{names}"
    return "\n".join(results)


# ── 统一调度入口 ──────────────────────────────────────────────────────────────

_HANDLERS: dict[str, Any] = {
    "read_file":       _handle_read_file,
    "write_file":      _handle_write_file,
    "edit_file":       _handle_edit_file,
    "list_files":      _handle_list_files,
    "grep_search":     _handle_grep_search,
    "web_fetch":       _handle_web_fetch,
    "save_memory":     _handle_save_memory,
    "run_shell":       _handle_run_shell,
    "enter_plan_mode": _handle_enter_plan_mode,
    "exit_plan_mode":  _handle_exit_plan_mode,
    "tool_search":     _handle_tool_search,
}


async def execute_tool(tool_name: str, args: dict) -> str:
    """
    执行工具调用，返回结果字符串。
    权限检查由 agent.py 的 tools 节点负责（在调用此函数前完成）。
    """
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return f"Error: 未知工具 '{tool_name}'"

    # deferred 工具未激活时提示
    tool_def = _TOOL_MAP.get(tool_name, {})
    if tool_def.get("deferred") and tool_name not in _activated_tools:
        return (
            f"Error: 工具 '{tool_name}' 未激活。请先调用 tool_search 激活它。"
        )

    try:
        result = await handler(**args)
        # 超长结果截断（保留头尾）
        if isinstance(result, str) and len(result) > 50000:
            head = result[:25000]
            tail = result[-5000:]
            result = head + f"\n\n...[内容过长，已截断 {len(result)-30000} 字符]...\n\n" + tail
        return result
    except TypeError as e:
        return f"Error: 参数错误 — {e}"
    except Exception as e:
        return f"Error: {e}"
