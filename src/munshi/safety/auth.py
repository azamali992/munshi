from __future__ import annotations
from langchain.agents import AgentState

DEFAULT_ROLE = "clerk"
ROLES = ("owner", "clerk", "driver")


class MunshiState(AgentState):
    role: str
