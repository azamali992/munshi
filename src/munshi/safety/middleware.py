"""Approval and role gating as middleware, generated from the risk registry."""
from __future__ import annotations

import json
from typing import Any, Callable

from langchain.agents.middleware import HumanInTheLoopMiddleware, ModelRequest, ModelResponse, dynamic_prompt, wrap_model_call
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import interrupt

from munshi.safety.auth import DEFAULT_ROLE
from munshi.safety.risk import tools_requiring_approval

# Key in the interrupt payload listing gated calls that were dropped from the step (never run).
DEFERRED_KEY = "munshi_deferred"


class OneGatedCallPerStep(HumanInTheLoopMiddleware):
    """HumanInTheLoopMiddleware that puts at most ONE gated tool call per model
    step in front of a human.

    Stock HITL batches every gated call of a step into one interrupt and needs
    one decision per call on resume. The platform shows one approval card per
    action and resolves it with one decision, so a step with two gated calls
    could never be resumed. Rather than half-surfacing such a step, the extra
    gated calls are removed from the model's message before anything runs:
    they never execute, the model's message says so (so it can ask for them
    again, one at a time), and the interrupt payload lists them under
    DEFERRED_KEY so the platform can tell the human. Non-gated calls in the
    same step are untouched. A step with zero or one gated call behaves
    exactly like the stock middleware."""

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state["messages"]
        ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if ai is None or not ai.tool_calls:
            return None
        gated = [i for i, tc in enumerate(ai.tool_calls)
                 if (cfg := self.interrupt_on.get(tc["name"])) is not None and self._should_interrupt(tc, cfg, state, runtime)]
        if len(gated) <= 1:
            return super().after_model(state, runtime)

        first, extra = gated[0], set(gated[1:])
        call = ai.tool_calls[first]
        cfg = self.interrupt_on[call["name"]]
        deferred = [{"name": ai.tool_calls[i]["name"], "args": ai.tool_calls[i]["args"]} for i in gated[1:]]
        request = {
            "action_requests": [{"name": call["name"], "args": call["args"],
                                 "description": f"{self.description_prefix}\n\nTool: {call['name']}\nArgs: {call['args']}"}],
            "review_configs": [{"action_name": call["name"], "allowed_decisions": cfg["allowed_decisions"]}],
            DEFERRED_KEY: deferred,
        }
        decisions = interrupt(request)["decisions"]
        if len(decisions) != 1:
            raise ValueError(f"expected exactly one decision for {call['name']}, got {len(decisions)}")
        decision = decisions[0]
        if decision.get("type") not in ("approve", "reject") or decision["type"] not in cfg["allowed_decisions"]:
            raise ValueError(f"unsupported decision {decision.get('type')!r} for {call['name']}")

        note = ("[Not requested — only one action needing approval per step: "
                + "; ".join(f"{d['name']}({json.dumps(d['args'], default=str)})" for d in deferred)
                + ". Ask for these again one at a time, after the current one is decided.]")
        content: Any = ai.content
        content = (f"{content}\n{note}" if content else note) if isinstance(content, str) else [*content, {"type": "text", "text": note}]
        trimmed = ai.model_copy(update={"content": content, "tool_calls": [tc for i, tc in enumerate(ai.tool_calls) if i not in extra]})
        out: list[Any] = [trimmed]
        if decision["type"] == "reject":
            reason = decision.get("message")
            out.append(ToolMessage(content=f"User rejected the tool call for `{call['name']}`" + (f" with reason: {reason}" if reason else ". The tool was not executed."),
                                   name=call["name"], tool_call_id=call["id"], status="error"))
        return {"messages": out}


def build_hitl_middleware() -> HumanInTheLoopMiddleware:
    return OneGatedCallPerStep(interrupt_on={n: True for n in tools_requiring_approval()})


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
