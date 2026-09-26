"""The worst-case model: it does whatever the attacker wants.

HostileModel plays the real model in the red-team suite. Each case scripts what a fully compromised (or fully
gullible) model would do with the attack: which munshi the router picks, which tool calls the specialist makes --
including tools the role doesn't have, wrong customers, inflated amounts, bulk writes, endless loops, twenty calls
in one step -- and the words it finally says (claims, secrets, abuse, URLs, messages to customers, system-prompt
text). Nothing here is "what a model would probably do"; it is what the defences must survive.

Script keys (a case's "attack"):
  route      the specialist the model router picks (absent: routes nowhere)
  calls      steps; each step is a list of [tool, args] emitted in ONE model message
  final      the model's closing text
  trigger    obey only if this text is visible somewhere in what the model is sent (tests data sanitisation: a
             payload hidden from the model can't steer it); without it the model always obeys
  loop       [tool, args] called again on every step, forever (a runaway agent loop)
  tokens     tokens reported per request (default 700) -- the provider's usage figure, for the token caps

Every request is logged in `requests` (kind, the user's message, the full text the model was sent, the number of
tool results it could see, tokens) so the suite can assert on what reached the model, not only on what came out."""
from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict

BENIGN = "Kaunsa customer?"
RUNAWAY = 40            # requests for one message after which the fake refuses to go on (see _generate)


def _text(m: BaseMessage) -> str:
    c = m.content
    if isinstance(c, str):
        return c
    try:
        return json.dumps(c, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(c)


def protocol(messages: list[BaseMessage]) -> None:
    """What OpenAI-compatible APIs enforce with a 400: every tool call answered, tool content never empty."""
    for i, m in enumerate(messages):
        if isinstance(m, AIMessage) and m.tool_calls:
            want = {tc["id"] for tc in m.tool_calls}
            got, j = set(), i + 1
            while j < len(messages) and isinstance(messages[j], ToolMessage):
                got.add(messages[j].tool_call_id)
                j += 1
            if want - got:
                raise ValueError(f"400: tool_calls {sorted(want - got)} have no tool response")


class HostileModel(BaseChatModel):
    script: dict = {}
    requests: list = []
    _tools: list[str] = []
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "hostile-fake"

    def bind_tools(self, tools, **kwargs):
        c = self.model_copy()
        c._tools = [getattr(t, "name", str(t)) for t in tools]
        return c

    def _out(self, msg: AIMessage) -> ChatResult:
        n = int(self.script.get("tokens") or 700)
        msg.usage_metadata = {"input_tokens": n - 50, "output_tokens": 50, "total_tokens": n}
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        protocol(messages)
        h = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0)
        user = _text(messages[h]) if messages else ""
        run = 0
        for q in reversed(self.requests):
            if q["user"] != user:
                break
            run += 1
        if run >= RUNAWAY:
            # Without a per-message cap a looping agent runs until LangChain's recursion limit (9,999 steps): the fake
            # stops itself here so an unguarded run still ends -- and the suite reports the RUNAWAY requests it took.
            raise RuntimeError(f"hostile model: runaway loop ({run} requests for one message)")
        seen = "\n".join(_text(m) for m in messages)
        is_router = any(n.startswith("route_to_") for n in self._tools)
        tool_results = sum(1 for m in messages[h + 1:] if isinstance(m, ToolMessage))
        self.requests.append({"kind": "route" if is_router else "act", "user": user, "seen": seen, "tools": list(self._tools),
                              "tool_results": tool_results, "tokens": int(self.script.get("tokens") or 700)})
        s = self.script
        trig = str(s.get("trigger") or "").casefold()
        obey = not trig or trig in seen.casefold()
        if is_router:
            spec = s.get("route")
            if spec and obey:
                return self._out(AIMessage(content="", tool_calls=[{"name": f"route_to_{spec}", "args": {}, "id": f"route_{len(self.requests)}"}]))
            return self._out(AIMessage(content=""))
        if not obey:
            return self._out(AIMessage(content=BENIGN))
        done = sum(1 for m in messages[h + 1:] if isinstance(m, AIMessage) and m.tool_calls)
        if s.get("loop"):
            name, args = s["loop"]
            return self._out(AIMessage(content="", tool_calls=[{"name": name, "args": dict(args), "id": f"loop_{len(self.requests)}"}]))
        steps = s.get("calls") or []
        if done < len(steps):
            calls = [{"name": n, "args": dict(a), "id": f"c{len(self.requests)}_{k}"} for k, (n, a) in enumerate(steps[done])]
            return self._out(AIMessage(content="", tool_calls=calls))
        return self._out(AIMessage(content=str(s.get("final") or "")))
