"""Approval and role gating as middleware, generated from the risk registry."""
from __future__ import annotations

from typing import Callable

from langchain.agents.middleware import (HumanInTheLoopMiddleware, ModelRequest, ModelResponse,
                                         dynamic_prompt, wrap_model_call)
from langchain_core.tools import BaseTool

from munshi.safety.auth import DEFAULT_ROLE
from munshi.safety.risk import tools_requiring_approval


def build_hitl_middleware() -> HumanInTheLoopMiddleware:
    return HumanInTheLoopMiddleware(interrupt_on={n: True for n in tools_requiring_approval()})


def build_role_gated_middleware(role_tool_map: dict[str, list[BaseTool]], role_prompt_map: dict[str, str], default_role: str = DEFAULT_ROLE):
    @wrap_model_call
    def role_gated_tools(request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        role = request.state.get("role", default_role)
        return handler(request.override(tools=role_tool_map.get(role, role_tool_map[default_role])))

    @dynamic_prompt
    def role_gated_prompt(request: ModelRequest) -> str:
        role = request.state.get("role", default_role)
        return role_prompt_map.get(role, role_prompt_map[default_role])

    return role_gated_tools, role_gated_prompt
