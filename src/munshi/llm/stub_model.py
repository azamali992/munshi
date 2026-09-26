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


# The request was understood, but the tool it needs isn't this role's (a clerk asking for a write-off, a salesman for a
# payment): the reply is marked response_metadata[UNBOUND_KEY] = {"tool", "extra"} so the platform can pass the request
# to the role that may do it (platform._handoff) instead of ending on "ask the owner".
UNBOUND_KEY = "munshi_unbound"


class NeedsRole(str):
    """A fallback reply for a request this role can't make itself: `tool` is what it needs, `extra` a sentence on what to
    do next (e.g. 'then send the right amount'). The stub marks it with UNBOUND_KEY."""

    tool: str
    extra: str

    def __new__(cls, text: str, tool: str, extra: str = ""):
        s = super().__new__(cls, text)
        s.tool, s.extra = tool, extra
        return s


def unbound_of(msg: Any) -> dict | None:
    """{"tool", "extra"} when `msg` is the stub's reply to a request the role has no tool for; else None."""
    return (getattr(msg, "response_metadata", None) or {}).get(UNBOUND_KEY) if isinstance(msg, AIMessage) else None


# A QUESTION never raises a write card unless it also carries an explicit instruction for it ('pakka? balance kitna reh
# jayega' is a balance question, not 'confirm the order'; 'scene kya he wasooli ka' is not 'draft every reminder').
_QUESTION = re.compile(r"[?؟]|\b(kya|kia|kiya hua|koi|kitna|kitni|kitne|kaun|kon|kaunsa|konsa|kaise|kaisa|kab|kahan|kidhar|kyun|kyon|scene|haal|halat|"
                       r"status|how much|how many|what|which|who|when|why|whether|is it|are they|has it|did)\b|کتنا|کتنی|کتنے|کون|کب|کہاں|کیوں")
_IMPERATIVE = re.compile(r"\b(kar do|kardo|kr do|karo|krdo|kar dein|kar den|kar dijiye|karwa do|karwao|bana do|banao|bana dein|bhej\w*|"
                         r"bhijwa\w*|de do|dedo|de dein|de den|de sakte|likh do|likho|daal do|dalo|nikal do|nikalo|chahiye|chahiyen|chaiye|please|plz|pls|"
                         r"record|recorded|received|confirm|cancel|approve|allocate|reverse|dispatch|send|book|pay|paid|transfer|draft|remind|"
                         r"diye|diya|di|dia|jama|aaye|aaya|aayi|aya|ayi|aye|mile|mila|liye|liya|li|wapis|utar\w*)\b"
                         r"|کر دو|کرو|بھیج|دے دو|دیے|دیا|چاہیے", re.I)


def question_only(text: str) -> bool:
    """A message shaped as a question, with no explicit instruction or report of money / goods moving in it."""
    f = fold(text)
    return bool(_QUESTION.search(f)) and not _IMPERATIVE.search(f)


def not_understood(msg: Any) -> bool:
    """True if `msg` is a stub reply carrying the explicit didn't-understand outcome."""
    return isinstance(msg, AIMessage) and (getattr(msg, "response_metadata", None) or {}).get(OUTCOME_KEY) == NOT_UNDERSTOOD


def _didnt(text: str) -> AIMessage:
    return AIMessage(content=str(text), response_metadata={OUTCOME_KEY: NOT_UNDERSTOOD})


# A clarifying question for ONE missing piece (llm.replies.Ask) is marked response_metadata[ASK_KEY] =
# {"slot", "candidates"}: the platform keeps it as the thread's open question (see agents/followup.py).
ASK_KEY = "munshi_ask"


def ask_meta(reply: Any) -> dict | None:
    slot = getattr(reply, "slot", None)
    return {"slot": slot, "candidates": list(getattr(reply, "candidates", None) or [])} if slot else None


def ask_of(msg: Any) -> dict | None:
    """The open question a message asks ({"slot", "candidates"}), or None."""
    return (getattr(msg, "response_metadata", None) or {}).get(ASK_KEY) if isinstance(msg, AIMessage) else None


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

    @staticmethod
    def _writes(tool: str) -> bool:
        from munshi.safety.risk import RiskTier, risk_of
        try:
            return risk_of(tool) != RiskTier.READ_ONLY
        except ValueError:
            return False            # not a business tool (the manager's route_to_* tools): routing is never suppressed

    def _decide(self, messages: list[BaseMessage]) -> AIMessage:
        text = self._latest_human_text(messages)
        intent_unbound = None
        asking = question_only(text)
        for rule in self.rules:
            try:
                if not rule.match(text):
                    continue
                if asking and self._writes(rule.tool_name):
                    continue                        # a question never raises a write card (see question_only)
                if rule.tool_name in self._bound_tool_names:
                    tool_call = {"name": rule.tool_name, "args": rule.args(text), "id": f"call_{rule.tool_name}"}
                    return AIMessage(content="", tool_calls=[tool_call])
            except Exception:                       # a parsing bug must never become a crash or a guess
                log.exception("stub rule %s failed on %r", rule.tool_name, text[:80])
                continue
            intent_unbound = rule.tool_name
            break   # the intent is clear but this role has no such tool: answer from the role's prompt, like a real model would

        system_text = "\n".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
        early = None
        if intent_unbound and self.fallback_fn is not None:
            try:
                early = self.fallback_fn(text, system_text)       # it may say more about who can do it (NeedsRole)
            except Exception:
                log.exception("stub fallback failed on %r", text[:80])
            if isinstance(early, NeedsRole):
                return AIMessage(content=str(early), response_metadata={UNBOUND_KEY: {"tool": early.tool, "extra": early.extra}})
        if intent_unbound or not self._bound_tool_names:
            for predicate, reply in self.prompt_fallbacks:
                if predicate(system_text):
                    return AIMessage(content=reply, response_metadata={UNBOUND_KEY: {"tool": intent_unbound, "extra": ""}} if intent_unbound else {})
        if self.fallback_fn is not None:
            try:
                reply = early if early is not None else self.fallback_fn(text, system_text)
            except Exception:
                log.exception("stub fallback failed on %r", text[:80])
                reply = None
            if reply:
                if isinstance(reply, NeedsRole):
                    return AIMessage(content=str(reply), response_metadata={UNBOUND_KEY: {"tool": reply.tool, "extra": reply.extra}})
                if isinstance(reply, NotUnderstood):
                    return _didnt(reply)
                am = ask_meta(reply)
                return AIMessage(content=str(reply), response_metadata={ASK_KEY: am} if am else {})
        # nothing matched and nothing specific to ask: the generic capability line is a "didn't understand"
        return _didnt(self.fallback_text)
