from typing import Annotated, List, Set
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    # ── 对话历史 ──────────────────────────────────────────────────────────────
    # add_messages reducer 自动处理追加，checkpoint 自动持久化
    messages: Annotated[List[BaseMessage], add_messages]

    # ── 权限系统 ──────────────────────────────────────────────────────────────
    # 当前权限模式，可由 enter_plan_mode / exit_plan_mode 工具动态修改
    # 取值：default | plan | acceptEdits | bypassPermissions | dontAsk
    permission_mode: str

    # enter_plan_mode 前保存的模式，exit_plan_mode 时恢复
    pre_plan_mode: str

    # 本会话已批准的危险路径，避免对同一路径重复弹确认
    confirmed_paths: Set[str]

    # ── Token 统计（费用 + 压缩触发）────────────────────────────────────────
    total_input_tokens: int
    total_output_tokens: int

    # 上一轮 API 调用的输入 token 数，用于计算 context 使用率
    last_input_token_count: int

    # 上一次 API 调用的时间戳（Unix time），用于 micro-compact 的 5 分钟空闲检测
    last_api_call_time: float

    # ── 循环控制 ──────────────────────────────────────────────────────────────
    # 当前已执行的 agentic turns，对比 max_turns 配置
    current_turns: int

    # ── 上下文压缩 ────────────────────────────────────────────────────────────
    # warn 节点 interrupt() 后用户的选择，路由函数据此决定下一节点
    # 取值："compress" | "skip" | ""
    compress_choice: str

    # ── 记忆系统 ──────────────────────────────────────────────────────────────
    surfaced_memories: Set[str]
    session_memory_bytes: int
