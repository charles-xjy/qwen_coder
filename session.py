"""
session.py - 会话管理

职责：
  - 轻量 JSON 索引：记录 session 元数据（id / startTime / title），供启动时列表展示
  - LangGraph checkpointer：消息历史由 LangGraph 自动持久化（SQLite），本模块只负责初始化
  - --resume 支持：get_latest_session_id() 返回最近一次会话
  - 会话 ID 即 LangGraph thread_id，两者保持一致

目录结构：
  ~/.qwen-coder/
  ├── sessions/
  │   └── {session_id}.json   # 元数据（id / startTime / title / last_updated）
  └── checkpoints.sqlite       # LangGraph SQLite checkpoint（消息历史）
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── 路径 ──────────────────────────────────────────────────────────────────────

_BASE_DIR       = Path.home() / ".qwen-coder"
_SESSIONS_DIR   = _BASE_DIR / "sessions"
_CHECKPOINT_DB  = _BASE_DIR / "checkpoints.sqlite"


def _ensure_dirs() -> None:
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


# ── 会话 ID 生成 ──────────────────────────────────────────────────────────────

def new_session_id() -> str:
    """生成新的会话 ID：session_{时间戳}_{短 UUID}，可读性与唯一性兼顾。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:6]
    return f"session_{ts}_{short}"


# ── 元数据 JSON 读写 ───────────────────────────────────────────────────────────

def save_session(session_id: str, title: str | None = None) -> None:
    """创建或更新会话元数据文件。"""
    _ensure_dirs()
    path = _SESSIONS_DIR / f"{session_id}.json"

    # 保留已有 startTime
    existing = _load_raw(path)
    start_time = existing.get("startTime") if existing else _now_iso()

    data = {
        "id":           session_id,
        "startTime":    start_time,
        "last_updated": _now_iso(),
        "title":        title or existing.get("title") if existing else None,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session(session_id: str) -> dict | None:
    """加载会话元数据，不存在或损坏返回 None。"""
    return _load_raw(_SESSIONS_DIR / f"{session_id}.json")


def update_title(session_id: str, title: str) -> None:
    """在会话结束后写入 LLM 生成的标题。"""
    save_session(session_id, title=title)


# ── 会话列表 ──────────────────────────────────────────────────────────────────

def list_sessions() -> list[dict]:
    """
    返回所有会话元数据列表，按 last_updated 倒序排列。
    每条记录：{id, startTime, last_updated, title}
    """
    _ensure_dirs()
    sessions = []
    for f in _SESSIONS_DIR.glob("*.json"):
        data = _load_raw(f)
        if data and "id" in data and "startTime" in data:
            sessions.append(data)
    sessions.sort(key=lambda s: s.get("last_updated", ""), reverse=True)
    return sessions


def get_latest_session_id() -> str | None:
    """返回最近一次会话的 ID，无会话记录时返回 None。"""
    sessions = list_sessions()
    return sessions[0]["id"] if sessions else None


# ── LangGraph Checkpointer ────────────────────────────────────────────────────

def get_checkpointer():
    """
    返回 LangGraph AsyncSqliteSaver 实例。
    必须在 async with 上下文中使用：

        async with get_checkpointer() as checkpointer:
            app = builder.compile(checkpointer=checkpointer)
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    _ensure_dirs()
    return AsyncSqliteSaver.from_conn_string(str(_CHECKPOINT_DB))


def make_thread_config(session_id: str) -> dict:
    """生成 LangGraph astream/ainvoke 需要的 config 字典。"""
    return {"configurable": {"thread_id": session_id}}


# ── 辅助 ──────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_raw(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
