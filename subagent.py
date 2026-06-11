"""
subagent.py - 子 Agent 系统

基于 Deer Flow 的 SubagentConfig 配置模型 + 轻量 for-loop 执行引擎。

所有子 Agent（包括内置的 explore/plan/general）统一通过 .claude/agents/{name}.md
定义，无硬编码特殊路径。加载优先级：项目级覆盖用户级覆盖内置级。

子 Agent 是独立的轻量 Agent 实例：
  - 有自己的消息历史（context 完全隔离）
  - 工具集通过 allowed_tools / disallowed_tools 双轴控制
  - 可按需加载 skills（注入 <skill> 标签到 system prompt）
  - 结果以字符串返回给父 Agent
  - 不使用 LangGraph StateGraph（一次性任务，无需 checkpoint）
"""

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from tools import execute_tool, get_active_tool_definitions

# ── 全局默认值（.md 文件未指定时使用）──────────────────────────────────────────
_DEFAULT_DISALLOWED_TOOLS = ["agent", "enter_plan_mode", "exit_plan_mode"]
_DEFAULT_MAX_TURNS = 50
_DEFAULT_TIMEOUT_SECONDS = 900  # 15 分钟

# ── 有效权限模式 ────────────────────────────────────────────────────────────────
_VALID_PERMISSION_MODES = {"default", "plan", "acceptEdits", "bypassPermissions", "dontAsk"}


# ═══════════════════════════════════════════════════════════════════════════════
# SubagentConfig 配置模型（对齐 cc-haha BaseAgentDefinition）
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SubagentConfig:
    """子 Agent 的完整配置，所有字段均可从 .md frontmatter 读取。

    字段语义：
      - name: 唯一标识，对应 agent 工具的 type 参数
      - description: 告诉主 Agent 此子 Agent 适合什么场景
      - system_prompt: 子 Agent 的 SystemMessage 内容（.md 正文）
      - allowed_tools: 工具白名单，None 表示继承父级全量工具
      - disallowed_tools: 工具黑名单，在白名单基础上进一步剔除
      - skills: 要注入的 skill 名称列表，None 表示不注入任何 skill
      - permission_mode: 权限模式，None 表示继承父级（默认）
      - model: 模型名，"inherit" 表示沿用父级模型
      - max_turns: 最大交互轮次
      - timeout_seconds: 最大 wall-clock 执行时间
    """
    name: str
    description: str
    system_prompt: str | None = None
    allowed_tools: list[str] | None = None       # None = 继承全量
    disallowed_tools: list[str] = field(
        default_factory=lambda: list(_DEFAULT_DISALLOWED_TOOLS)
    )
    permission_mode: str | None = None           # None = 继承父级
    model: str = "inherit"
    max_turns: int = _DEFAULT_MAX_TURNS
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS


# ═══════════════════════════════════════════════════════════════════════════════
# .claude/agents/{name}.md 加载
# ═══════════════════════════════════════════════════════════════════════════════

_cached_agents: dict[str, SubagentConfig] | None = None


def reset_agent_cache() -> None:
    """清空缓存，下次调用时重新扫描 .claude/agents/ 目录。"""
    global _cached_agents
    _cached_agents = None


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML frontmatter，返回 (元数据字典, 正文)。"""
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


def _parse_tools_field(raw: str) -> list[str]:
    """解析工具列表字段（逗号分隔或 JSON 数组），空字符串返回空列表。"""
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        import json
        try:
            return json.loads(raw)
        except Exception:
            pass
    return [s.strip() for s in raw.split(",") if s.strip()]



def _parse_permission_mode(raw: str) -> str | None:
    """解析并校验 permission-mode 字段，无效值打印警告并返回 None。"""
    mode = raw.strip()
    if not mode:
        return None
    if mode in _VALID_PERMISSION_MODES:
        return mode
    import sys
    print(
        f"\033[33m[WARN] 无效的 permission-mode '{mode}'，"
        f"有效值：{', '.join(sorted(_VALID_PERMISSION_MODES))}。"
        f"已回退为继承父级权限。\033[0m",
        file=sys.stderr,
    )
    return None


def _load_agents_from_dir(base: Path, result: dict[str, SubagentConfig]) -> None:
    """扫描 base 目录下的 *.md 文件，解析为 SubagentConfig 并合并到 result。"""
    if not base.is_dir():
        return
    for f in base.glob("*.md"):
        try:
            text = f.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            name = meta.get("name", f.stem).strip()
            if not name:
                continue

            # allowed-tools：未声明 → None（继承全量）；声明但为空 → None（等同继承全量）
            allowed_raw = _parse_tools_field(meta.get("allowed-tools", ""))
            allowed_tools = allowed_raw if allowed_raw else None

            # disallowed-tools：未声明 → 默认黑名单；声明但为空 → []（无黑名单）
            if "disallowed-tools" in meta:
                disallowed_tools = _parse_tools_field(meta["disallowed-tools"])
            else:
                disallowed_tools = list(_DEFAULT_DISALLOWED_TOOLS)

            config = SubagentConfig(
                name=name,
                description=meta.get("description", "").strip(),
                system_prompt=body.strip() or None,
                allowed_tools=allowed_tools,
                disallowed_tools=disallowed_tools,
                permission_mode=_parse_permission_mode(meta.get("permission-mode", "")),
                model=meta.get("model", "inherit").strip(),
                max_turns=int(meta.get("max-turns", str(_DEFAULT_MAX_TURNS))),
                timeout_seconds=int(meta.get("timeout-seconds", str(_DEFAULT_TIMEOUT_SECONDS))),
            )
            result[name] = config
        except Exception:
            continue


def _discover_agents() -> dict[str, SubagentConfig]:
    """扫描所有 agent 定义目录，后加载覆盖先加载。

    目录优先级（低 → 高）：
      1. 包内置 agents/         （框架级：explore / plan / general）
      2. ~/.claude/agents/      （用户级）
      3. ./.claude/agents/      （项目级，最高优先级）
    """
    global _cached_agents
    if _cached_agents is not None:
        return _cached_agents

    agents: dict[str, SubagentConfig] = {}

    # 包内置（最低优先级）
    _load_agents_from_dir(Path(__file__).parent / "agents", agents)

    # 用户级
    _load_agents_from_dir(Path.home() / ".claude" / "agents", agents)

    # 项目级（最高优先级）
    _load_agents_from_dir(Path(".claude") / "agents", agents)

    _cached_agents = agents
    return agents


# ═══════════════════════════════════════════════════════════════════════════════
# 公共 API
# ═══════════════════════════════════════════════════════════════════════════════

def get_sub_agent_config(agent_type: str) -> SubagentConfig | None:
    """按名称获取子 Agent 配置，从 .claude/agents/{name}.md 加载。"""
    return _discover_agents().get(agent_type)


def list_available_subagents() -> list[str]:
    """返回所有可用的子 Agent 名称。"""
    return sorted(_discover_agents().keys())


# ═══════════════════════════════════════════════════════════════════════════════
# Skill 注入
# ═══════════════════════════════════════════════════════════════════════════════

def _build_skill_injection(skill_names: list[str]) -> tuple[str, list[str]]:
    """根据 skill 名称列表，生成要注入 system prompt 的 skill 内容段落，
    同时收集 skill 声明的额外工具名。

    返回 (prompt_text, extra_tool_names)。
    """
    from skills import discover_skills, resolve_skill_prompt

    all_skills = discover_skills()
    parts: list[str] = []
    extra_tools: list[str] = []

    for name in skill_names:
        skill = all_skills.get(name)
        if skill is None:
            continue
        resolved = resolve_skill_prompt(skill, "")
        parts.append(f'<skill name="{skill.name}">\n{resolved}\n</skill>')
        if skill.allowed_tools:
            extra_tools.extend(skill.allowed_tools)

    return "\n\n".join(parts), extra_tools


# ═══════════════════════════════════════════════════════════════════════════════
# 工具过滤
# ═══════════════════════════════════════════════════════════════════════════════

def _filter_tools(
    all_schemas: list[dict],
    config: SubagentConfig,
) -> list[dict]:
    """根据 SubagentConfig 的 allowed_tools / disallowed_tools 过滤工具 schema。

    规则：
      1. allowed_tools 为 None → 取全量工具
      2. allowed_tools 为列表 → 只取白名单中的工具
      3. 从结果中剔除 disallowed_tools 中的工具
    """
    if config.allowed_tools is None:
        filtered = list(all_schemas)
    else:
        allowed_set = set(config.allowed_tools)
        filtered = [t for t in all_schemas if t["name"] in allowed_set]

    if config.disallowed_tools:
        disallowed_set = set(config.disallowed_tools)
        filtered = [t for t in filtered if t["name"] not in disallowed_set]

    return filtered


# ═══════════════════════════════════════════════════════════════════════════════
# 子 Agent 运行循环
# ═══════════════════════════════════════════════════════════════════════════════

async def run_sub_agent(
    prompt: str,
    agent_type: str,
    model: Any,
    parent_permission_mode: str,
    extra_skills: list[str] | None = None,
) -> str:
    """启动一个独立子 Agent，运行完整的工具调用循环，返回最终文本输出。

    权限优先级（对齐 cc-haha）：
      1. config.permission_mode 显式指定 → 使用该模式
      2. 父为 plan → 子也用 plan（只读）
      3. 其他      → 子用 bypassPermissions（不弹确认框）
    """
    config = get_sub_agent_config(agent_type)
    if config is None:
        return (
            f"Error: 未知子 Agent 类型 '{agent_type}'。"
            f"可用类型：{', '.join(list_available_subagents())}"
        )

    # ── 收集 skill 内容和额外工具（必须在工具过滤之前）─────────────────────
    system_parts: list[str] = []
    skill_extra_tools: list[str] = []

    if config.system_prompt:
        system_parts.append(config.system_prompt)

    if extra_skills:
        skill_text, skill_extra_tools = _build_skill_injection(extra_skills)
        if skill_text:
            system_parts.append(skill_text)

    # ── 工具过滤（合并 skill 声明的额外工具）────────────────────────────────
    all_schemas = get_active_tool_definitions()
    effective_config = config
    if skill_extra_tools and config.allowed_tools is not None:
        # allowed_tools 不为 None 说明是白名单模式，需要合并 skill 工具
        merged = list(set(config.allowed_tools) | set(skill_extra_tools))
        effective_config = replace(config, allowed_tools=merged)
    # allowed_tools 为 None 表示继承全量，skill 工具自然包含在内，无需处理
    allowed_schemas = _filter_tools(all_schemas, effective_config)
    allowed_names = {t["name"] for t in allowed_schemas}

    # ── 权限模式解析 ────────────────────────────────────────────────────────
    if config.permission_mode:
        sub_permission_mode = config.permission_mode
    elif parent_permission_mode == "plan":
        sub_permission_mode = "plan"
    else:
        sub_permission_mode = "bypassPermissions"

    # ── 绑定工具到模型 ──────────────────────────────────────────────────────
    lc_tools = _schemas_to_lc_tools(allowed_schemas)
    sub_model = model.bind_tools(lc_tools) if lc_tools else model

    # ── 构建 System Prompt ──────────────────────────────────────────────────

    system_content = "\n\n".join(system_parts)

    messages = []
    if system_content:
        messages.append(SystemMessage(content=system_content))
    messages.append(HumanMessage(content=prompt))

    # ── 运行循环 ────────────────────────────────────────────────────────────
    output_parts: list[str] = []
    start_time = time.time()

    for turn in range(config.max_turns):
        elapsed = time.time() - start_time
        if elapsed > config.timeout_seconds:
            return (
                f"Error: 子 Agent 执行超时"
                f"（{config.timeout_seconds}s，已用 {elapsed:.0f}s，{turn} 轮）"
            )

        try:
            response = await sub_model.ainvoke(messages)
        except Exception as e:
            return f"Error: 子 Agent 调用 LLM 失败 — {e}"

        text = response.content if isinstance(response.content, str) else ""
        if text.strip():
            output_parts.append(text)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break

        messages.append(response)

        for tc in tool_calls:
            name = tc["name"]
            args = tc["args"] if isinstance(tc["args"], dict) else {}

            if name not in allowed_names:
                result = f"Error: 工具 '{name}' 不在此 Agent 的允许列表中。"
            else:
                result = await execute_tool(name, args)

            messages.append(ToolMessage(
                content=str(result),
                tool_call_id=tc["id"],
            ))

    if not output_parts:
        return "（子 Agent 完成，无文本输出）"
    return "\n\n".join(output_parts)


# ═══════════════════════════════════════════════════════════════════════════════
# 工具格式转换
# ═══════════════════════════════════════════════════════════════════════════════

def _schemas_to_lc_tools(schemas: list[dict]) -> list[dict]:
    """将原始 tool schema 字典列表转为 LangChain bind_tools 接受的格式。"""
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


# ═══════════════════════════════════════════════════════════════════════════════
# agent 工具的处理入口（由 agent.py 的 tools 节点调用）
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_agent_tool(
    agent_type: str,
    prompt: str,
    description: str,
    model: Any,
    parent_permission_mode: str,
    skills: list[str] | None = None,
) -> str:
    """agent 工具的执行入口。

    agent.py 的 tools 节点检测到 tool_call.name == "agent" 时调用此函数。
    """
    print(f"\n\033[36m[子 Agent: {agent_type}] {description}\033[0m")
    result = await run_sub_agent(
        prompt=prompt,
        agent_type=agent_type,
        model=model,
        parent_permission_mode=parent_permission_mode,
        extra_skills=skills,
    )
    print(f"\033[36m[子 Agent: {agent_type}] 完成\033[0m")
    return result
