"""
session.py - 会话管理（单文件 JSONL + MemorySaver）

存储结构（全在 WORKDIR/.sessions/ 下）：
  {session_id}.jsonl   — 第一行是元数据（__meta__: true），其余行是消息

Checkpointer 使用 MemorySaver（纯内存），不依赖 SQLite。
每轮对话结束后调用 save_turn() 把最新消息写入 JSONL。
会话恢复时读取 JSONL 重建消息列表，注入 MemorySaver。
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import messages_from_dict, messages_to_dict

from tools import WORKDIR

# ── 路径 ──────────────────────────────────────────────────────────────────────

_SESSIONS_DIR = WORKDIR / ".sessions"


def _ensure_dirs() -> None:
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _jsonl_path(session_id: str) -> Path:
    return _SESSIONS_DIR / f"{session_id}.jsonl"


# ── 会话 ID 生成 ──────────────────────────────────────────────────────────────

def new_session_id() -> str:
    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:6]
    return f"session_{ts}_{short}"


# ── 底层读写 ──────────────────────────────────────────────────────────────────

def _read_jsonl(session_id: str) -> tuple[dict, list[dict]]:
    """返回 (meta_dict, message_dicts)。文件不存在时返回空值。"""
    path = _jsonl_path(session_id)
    if not path.exists():
        return {}, []

    meta: dict = {}
    message_dicts: list[dict] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if i == 0 and obj.get("__meta__"):
            meta = obj
        else:
            message_dicts.append(obj)
    return meta, message_dicts


def _write_jsonl(session_id: str, meta: dict, messages: list) -> None:
    """把 meta 写第一行，消息写其余行。"""
    _ensure_dirs()
    path = _jsonl_path(session_id)
    lines = [json.dumps({**meta, "__meta__": True}, ensure_ascii=False)]
    for d in messages_to_dict(messages):
        lines.append(json.dumps(d, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── 元数据操作 ────────────────────────────────────────────────────────────────

def save_session(session_id: str, title: str | None = None, extra: dict | None = None) -> None:
    """创建或更新会话元数据（写入 JSONL 第一行，保留消息）。"""
    meta, messages_raw = _read_jsonl(session_id)

    # 恢复原始消息对象列表（保持不变）
    try:
        messages = messages_from_dict(messages_raw) if messages_raw else []
    except Exception:
        messages = []

    updated_meta = {
        "id":           session_id,
        "startTime":    meta.get("startTime") or _now_iso(),
        "last_updated": _now_iso(),
        "title":        title or meta.get("title"),
        **(extra or {}),
    }
    _write_jsonl(session_id, updated_meta, messages)


def load_session(session_id: str) -> dict | None:
    """读取元数据，文件不存在返回 None。"""
    meta, _ = _read_jsonl(session_id)
    return meta if meta else None


def update_title(session_id: str, title: str) -> None:
    save_session(session_id, title=title)


def list_sessions() -> list[dict]:
    _ensure_dirs()
    sessions = []
    for f in _SESSIONS_DIR.glob("*.jsonl"):
        session_id = f.stem
        meta, _ = _read_jsonl(session_id)
        if meta and "id" in meta:
            sessions.append(meta)
    sessions.sort(key=lambda s: s.get("last_updated", ""), reverse=True)
    return sessions


def get_latest_session_id() -> str | None:
    sessions = list_sessions()
    return sessions[0]["id"] if sessions else None


# ── 消息读写 ──────────────────────────────────────────────────────────────────

def save_messages(session_id: str, messages: list) -> None:
    """把消息列表写入 JSONL（保留第一行 meta）。"""
    meta, _ = _read_jsonl(session_id)
    if not meta:
        meta = {"id": session_id, "startTime": _now_iso()}
    meta["last_updated"] = _now_iso()
    _write_jsonl(session_id, meta, messages)


def load_messages(session_id: str) -> list:
    """从 JSONL 加载消息列表，文件不存在返回空列表。"""
    _, message_dicts = _read_jsonl(session_id)
    if not message_dicts:
        return []
    try:
        return messages_from_dict(message_dicts)
    except Exception:
        return []


# ── Checkpointer ──────────────────────────────────────────────────────────────

def get_checkpointer() -> MemorySaver:
    return MemorySaver()


def make_thread_config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


# ── 会话恢复 ──────────────────────────────────────────────────────────────────

async def restore_session(app: Any, config: dict, session_id: str) -> None:
    """从 JSONL 加载消息和元数据，注入 MemorySaver。"""
    from agent import make_initial_state

    messages = load_messages(session_id)
    meta     = load_session(session_id) or {}

    state = make_initial_state(meta.get("permission_mode", "default"))
    state["messages"]             = messages
    state["total_input_tokens"]   = meta.get("total_input_tokens",  0)
    state["total_output_tokens"]  = meta.get("total_output_tokens", 0)
    state["session_memory_bytes"] = meta.get("session_memory_bytes", 0)

    await app.aupdate_state(config, state)


# ── 每轮存档 ──────────────────────────────────────────────────────────────────

async def save_turn(app: Any, config: dict, session_id: str) -> None:
    """每轮对话结束后调用：把当前 messages 和统计写入 JSONL。"""
    try:
        snap  = await app.aget_state(config)
        state = snap.values
        messages = state.get("messages") or []

        meta, _ = _read_jsonl(session_id)
        if not meta:
            meta = {"id": session_id, "startTime": _now_iso()}

        meta.update({
            "last_updated":         _now_iso(),
            "permission_mode":      state.get("permission_mode", "default"),
            "total_input_tokens":   state.get("total_input_tokens",  0),
            "total_output_tokens":  state.get("total_output_tokens", 0),
            "session_memory_bytes": state.get("session_memory_bytes", 0),
        })
        _write_jsonl(session_id, meta, messages)
    except Exception:
        pass


# ── 辅助 ──────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
