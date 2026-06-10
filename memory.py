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

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools import WORKDIR

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
    """当前项目的记忆目录，按 WORKDIR 哈希隔离不同项目。"""
    project_hash = hashlib.md5(str(WORKDIR).encode()).hexdigest()[:12]
    d = Path.home() / ".qwen-coder" / "projects" / project_hash / "memory"
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
) -> list[RelevantMemory]:
    """
    将 MEMORY.md 索引发给 LLM，让它选出与 query 最相关的 ≤5 条记忆。
    已在本会话注入过的记忆（already_surfaced）不重复注入。

    返回完整加载的 RelevantMemory 列表，可直接注入 system prompt。
    """
    index = load_memory_index()
    if not index.strip():
        return []

    # 过滤掉已注入的
    headers = [h for h in list_memories() if h.filename not in already_surfaced]
    if not headers:
        return []

    # 构建 manifest（只含文件名和描述，不含内容）
    manifest_lines = []
    for h in headers:
        manifest_lines.append(f"- {h.filename}: {h.description} [类型: {h.type}]")
    manifest = "\n".join(manifest_lines)

    prompt = (
        f"用户当前的查询/任务：\n{query}\n\n"
        f"可用记忆列表：\n{manifest}\n\n"
        f"请从上述列表中选出最相关的记忆文件名（最多 {_SIDE_QUERY_TOP_K} 个）。\n"
        "只返回 JSON，格式：{\"selected\": [\"filename1.md\", \"filename2.md\"]}\n"
        "如果没有相关记忆，返回：{\"selected\": []}"
    )

    try:
        from langchain_core.messages import HumanMessage
        response = await model.ainvoke([HumanMessage(content=prompt)])
        raw = response.content.strip()

        # 提取 JSON（兼容 LLM 在 JSON 前后加说明文字的情况）
        match = re.search(r"\{.*\}", raw, re.DOTALL)
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

            warn = _freshness_warning(header.mtime_ms / 1000)
            header_text = f"### 记忆：{header.name}（{header.type}）{' ' + warn if warn else ''}"

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

    memories = await select_relevant_memories(query, model, already_surfaced)
    if not memories:
        return "", set(), 0

    sections: list[str] = ["## 相关记忆\n"]
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

    if len(sections) == 1:  # 只有标题，没有内容
        return "", set(), 0

    return "".join(sections), newly_surfaced, bytes_added
