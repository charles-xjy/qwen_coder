"""
skills.py - Skills 系统

Skills 是可复用的提示模板，通过 SKILL.md 文件定义。
支持用户手动触发（/skillname）和 Agent 自动调用（skill 工具）。

目录扫描顺序（项目级覆盖用户级）：
  1. ~/.claude/skills/*/SKILL.md   （用户级，低优先级）
  2. ./.claude/skills/*/SKILL.md   （项目级，高优先级）

frontmatter 字段：
  name, description, when_to_use, allowed-tools, user-invocable, context
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class SkillDefinition:
    name:          str
    description:   str
    when_to_use:   str | None       = None
    allowed_tools: list[str] | None = None   # None 表示不限制
    user_invocable: bool            = True
    context:       str              = "inline"  # "inline" | "fork"
    prompt_template: str            = ""
    source:        str              = "project"  # "project" | "user"
    skill_dir:     str              = ""


# ── 缓存 ──────────────────────────────────────────────────────────────────────

_cached_skills: dict[str, SkillDefinition] | None = None  # name → skill


def reset_skill_cache() -> None:
    """清空缓存，下次调用 discover_skills() 时重新扫描文件系统。"""
    global _cached_skills
    _cached_skills = None


# ── Frontmatter 解析（复用 memory.py 逻辑，独立实现避免循环依赖）───────────

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    meta: dict = {}
    for line in raw.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta, body


# ── 加载 ──────────────────────────────────────────────────────────────────────

def _parse_skill_file(path: Path, source: str) -> SkillDefinition | None:
    """解析单个 SKILL.md，返回 SkillDefinition，解析失败返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None

    meta, body = _parse_frontmatter(text)
    name = meta.get("name", "").strip()
    description = meta.get("description", "").strip()
    if not name or not description:
        return None

    # allowed-tools：支持 JSON 数组或逗号分隔字符串
    allowed_tools = None
    raw_tools = meta.get("allowed-tools", "").strip()
    if raw_tools:
        if raw_tools.startswith("["):
            try:
                allowed_tools = json.loads(raw_tools)
            except Exception:
                allowed_tools = [s.strip() for s in raw_tools.split(",")]
        else:
            allowed_tools = [s.strip() for s in raw_tools.split(",") if s.strip()]

    # user-invocable：默认 true
    raw_invocable = meta.get("user-invocable", "true").strip().lower()
    user_invocable = raw_invocable not in ("false", "0", "no")

    # context：inline（默认）或 fork
    context = meta.get("context", "inline").strip().lower()
    if context not in ("inline", "fork"):
        context = "inline"

    return SkillDefinition(
        name=name,
        description=description,
        when_to_use=meta.get("when_to_use", meta.get("when-to-use", "")).strip() or None,
        allowed_tools=allowed_tools,
        user_invocable=user_invocable,
        context=context,
        prompt_template=body,
        source=source,
        skill_dir=str(path.parent),
    )


def _load_from_dir(base: Path, source: str) -> dict[str, SkillDefinition]:
    """扫描 base/*/SKILL.md，返回 name → SkillDefinition 字典。"""
    skills: dict[str, SkillDefinition] = {}
    if not base.is_dir():
        return skills
    for sub in base.iterdir():
        if not sub.is_dir():
            continue
        skill_file = sub / "SKILL.md"
        if not skill_file.exists():
            continue
        skill = _parse_skill_file(skill_file, source)
        if skill:
            skills[skill.name] = skill
    return skills


def discover_skills() -> dict[str, SkillDefinition]:
    """
    扫描用户级和项目级 skills 目录，返回 name → SkillDefinition 字典。
    使用模块级缓存；调用 reset_skill_cache() 可强制重新扫描。
    """
    global _cached_skills
    if _cached_skills is not None:
        return _cached_skills

    skills: dict[str, SkillDefinition] = {}

    # 用户级（低优先级）
    user_dir = Path.home() / ".claude" / "skills"
    skills.update(_load_from_dir(user_dir, source="user"))

    # 项目级（高优先级，同名覆盖用户级）
    project_dir = Path(".claude") / "skills"
    skills.update(_load_from_dir(project_dir, source="project"))

    _cached_skills = skills
    return skills


# ── 查询 ──────────────────────────────────────────────────────────────────────

def get_skill_by_name(name: str) -> SkillDefinition | None:
    """按名称查找 skill，不存在返回 None。"""
    return discover_skills().get(name)


def list_user_invocable_skills() -> list[SkillDefinition]:
    """返回所有 user_invocable=True 的 skills，供 REPL /cmd 补全。"""
    return [s for s in discover_skills().values() if s.user_invocable]


# ── Prompt 解析与执行 ──────────────────────────────────────────────────────────

def resolve_skill_prompt(skill: SkillDefinition, args: str = "") -> str:
    """
    将 skill 的 prompt_template 中的变量替换为实际值：
      $ARGUMENTS 或 ${ARGUMENTS}  → args
      ${CLAUDE_SKILL_DIR}         → skill 所在目录的绝对路径
    """
    prompt = skill.prompt_template
    prompt = re.sub(r"\$\{ARGUMENTS\}|\$ARGUMENTS", args, prompt)
    prompt = re.sub(r"\$\{CLAUDE_SKILL_DIR\}", skill.skill_dir, prompt)
    return prompt


def execute_skill(name: str, args: str = "") -> dict | None:
    """
    执行 skill，返回包含 prompt、allowed_tools、context 的字典。
    skill 不存在返回 None。

    调用方根据 context 决定：
      inline → 把 prompt 注入当前 Agent 的 system prompt
      fork   → 用 prompt 启动独立子 Agent（见 subagent.py）
    """
    skill = get_skill_by_name(name)
    if skill is None:
        return None
    return {
        "prompt":        resolve_skill_prompt(skill, args),
        "allowed_tools": skill.allowed_tools,
        "context":       skill.context,
        "skill_dir":     skill.skill_dir,
    }


# ── System Prompt 注入 ────────────────────────────────────────────────────────

def build_skill_catalog() -> str:
    """
    生成 skills 目录字符串，注入 Agent 的 system prompt。
    包含名称、描述、触发条件。
    """
    skills = discover_skills()
    if not skills:
        return ""

    lines = ["## 可用 Skills\n"]
    for skill in sorted(skills.values(), key=lambda s: s.name):
        invocable = "（用户可触发）" if skill.user_invocable else "（自动触发）"
        lines.append(f"- `{skill.name}` {invocable}：{skill.description}")
        if skill.when_to_use:
            lines.append(f"  触发时机：{skill.when_to_use}")
        if skill.allowed_tools:
            lines.append(f"  工具范围：{', '.join(skill.allowed_tools)}")
    return "\n".join(lines)


def parse_repl_command(inp: str) -> tuple[str, str] | None:
    """
    解析 REPL 输入中的 /command 格式。
    返回 (skill_name, args) 或 None（不是 /command 格式）。
    """
    if not inp.startswith("/"):
        return None
    parts = inp[1:].split(" ", 1)
    name = parts[0].strip()
    args = parts[1].strip() if len(parts) > 1 else ""
    return (name, args) if name else None
