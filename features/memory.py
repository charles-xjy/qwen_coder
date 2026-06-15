"""
memory.py - 文件记忆系统

存储：~/.qwen-coder/projects/{project_hash}/memory/
  - 每条记忆是一个 Markdown 文件，YAML frontmatter 含 name / description / type
  - MEMORY.md 为自动维护的索引（最多 200 行，25KB 上限）

检索：sideQuery
  - 将索引发给 LLM，由 LLM 选出最相关的 ≤5 个文件名
  - 异步后台执行，不阻塞主 Agent 循环

4 种记忆类型：user / feedback / project / reference
大小限制：单文件注入 4KB，单会话注入总量 60KB
"""

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.tools import WORKDIR

# ── 常量 ──────────────────────────────────────────────────────────────────────

MEMORY_TYPES = ("user", "feedback", "project", "reference")

_MAX_INDEX_LINES   = 200
_MAX_INDEX_BYTES   = 25_000
_MAX_FILE_BYTES    = 4_096
_MAX_SESSION_BYTES = 60_000
_FRESHNESS_DAYS    = 1      # 超过此天数显示新鲜度警告
_MAX_MEMORIES_SCAN = 200    # 扫描文件数上限
_SIDE_QUERY_TOP_K  = 5      # sideQuery 最多选取条数


# ── 路径 ──────────────────────────────────────────────────────────────────────

def _memory_dir() -> Path:
    """记忆目录：项目根目录下的 .memory/，随项目一起版本控制。"""
    d = WORKDIR / ".memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _memory_index_path() -> Path:
    return _memory_dir() / "MEMORY.md"


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class MemoryHeader:
    """轻量元数据，只读 frontmatter，用于 sideQuery 构建 manifest。"""
    filename:    str
    path:        Path
    mtime_ms:    int
    name:        str
    description: str
    type:        str


@dataclass
class RelevantMemory:
    """sideQuery 选中后完整加载的记忆，注入 system prompt。"""
    path:    Path
    mtime:   float
    header:  str    # 格式化的标题行（含新鲜度警告）
    content: str    # 完整 Markdown 内容（截断至 4KB）


# ── Frontmatter 解析 ──────────────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    解析 YAML frontmatter，返回 (metadata_dict, body)。
    frontmatter 格式：文件开头 --- ... --- 包裹的区域。
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw_fm = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    meta: dict = {}
    for line in raw_fm.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta, body


def _slugify(name: str) -> str:
    """将记忆名称转为安全文件名。"""
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    return re.sub(r"[\s_-]+", "_", slug).strip("_")[:50]


# ── 新鲜度 ────────────────────────────────────────────────────────────────────

def _memory_age_days(mtime: float) -> float:
    return (time.time() - mtime) / 86400


def _relative_time(mtime: float) -> str:
    """把 mtime 转成精确到分钟级的相对时间，用于冲突时按新旧裁决。

    与 _freshness_warning 不同：后者只在超过 1 天后才出现，无法区分同一
    会话内几分钟差距的两条记忆；本函数任何时刻都给出可比较的相对时间。
    """
    secs = max(0.0, time.time() - mtime)
    if secs < 60:
        return "刚刚"
    mins = secs / 60
    if mins < 60:
        return f"{mins:.0f} 分钟前"
    hours = mins / 60
    if hours < 24:
        return f"{hours:.0f} 小时前"
    return f"{hours / 24:.0f} 天前"


def _freshness_warning(mtime: float) -> str:
    days = _memory_age_days(mtime)
    if days < _FRESHNESS_DAYS:
        return ""
    if days < 7:
        return f"[注意：此记忆 {days:.0f} 天前记录，可能已过时]"
    return f"[警告：此记忆 {days:.0f} 天前记录，请以当前代码为准]"


# ── 核心操作 ──────────────────────────────────────────────────────────────────

def save_memory(
    name: str,
    description: str,
    mem_type: str,
    content: str,
) -> Path:
    """
    保存一条记忆到文件，自动更新 MEMORY.md 索引。
    filename 格式：{type}_{slugified_name}.md
    """
    if mem_type not in MEMORY_TYPES:
        raise ValueError(f"无效的记忆类型：{mem_type}，可选：{MEMORY_TYPES}")

    slug = _slugify(name)
    filename = f"{mem_type}_{slug}.md"
    path = _memory_dir() / filename

    frontmatter = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"type: {mem_type}\n"
        "---\n\n"
    )
    path.write_text(frontmatter + content, encoding="utf-8")
    _update_memory_index()
    return path


def delete_memory(filename: str) -> bool:
    """删除指定记忆文件，自动更新索引。返回是否成功。"""
    path = _memory_dir() / filename
    if not path.exists() or filename == "MEMORY.md":
        return False
    path.unlink()
    _update_memory_index()
    return True


def list_memories() -> list[MemoryHeader]:
    """
    扫描记忆目录，返回所有有效记忆的 MemoryHeader 列表（按 mtime 倒序）。
    只读 frontmatter 前 30 行，不加载完整内容。
    """
    d = _memory_dir()
    headers: list[MemoryHeader] = []

    for f in sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.name == "MEMORY.md":
            continue
        if len(headers) >= _MAX_MEMORIES_SCAN:
            break
        try:
            # 只读前 30 行以提高速度
            lines = []
            with f.open(encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    if i >= 30:
                        break
                    lines.append(line)
            meta, _ = _parse_frontmatter("".join(lines))
            if not all(k in meta for k in ("name", "description", "type")):
                continue
            stat = f.stat()
            headers.append(MemoryHeader(
                filename=f.name,
                path=f,
                mtime_ms=int(stat.st_mtime * 1000),
                name=meta["name"],
                description=meta["description"],
                type=meta["type"],
            ))
        except Exception:
            continue

    return headers


def load_memory_index() -> str:
    """
    读取 MEMORY.md，应用行数和字节数上限截断。
    用于构建 sideQuery 的 manifest。
    """
    path = _memory_index_path()
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""

    lines = text.splitlines(keepends=True)
    if len(lines) > _MAX_INDEX_LINES:
        lines = lines[:_MAX_INDEX_LINES]
        lines.append(f"\n...（索引已截断，共 {len(lines)} 条）\n")

    result = "".join(lines)
    if len(result.encode()) > _MAX_INDEX_BYTES:
        result = result.encode()[:_MAX_INDEX_BYTES].decode(errors="replace")
        result += "\n...（索引已截断）\n"

    return result


def _update_memory_index() -> None:
    """重新生成 MEMORY.md 索引，在任何 CRUD 操作后调用。"""
    headers = list_memories()
    lines = ["# 记忆索引\n\n"]
    for h in headers:
        warn = "⚠️ " if _memory_age_days(h.mtime_ms / 1000) >= _FRESHNESS_DAYS else ""
        lines.append(f"- [{h.name}]({h.filename}) — {warn}{h.description}\n")

    content = "".join(lines)
    # 强制截断
    if len(content.encode()) > _MAX_INDEX_BYTES:
        content = content.encode()[:_MAX_INDEX_BYTES].decode(errors="replace")
        content += "\n...（索引已截断）\n"

    _memory_index_path().write_text(content, encoding="utf-8")


# ── sideQuery：语义检索 ───────────────────────────────────────────────────────

async def select_relevant_memories(
    query: str,
    model: Any,
    already_surfaced: set[str],
    recent_tools: list[str] | None = None,
) -> list[RelevantMemory]:
    """
    将记忆 manifest 发给 LLM，让它选出与 query 最相关的 ≤5 条记忆。
    已在本会话注入过的记忆（already_surfaced）不重复注入。
    recent_tools：本轮已调用的工具名，跳过这些工具的 reference 文档。

    返回完整加载的 RelevantMemory 列表，可直接注入 system prompt。
    """
    index = load_memory_index()
    if not index.strip():
        return []

    # 过滤掉已注入的
    headers = [h for h in list_memories() if h.filename not in already_surfaced]
    if not headers:
        return []

    # 构建 manifest（只含文件名、描述、类型、相对时间，不含内容）
    # 带上相对时间，便于 LLM 在描述相近/冲突时把新旧两条都选出来交主模型裁决
    manifest_lines = []
    for h in headers:
        rel = _relative_time(h.mtime_ms / 1000)
        manifest_lines.append(
            f"- {h.filename}: {h.description} [类型: {h.type}, 记录于: {rel}]"
        )
    manifest = "\n".join(manifest_lines)

    # recentTools 过滤说明
    recent_tools_section = ""
    if recent_tools:
        tools_str = ", ".join(recent_tools)
        recent_tools_section = (
            f"\n本轮已使用的工具：{tools_str}。"
            "不要选择这些工具的 reference 类型文档——对话历史中已有使用示例，注入会造成冗余。\n"
        )

    prompt = (
        f"用户当前的查询/任务：\n{query}\n"
        f"{recent_tools_section}\n"
        f"可用记忆列表：\n{manifest}\n\n"
        f"从上述列表中选出最相关的记忆文件名（最多 {_SIDE_QUERY_TOP_K} 个）。\n"
        "注意：若有多条记忆描述同一主题但可能相互冲突（如先后记录的偏好不同），"
        "请把它们**全部**选出（连同较旧的一条），交由主模型结合时间戳裁决，不要只选其一。\n"
        '只返回 JSON，不加任何解释，格式：{"selected": ["filename1.md", "filename2.md"]}\n'
        '没有相关记忆则返回：{"selected": []}'
    )

    try:
        from langchain_core.messages import HumanMessage
        # max_tokens=256 足够输出 5 个文件名的 JSON，避免模型输出冗余文字
        response = await model.bind(max_tokens=256).ainvoke([HumanMessage(content=prompt)])
        raw = response.content.strip()

        # 提取 JSON（兼容 LLM 在 JSON 前后加说明文字的情况）
        match = re.search(r"\{[^{}]*\}", raw)
        if not match:
            return []
        selected_names: list[str] = json.loads(match.group())["selected"]
    except Exception:
        return []

    # 加载选中文件的完整内容
    filename_map = {h.filename: h for h in headers}
    results: list[RelevantMemory] = []

    for filename in selected_names[:_SIDE_QUERY_TOP_K]:
        header = filename_map.get(filename)
        if header is None:
            continue
        try:
            raw_content = header.path.read_text(encoding="utf-8")
            _, body = _parse_frontmatter(raw_content)

            # 截断至 4KB
            body_bytes = body.encode("utf-8")
            if len(body_bytes) > _MAX_FILE_BYTES:
                body = body_bytes[:_MAX_FILE_BYTES].decode(errors="replace") + "\n...(截断)"

            rel = _relative_time(header.mtime_ms / 1000)
            warn = _freshness_warning(header.mtime_ms / 1000)
            header_text = (
                f"### 记忆：{header.name}（{header.type}，记录于 {rel}）"
                f"{' ' + warn if warn else ''}"
            )

            results.append(RelevantMemory(
                path=header.path,
                mtime=header.mtime_ms / 1000,
                header=header_text,
                content=body,
            ))
        except Exception:
            continue

    return results


async def get_memories_for_prompt(
    query: str,
    model: Any,
    already_surfaced: set[str],
    session_bytes_used: int,
    recent_tools: list[str] | None = None,
) -> tuple[str, set[str], int]:
    """
    主入口：检索相关记忆，返回可直接拼接到 system prompt 的字符串。

    返回：(prompt_section, newly_surfaced_filenames, bytes_added)
      - prompt_section：空字符串表示无相关记忆或已超出会话预算
      - newly_surfaced_filenames：本次新注入的文件名集合，调用方更新 state
      - bytes_added：本次注入的字节数
    """
    # 跳过条件：query 太短，或会话记忆预算已耗尽
    if len(query.split()) < 3:
        return "", set(), 0
    if session_bytes_used >= _MAX_SESSION_BYTES:
        return "", set(), 0

    memories = await select_relevant_memories(query, model, already_surfaced, recent_tools)
    if not memories:
        return "", set(), 0

    sections: list[str] = [
        "## 相关记忆\n",
        "（每条记忆标注了记录时间。若两条记忆就同一事项相互冲突，"
        "以记录时间较新的为准；若内容互补、并不矛盾，则同时采纳，不要因为旧而丢弃。）\n",
    ]
    newly_surfaced: set[str] = set()
    bytes_added = 0

    for mem in memories:
        block = f"{mem.header}\n\n{mem.content}\n\n"
        block_bytes = len(block.encode())

        # 超出会话预算则停止追加
        if session_bytes_used + bytes_added + block_bytes > _MAX_SESSION_BYTES:
            break

        sections.append(block)
        newly_surfaced.add(mem.path.name)
        bytes_added += block_bytes

    if not newly_surfaced:  # 标题/说明之外没有实际记忆内容
        return "", set(), 0

    return "".join(sections), newly_surfaced, bytes_added


# ── AutoDream：定期记忆整合 ───────────────────────────────────────────────────

_DREAM_MIN_HOURS    = 24   # 距上次整合至少 24 小时
_DREAM_MIN_SESSIONS = 5    # 至少累计 5 个会话
_dream_lock         = False


def _dream_state_path() -> Path:
    return _memory_dir() / ".dream_state.json"


def _load_dream_state() -> dict:
    path = _dream_state_path()
    if not path.exists():
        return {"last_dream_time": 0.0, "sessions_since_dream": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"last_dream_time": 0.0, "sessions_since_dream": 0}


def _save_dream_state(state: dict) -> None:
    _dream_state_path().write_text(json.dumps(state), encoding="utf-8")


def increment_dream_session() -> None:
    """每次会话结束时调用，累计会话计数。"""
    state = _load_dream_state()
    state["sessions_since_dream"] = state.get("sessions_since_dream", 0) + 1
    _save_dream_state(state)


def _should_run_dream() -> bool:
    state = _load_dream_state()
    hours_since = (time.time() - state.get("last_dream_time", 0)) / 3600
    sessions    = state.get("sessions_since_dream", 0)
    return hours_since >= _DREAM_MIN_HOURS and sessions >= _DREAM_MIN_SESSIONS


_DREAM_PROMPT = """\
你是记忆管理专家。以下是所有现有记忆文件的完整内容。

请整合这些记忆：
1. 合并重复或高度相关的条目为单条更完整的记忆
2. 删除过时、矛盾或不再相关的条目
3. 更新包含陈旧信息的条目

现有记忆：
{memories}

返回 JSON，描述需执行的操作：
{{
  "delete": ["filename1.md"],
  "update": [{{"filename": "旧文件名.md", "name": "...", "description": "...", "type": "user|feedback|project|reference", "content": "..."}}],
  "create": [{{"name": "...", "description": "...", "type": "user|feedback|project|reference", "content": "..."}}]
}}
只返回 JSON，不加任何解释。\
"""


async def _run_dream_impl(model: Any) -> int:
    """执行记忆整合，返回实际操作数（delete + update + create）。"""
    headers = list_memories()
    if not headers:
        return 0

    parts = []
    for h in headers:
        try:
            parts.append(f"=== {h.filename} ===\n{h.path.read_text(encoding='utf-8')}")
        except Exception:
            continue
    if not parts:
        return 0

    prompt = _DREAM_PROMPT.format(memories="\n\n".join(parts)[:12000])

    try:
        from langchain_core.messages import HumanMessage
        response = await model.bind(max_tokens=2048).ainvoke([HumanMessage(content=prompt)])
        match = re.search(r"\{.*\}", response.content.strip(), re.DOTALL)
        if not match:
            return 0
        ops = json.loads(match.group())
    except Exception:
        return 0

    count = 0

    for filename in ops.get("delete", []):
        if delete_memory(filename):
            count += 1

    for item in ops.get("update", []):
        old_path = _memory_dir() / item.get("filename", "")
        try:
            save_memory(item["name"], item["description"], item["type"], item["content"])
            new_name = f"{item['type']}_{_slugify(item['name'])}.md"
            if old_path.exists() and old_path.name != new_name:
                old_path.unlink(missing_ok=True)
            count += 1
        except Exception:
            pass

    for item in ops.get("create", []):
        try:
            save_memory(item["name"], item["description"], item["type"], item["content"])
            count += 1
        except Exception:
            pass

    return count


async def trigger_dream(model: Any) -> None:
    """会话结束时调用：双门槛（时间 + 会话数）均满足才运行整合。

    门槛：距上次整合 ≥ 24h 且累计会话数 ≥ 5。
    互斥锁防止并发；不满足门槛时立即返回，不阻塞退出。
    """
    global _dream_lock
    if _dream_lock or not _should_run_dream():
        return

    _dream_lock = True
    try:
        print("\033[35m[Dream] 正在整合长期记忆...\033[0m")
        count = await _run_dream_impl(model)
        state = _load_dream_state()
        state["last_dream_time"]     = time.time()
        state["sessions_since_dream"] = 0
        _save_dream_state(state)
        msg = f"完成，执行了 {count} 项操作" if count else "完成，无需整合"
        print(f"\033[35m[Dream] {msg}\033[0m")
    except Exception:
        pass
    finally:
        _dream_lock = False


# ── 自动记忆写入（fire-and-forget）────────────────────────────────────────────

_AUTO_SAVE_PROMPT = """\
分析以下对话，判断是否存在值得跨会话保留的长期记忆。

值得保存：
- user：用户偏好、习惯、工作风格
- feedback：用户对 Agent 行为的纠正或认可（"不要这样做"、"很好继续这样"）
- project：项目决策、架构选择、关键约束
- reference：外部资源位置（URL、文档路径、工具名）

不值得保存：普通问答、代码实现细节、可从代码直接读取的信息。

用户：{user_message}

助手：{assistant_response}

如有值得保存的内容，返回 JSON：
{{"memories": [{{"name": "简短标题", "description": "一句话摘要", "type": "user|feedback|project|reference", "content": "详细内容（Markdown）"}}]}}
没有则返回：{{"memories": []}}
只返回 JSON，不加任何解释。\
"""


async def auto_save_memory(
    user_message: str,
    assistant_response: str,
    model: Any,
) -> None:
    """fire-and-forget：分析一轮对话，自动保存值得长期记忆的内容。

    调用方用 asyncio.create_task() 触发，不阻塞主 Agent 循环。
    内部所有异常静默处理，确保后台任务不会影响主流程。
    """
    if not user_message.strip() or not assistant_response.strip():
        return

    prompt = _AUTO_SAVE_PROMPT.format(
        user_message=user_message[:800],
        assistant_response=assistant_response[:800],
    )

    try:
        from langchain_core.messages import HumanMessage
        response = await model.bind(max_tokens=512).ainvoke([HumanMessage(content=prompt)])
        raw = response.content.strip()

        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return
        data = json.loads(match.group())

        for mem in data.get("memories", []):
            name        = mem.get("name", "").strip()
            description = mem.get("description", "").strip()
            mem_type    = mem.get("type", "").strip()
            content     = mem.get("content", "").strip()
            if name and description and mem_type in MEMORY_TYPES and content:
                save_memory(name, description, mem_type, content)
    except Exception:
        pass
