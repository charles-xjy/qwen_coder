from typing import Annotated, List, Set

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    # Conversation history
    messages: Annotated[List[BaseMessage], add_messages]

    # Permission system
    permission_mode: str
    pre_plan_mode: str
    confirmed_paths: Set[str]

    # Token stats
    total_input_tokens: int
    total_output_tokens: int
    last_input_token_count: int
    last_api_call_time: float

    # Loop control
    current_turns: int

    # Context compression
    compress_choice: str

    # Long-term memory
    surfaced_memories: Set[str]
    session_memory_bytes: int
