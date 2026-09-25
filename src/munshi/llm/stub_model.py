"""A deterministic, rule-based chat model implementing LangChain's
BaseChatModel + tool-calling interface, so every agent in this platform can
be exercised end to end -- including the HumanInTheLoopMiddleware
interrupt/resume flow -- with zero network calls and zero API key. This is
what makes the whole test suite and CI run without needing a Groq key,
exactly like the stub LLM clients in this account's other two agent
projects (the author's other agent projects).

It is deliberately simple: one keyword-matched tool call per user turn,
then a one-line summary once the tool result comes back. The language work
it relies on (normalising Urdu script and Roman Urdu, reading spoken
numbers, resolving customers and products with a confidence rule) lives in
llm/text.py, llm/numbers.py, llm/resolve.py and llm/parse.py; when a rule
isn't sure it doesn't fire, and `fallback_fn` asks a question instead.
"""
from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict

from munshi.llm.text import fold

log = logging.getLogger("munshi.stub")

# The conversation the current turn belongs to, and a scratch cache for this one model call
# (rules and their argument builders parse the same text several times).
_HISTORY: ContextVar[list[BaseMessage] | None] = ContextVar("munshi_stub_history", default=None)
_TURN: ContextVar[dict | None] = ContextVar("munshi_stub_turn", default=None)

# Keys never echoed back into chat: the delivery code is the customer's, not the reader's.
_REDACT = {"otp"}

# The explicit "didn't understand" signal. A turn the rules could not read at all -- no rule fired and the
# fallback had no specific question or refusal to give -- ends in an AIMessage carrying
# response_metadata[OUTCOME_KEY] == NOT_UNDERSTOOD. A clarifying question ("Which customer -- ...?"), a refusal
# or a tool call never carries it. The platform uses it (and only it) to decide whether a real model may try.
OUTCOME_KEY = "munshi_outcome"
NOT_UNDERSTOOD = "not_understood"


class NotUnderstood(str):
    """A fallback reply that means "I didn't understand this message" (as opposed to a question or a refusal).
    Return one from a `fallback_fn` and the stub marks its message with the NOT_UNDERSTOOD outcome."""


def not_understood(msg: Any) -> bool:
    """True if `msg` is a stub reply carrying the explicit didn't-understand outcome."""
    return isinstance(msg, AIMessage) and (getattr(msg, "response_metadata", None) or {}).get(OUTCOME_KEY) == NOT_UNDERSTOOD


def _didnt(text: str) -> AIMessage:
    return AIMessage(content=str(text), response_metadata={OUTCOME_KEY: NOT_UNDERSTOOD})


def history() -> list[BaseMessage]:
    """The messages of the thread the current stub turn is answering (empty outside a turn)."""
    return list(_HISTORY.get() or [])


def turn_cache() -> dict:
    c = _TURN.get()
    return c if c is not None else {}


def memo(key: Any, fn: Callable[[], Any]) -> Any:
    c = turn_cache()
    if key not in c:
        c[key] = fn()
    return c[key]


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
    because one contains the other's letters. Both sides go through
    text.fold, so Urdu-script keywords match whichever keyboard typed them."""
    patterns = [re.compile(r"(?<!\w)" + re.escape(fold(k)) + r"(?!\w)") for k in keywords]

    def _match(text: str) -> bool:
        folded = fold(text)
        return any(p.search(folded) for p in patterns)

    return _match


def _redact(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _redact(v) for k, v in x.items() if k not in _REDACT}
    if isinstance(x, list):
        return [_redact(i) for i in x]
    return x


class StubToolCallingModel(BaseChatModel):
    """`rules` are tried in order against the most recent HumanMessage; the
    first match whose tool is currently bound wins. If a rule matched but its
    tool isn't bound for this role (or the role has no tools at all), the model
    answers from the first `prompt_fallbacks` entry whose predicate matches the
    current SystemMessage -- the role-gated prompt set by
    safety.middleware -- e.g. "Drivers can't place orders". Otherwise
    `fallback_fn(text, system_text)` may return a context-aware reply (a
    clarifying question naming the candidates, a refusal); if it returns None,
    `fallback_text` is used."""

    rules: list[Rule] = []
    prompt_fallbacks: list[tuple[Callable[[str], bool], str]] = []
    fallback_text: str = "I'm not sure how to help with that."
    fallback_fn: Callable[[str, str], str | None] | None = None
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

    @staticmethod
    def _summary(content: str) -> AIMessage:
        if content.lstrip().startswith("{") and '"error"' in content[:40]:
            try:
                return AIMessage(content=f"Couldn't do that: {json.loads(content)['error']}")
            except Exception:
                return AIMessage(content=f"Couldn't do that: {content}")
        try:
            data = json.loads(content)
            content = json.dumps(_redact(data), ensure_ascii=False)
        except (ValueError, TypeError):
            pass
        return AIMessage(content=f"Done -- {content}")

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            return ChatResult(generations=[ChatGeneration(message=self._summary(str(last.content)))])

        h_tok, t_tok = _HISTORY.set(list(messages)), _TURN.set({})
        try:
            return ChatResult(generations=[ChatGeneration(message=self._decide(messages))])
        finally:
            _HISTORY.reset(h_tok)
            _TURN.reset(t_tok)

    def _decide(self, messages: list[BaseMessage]) -> AIMessage:
        text = self._latest_human_text(messages)
        intent_unbound = False
        for rule in self.rules:
            try:
                if not rule.match(text):
                    continue
                if rule.tool_name in self._bound_tool_names:
                    tool_call = {"name": rule.tool_name, "args": rule.args(text), "id": f"call_{rule.tool_name}"}
                    return AIMessage(content="", tool_calls=[tool_call])
            except Exception:                       # a parsing bug must never become a crash or a guess
                log.exception("stub rule %s failed on %r", rule.tool_name, text[:80])
                continue
            intent_unbound = True
            break   # the intent is clear but this role has no such tool: answer from the role's prompt, like a real model would

        system_text = "\n".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
        if intent_unbound or not self._bound_tool_names:
            for predicate, reply in self.prompt_fallbacks:
                if predicate(system_text):
                    return AIMessage(content=reply)
        if self.fallback_fn is not None:
            try:
                reply = self.fallback_fn(text, system_text)
            except Exception:
                log.exception("stub fallback failed on %r", text[:80])
                reply = None
            if reply:
                return _didnt(reply) if isinstance(reply, NotUnderstood) else AIMessage(content=reply)
        # nothing matched and nothing specific to ask: the generic capability line is a "didn't understand"
        return _didnt(self.fallback_text)
