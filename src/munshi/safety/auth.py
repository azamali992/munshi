from __future__ import annotations

from langchain.agents import AgentState

DEFAULT_ROLE = "clerk"
ROLES = ("owner", "clerk", "salesman", "driver")


class MunshiState(AgentState):
    role: str
