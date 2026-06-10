"""
subagent.py - 子 Agent 系统

子 Agent 是独立的轻量 Agent 实例：
  - 有自己的消息历史（context 完全隔离）
  - 工具集按类型限制
  - 结果以字符串返回给父 Agent
  - 不使用 LangGraph StateGraph（一次性任务，无需 checkpoint）

三种内置类型：
  explore  → 只读（read_file / list_files / grep_search）
  plan     → 只读，输出实现方案
  general  → 全部工具（排除 agent 自身，防递归）

自定义类型：读取 .claude/agents/{name}.md，frontmatter 定义 allowed-tools
"""

from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from tools import execute_tool, get_active_tool_definitions

# ── 内置 Prompt ───────────────────────────────────────────────────────────────

_EXPLORE_PROMPT = """\
你是一个代码探索专家，专注于快速定位代码库中的信息。

规则：
- 只能使用只读工具（read_file / list_files / grep_search）
- 禁止创建、修改或删除任何文件
- 优先并行调用工具以提高效率
- 用 list_files 了解目录结构，用 grep_search 定位符号，用 read_file 读取具体内容
- 返回简洁的发现报告，只包含关键信息
"""

_PLAN_PROMPT = """\
你是一个软件架构分析师，负责设计实现方案。

规则：
- 只能使用只读工具（read_file / list_files / grep_search）
- 禁止修改任何文件
- 先充分探索代码库，再输出方案
- 输出结构化方案，包含：
  1. 现状概述
  2. 分步实现计划
  3. 需要修改的关键文件及修改要点
  4. 潜在风险与注意事项
"""

_GENERAL_PROMPT = """\
你是一个独立任务执行专家，负责完成具体的编程子任务。

规则：
- 可使用除 agent 外的全部工具
- 编辑文件前必须先用 read_file 读取
- 完成任务后输出简洁的执行结果摘要
"""

# 只读工具集
_READ_ONLY_TOOLS = {"read_file", "list_files", "grep_search"}

# ── 自定义 Agent 缓存 ─────────────────────────────────────────────────────────

_cached_custom_agents: dict[str, dict] | None = None


def reset_agent_cache() -> None:
    global _cached_custom_agents
    _cached_custom_agents = None


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


def _load_agents_from_dir(base: Path, result: dict) -> None:
    if not base.is_dir():
        return
    for f in base.glob("*.md"):
        try:
            text = f.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            name = meta.get("name", f.stem).strip()
            if not name:
                continue
            raw_tools = meta.get("allowed-tools", "").strip()
            allowed_tools = None
            if raw_tools:
                allowed_tools = [s.strip() for s in raw_tools.split(",") if s.strip()]
            result[name] = {
                "system_prompt":  body.strip(),
                "description":    meta.get("description", ""),
                "allowed_tools":  allowed_tools,
            }
        except Exception:
            continue


def _discover_custom_agents() -> dict[str, dict]:
    global _cached_custom_agents
    if _cached_custom_agents is not None:
        return _cached_custom_agents
    agents: dict[str, dict] = {}
    _load_agents_from_dir(Path.home() / ".claude" / "agents", agents)
    _load_agents_from_dir(Path(".claude") / "agents", agents)
    _cached_custom_agents = agents
    return agents


# ── Agent 配置 ────────────────────────────────────────────────────────────────

def get_sub_agent_config(agent_type: str) -> dict:
    """
    返回 {system_prompt, tools, permission_mode} 字典。
    tools 是过滤后的 schema 列表（直接传给 model.bind_tools 用的格式）。
    """
    all_tools = get_active_tool_definitions()

    # 先查自定义 agent
    custom = _discover_custom_agents().get(agent_type)
    if custom:
        if custom["allowed_tools"]:
            tools = [t for t in all_tools if t["name"] in custom["allowed_tools"]]
        else:
            tools = [t for t in all_tools if t["name"] != "agent"]
        return {
            "system_prompt":   custom["system_prompt"],
            "tools":           tools,
        }

    # 内置类型
    read_only = [t for t in all_tools if t["name"] in _READ_ONLY_TOOLS]

    if agent_type == "explore":
        return {"system_prompt": _EXPLORE_PROMPT, "tools": read_only}
    if agent_type == "plan":
        return {"system_prompt": _PLAN_PROMPT,    "tools": read_only}

    # general（默认）
    return {
        "system_prompt": _GENERAL_PROMPT,
        "tools": [t for t in all_tools if t["name"] != "agent"],
    }


# ── 子 Agent 运行循环 ─────────────────────────────────────────────────────────

_MAX_SUB_AGENT_TURNS = 30


async def run_sub_agent(
    prompt: str,
    agent_type: str,
    model: Any,
    parent_permission_mode: str,
) -> str:
    """
    启动一个独立子 Agent，运行完整的工具调用循环，返回最终文本输出。

    权限继承：
      父为 plan → 子也用 plan（只读）
      其他      → 子用 bypassPermissions（不弹确认框）
    """
    config = get_sub_agent_config(agent_type)
    system_prompt = config["system_prompt"]
    allowed_tool_schemas = config["tools"]
    allowed_tool_names = {t["name"] for t in allowed_tool_schemas}

    # 权限继承
    sub_permission_mode = (
        "plan" if parent_permission_mode == "plan" else "bypassPermissions"
    )

    # 把工具 schema 绑定到模型
    # get_active_tool_definitions() 返回的是 dict 格式，需转为 LangChain tool 格式
    lc_tools = _schemas_to_lc_tools(allowed_tool_schemas)
    sub_model = model.bind_tools(lc_tools) if lc_tools else model

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=prompt),
    ]
    output_parts: list[str] = []

    for turn in range(_MAX_SUB_AGENT_TURNS):
        try:
            response = await sub_model.ainvoke(messages)
        except Exception as e:
            return f"Error: 子 Agent 调用 LLM 失败 — {e}"

        # 收集文本输出
        text = response.content if isinstance(response.content, str) else ""
        if text.strip():
            output_parts.append(text)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break

        messages.append(response)

        # 执行工具（子 agent 的权限模式为 bypassPermissions，不需要 interrupt）
        for tc in tool_calls:
            name = tc["name"]
            args = tc["args"] if isinstance(tc["args"], dict) else {}

            # 工具白名单检查
            if name not in allowed_tool_names:
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


def _schemas_to_lc_tools(schemas: list[dict]) -> list:
    """
    将原始 tool schema 字典列表转为 LangChain 可识别的格式。
    LangChain 的 bind_tools 接受 dict（含 name/description/input_schema）。
    """
    lc_tools = []
    for s in schemas:
        # LangChain bind_tools 可直接接受符合 OpenAI function calling 格式的 dict
        lc_tools.append({
            "type": "function",
            "function": {
                "name":        s["name"],
                "description": s["description"],
                "parameters":  s.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return lc_tools


# ── agent 工具的处理函数（由 agent.py 的 tools 节点调用）─────────────────────

async def handle_agent_tool(
    agent_type: str,
    prompt: str,
    description: str,
    model: Any,
    parent_permission_mode: str,
) -> str:
    """
    agent 工具的执行入口。
    agent.py 的 tools 节点检测到 tool_call.name == "agent" 时调用此函数。
    """
    print(f"\n\033[36m[子 Agent: {agent_type}] {description}\033[0m")
    result = await run_sub_agent(
        prompt=prompt,
        agent_type=agent_type,
        model=model,
        parent_permission_mode=parent_permission_mode,
    )
    print(f"\033[36m[子 Agent: {agent_type}] 完成\033[0m")
    return result
