"""A deterministic, rule-based chat model implementing LangChain's
BaseChatModel + tool-calling interface, so every agent in this platform can
be exercised end to end -- including the HumanInTheLoopMiddleware
interrupt/resume flow -- with zero network calls and zero API key. This is
what makes the whole test suite and CI run without needing a Groq key,
exactly like the stub LLM clients in this account's other two agent
projects (the author's other agent projects).

It is deliberately simple: one keyword-matched tool call per user turn,
then a one-line summary once the tool result comes back. That's enough to
exercise real control flow (tool selection, risk-gated approval,
role-based tool visibility, multi-agent delegation) without trying to fake
actual language understanding -- for that, point the platform at a real
model via llm/factory.py instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict


@dataclass
class Rule:
    """If `match(text)` is true for the latest human turn, call `tool_name`
    with `args(text)`."""

    match: Callable[[str], bool]
    tool_name: str
    args: Callable[[str], dict[str, Any]]


def contains(*keywords: str) -> Callable[[str], bool]:
    """Whole-word/whole-phrase matching, not raw substring -- "restock"
    must not accidentally match a rule for the keyword "stock" just
    because one contains the other's letters."""
    import re

    patterns = [re.compile(r"\b" + re.escape(k.lower()) + r"\b") for k in keywords]

    def _match(text: str) -> bool:
        lowered = text.lower()
        return any(p.search(lowered) for p in patterns)

    return _match


class StubToolCallingModel(BaseChatModel):
    """`rules` are tried in order against the most recent HumanMessage; the
    first match whose tool is currently bound wins. If nothing matches, the
    model falls back to the first entry in `prompt_fallbacks` whose
    predicate matches the current SystemMessage (this is how the stub
    reacts to dynamic_prompt-driven role messaging -- e.g. a billing
    request from a role with no billing tools bound can still surface a
    "this needs manager approval" reply, matched against the system prompt
    ops_platform.safety.middleware's role-gated prompt middleware set for
    that role -- rather than a generic non-answer). If nothing in
    `prompt_fallbacks` matches either, `fallback_text` is used."""

    rules: list[Rule] = []
    prompt_fallbacks: list[tuple[Callable[[str], bool], str]] = []
    fallback_text: str = "I'm not sure how to help with that."
    _bound_tool_names: list[str] = []

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "munshi-stub"

    def bind_tools(self, tools, **kwargs):
        # Return a bound COPY. A shared instance that mutates on bind would keep
        # the previous turn's tools when the framework skips binding for a role
        # with no tools at all -- which is exactly the case where it matters.
        bound = self.model_copy()
        bound._bound_tool_names = [getattr(t, "name", str(t)) for t in tools]
        return bound

    def _latest_human_text(self, messages: list[BaseMessage]) -> str:
        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                return str(m.content)
        return ""

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            content = str(last.content)
            if content.lstrip().startswith("{") and '"error"' in content[:40]:
                import json as _json
                try:
                    msg = AIMessage(content=f"Couldn't do that: {_json.loads(content)['error']}")
                except Exception:
                    msg = AIMessage(content=f"Couldn't do that: {content}")
            else:
                msg = AIMessage(content=f"Done -- {content}")
            return ChatResult(generations=[ChatGeneration(message=msg)])

        text = self._latest_human_text(messages)
        for rule in self.rules:
            if rule.tool_name in self._bound_tool_names and rule.match(text):
                tool_call = {"name": rule.tool_name, "args": rule.args(text), "id": f"call_{rule.tool_name}"}
                msg = AIMessage(content="", tool_calls=[tool_call])
                return ChatResult(generations=[ChatGeneration(message=msg)])

        system_text = "\n".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
        for predicate, reply in self.prompt_fallbacks:
            if predicate(system_text):
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.fallback_text))])
