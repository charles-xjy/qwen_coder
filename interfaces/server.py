"""
server.py - FastAPI + SSE 服务

将 LangGraph agent 的 astream_events 事件流转为 SSE 推给 TS 前端。

接口：
  POST /chat           发送消息，返回 SSE 事件流
  POST /interrupt      响应权限确认（yes/no/compress/skip）
  GET  /sessions       列出历史会话
  POST /sessions/new   创建新会话
  POST /sessions/{id}/restore  恢复会话
  GET  /health         健康检查

SSE 事件格式（每行 data: <json>\n\n）：
  { "type": "token",      "text": "..." }
  { "type": "tool_start", "name": "...", "args": {...} }
  { "type": "tool_end",   "name": "...", "result": "...", "diff": {...} }
  { "type": "interrupt",  "message": "...", "kind": "permission|compress" }
  { "type": "done",       "input_tokens": N, "output_tokens": N }
  { "type": "error",      "message": "..." }

启动：
  python server.py
  或配合 run.ps1：$env:... ; python server.py
"""

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

# ── 应用初始化 ────────────────────────────────────────────────────────────────

_app_state: dict = {}   # 存放 graph、model、checkpointer


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时初始化模型和 graph。"""
    model_name = os.environ.get("MODEL_NAME", "qwen2.5-coder-32b-instruct")
    api_key    = os.environ.get("OPENAI_API_KEY", "sk-dummy")
    base_url   = os.environ.get("OPENAI_BASE_URL")

    try:
        from langchain_openai import ChatOpenAI
        model = ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            streaming=True,
        )
    except ImportError:
        print("[server] langchain-openai 未安装", file=sys.stderr)
        sys.exit(1)

    from graph.agent import build_graph
    from features.session import get_checkpointer

    checkpointer = get_checkpointer()
    builder      = build_graph(model, max_turns=100)
    graph        = builder.compile(checkpointer=checkpointer)

    _app_state["model"]       = model
    _app_state["graph"]       = graph
    _app_state["checkpointer"] = checkpointer

    print(f"[server] 启动成功，模型：{model_name}，监听 :8010", file=sys.stderr)
    yield


app = FastAPI(title="qwen-coder server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str
    permission_mode: str = "default"


class InterruptRequest(BaseModel):
    session_id: str
    answer: str   # "yes" | "no" | "compress" | "skip"


class NewSessionRequest(BaseModel):
    permission_mode: str = "default"


# ── 会话级 interrupt 队列（session_id → asyncio.Queue）────────────────────────

_interrupt_queues: dict[str, asyncio.Queue] = {}


def _get_interrupt_queue(session_id: str) -> asyncio.Queue:
    if session_id not in _interrupt_queues:
        _interrupt_queues[session_id] = asyncio.Queue()
    return _interrupt_queues[session_id]


# ── SSE 帮助函数 ──────────────────────────────────────────────────────────────

def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── 核心：将 astream_events 转为 SSE ─────────────────────────────────────────

async def _stream_events(
    graph: Any,
    config: dict,
    input_data: Any,
    session_id: str,
) -> AsyncIterator[str]:
    """
    消费 graph.astream_events，把每类事件序列化成 SSE 字符串 yield 出去。
    遇到 interrupt() 时推送 interrupt 事件，然后等待前端通过 /interrupt 接口回复。
    """
    _pending_tool: dict = {}
    _in_llm = False
    input_tokens  = 0
    output_tokens = 0

    try:
        async for event in graph.astream_events(input_data, config, version="v2"):
            etype = event.get("event", "")
            name  = event.get("name", "")
            data  = event.get("data", {})

            if etype == "on_chat_model_stream":
                _in_llm = True
                chunk = data.get("chunk")
                text  = getattr(chunk, "content", "") if chunk else ""
                if isinstance(text, str) and text:
                    yield _sse({"type": "token", "text": text})

            elif etype == "on_chat_model_end":
                if _in_llm:
                    _in_llm = False
                # 提取 token 用量
                output = data.get("output")
                if output:
                    usage = getattr(output, "usage_metadata", None) or {}
                    input_tokens  += usage.get("input_tokens", 0)
                    output_tokens += usage.get("output_tokens", 0)

            elif etype == "on_tool_start":
                args = data.get("input", {}) or {}
                if not isinstance(args, dict):
                    try:
                        args = json.loads(str(args))
                    except Exception:
                        args = {}
                yield _sse({"type": "tool_start", "name": name, "args": args})
                if name == "edit_file":
                    _pending_tool = {
                        "path": args.get("path", ""),
                        "old":  args.get("old_string", ""),
                        "new":  args.get("new_string", ""),
                    }

            elif etype == "on_tool_end":
                result = str(data.get("output", ""))
                diff   = None
                if name == "edit_file" and _pending_tool:
                    diff = {
                        "path": _pending_tool["path"],
                        "old":  _pending_tool["old"],
                        "new":  _pending_tool["new"],
                    }
                    _pending_tool = {}
                yield _sse({"type": "tool_end", "name": name, "result": result[:500], "diff": diff})

    except Exception as exc:
        # interrupt() 抛出的是 GraphInterrupt，需要特殊处理
        exc_type = type(exc).__name__
        if "Interrupt" in exc_type or "interrupt" in exc_type.lower():
            pass   # 走下面的 interrupt 处理
        else:
            yield _sse({"type": "error", "message": str(exc)})
            return

    # ── 检查是否有 interrupt() 暂停 ──────────────────────────────────────────
    snap = await graph.aget_state(config)
    while snap.tasks:
        interrupts = []
        for task in snap.tasks:
            interrupts.extend(getattr(task, "interrupts", []))
        if not interrupts:
            break

        for intr in interrupts:
            msg      = intr.value if hasattr(intr, "value") else str(intr)
            msg_str  = str(msg)
            kind     = "compress" if any(k in msg_str for k in ("压缩", "compress", "上下文")) else "permission"
            yield _sse({"type": "interrupt", "message": msg_str, "kind": kind})

        # 等待前端通过 /interrupt 接口回复
        queue  = _get_interrupt_queue(session_id)
        answer = await asyncio.wait_for(queue.get(), timeout=300.0)

        # resume graph
        async for chunk in _stream_events(graph, config, Command(resume=answer), session_id):
            yield chunk
        return

    # ── 存档 ─────────────────────────────────────────────────────────────────
    try:
        from features.session import save_turn
        await save_turn(graph, config, session_id)
    except Exception:
        pass

    yield _sse({"type": "done", "input_tokens": input_tokens, "output_tokens": output_tokens})


# ── 路由 ──────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "model": os.environ.get("MODEL_NAME", "unknown")}


@app.post("/chat")
async def chat(req: ChatRequest):
    graph  = _app_state["graph"]
    config = _make_config(req.session_id)

    input_data = {"messages": [HumanMessage(content=req.message)]}

    return StreamingResponse(
        _stream_events(graph, config, input_data, req.session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/interrupt")
async def interrupt_reply(req: InterruptRequest):
    queue = _get_interrupt_queue(req.session_id)
    await queue.put(req.answer)
    return {"ok": True}


@app.get("/sessions")
def list_sessions_route():
    from features.session import list_sessions
    return list_sessions()


@app.post("/sessions/new")
async def new_session(req: NewSessionRequest):
    from graph.agent import make_initial_state
    from features.session import new_session_id, save_session, make_thread_config

    sid    = new_session_id()
    config = make_thread_config(sid)
    graph  = _app_state["graph"]

    await graph.aupdate_state(config, make_initial_state(req.permission_mode))
    save_session(sid)
    return {"session_id": sid}


@app.post("/sessions/{session_id}/restore")
async def restore_session_route(session_id: str):
    from features.session import restore_session, make_thread_config, load_session

    if not load_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")

    graph  = _app_state["graph"]
    config = make_thread_config(session_id)
    await restore_session(graph, config, session_id)
    return {"ok": True, "session_id": session_id}


# ── 帮助函数 ──────────────────────────────────────────────────────────────────

def _make_config(session_id: str) -> dict:
    from features.session import make_thread_config
    return make_thread_config(session_id)


# ── 启动入口 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8010, log_level="info")
