from __future__ import annotations

import json
from dataclasses import dataclass

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from munshi.safety.auth import MunshiState
from munshi.safety.middleware import build_hitl_middleware, build_role_gated_middleware


@dataclass
class AgentBundle:
    name: str
    title: str
    agent: object
    checkpointer: object
    role_tools: dict[str, list[str]]


HOUSE_RULES = (
    " House rules: today is {today}. Use the tools for every fact — never invent an ID, a balance or a stock figure. "
    "IDs look like C-001 (customer), S-001 (supplier), UREA-50 (product), ORD-XXXXXXXX (order), DSP-XXXXXXXX (plan), STP-XXXXXXXX (stop), REM-XXXXXXXX (reminder). "
    "Amounts are in Pakistani rupees. Reply in the user's language (Urdu, English or mixed), in one or two short sentences. "
    "Never write a message to a customer yourself; only the templated reminders, codes and receipts go out."
)

# Added for a real model only (the offline rules never read prompts beyond their role predicates).
MODEL_RULES = (
    " How to work: users name customers, suppliers and products the way they speak — 'Malik Seeds', 'چوہدری فارمز', 'haji sons', 'makai', 'dap'. "
    "Look each one up yourself with find_customer / find_supplier / search_products, passing the name exactly as the user wrote it, and use the ID that comes back. "
    "When a system note says what code read in the message (a customer = its ID, the items), use exactly those -- no lookup needed; if it says a name is "
    "ambiguous, ask which of the candidates it lists. "
    "NEVER ask the user for an ID or a SKU and never guess one.If a lookup says `ambiguous`, ask which one, naming the candidates it returned; if it finds nothing, ask for the name again. "
    "Copy quantities and amounts from the message as the user wrote them ('50 hazar' = 50000, 'dedh sau' = 150); never work out a number the user didn't say. "
    "YOUR WORDS ARE NOT SHOWN TO THE USER. The user sees what CODE writes from your tool results: to answer a question, call the read "
    "tool(s) that hold the answer (khata, stock, orders, stops, aging, collections, cashbook, reports) and stop -- do not summarise them. "
    "To act, call the action tool: the user then sees the approval card code builds from it. Spend your effort on choosing the right "
    "tool and arguments. The ONLY text of yours that is shown is a short clarifying question when something is missing: ask it with no "
    "numbers, amounts, dates or IDs, and never ask the user for an ID, SKU or code (ask for a name). "
    "Never say anything was recorded, noted, sent or is waiting for approval -- code says that. "
    "Language of a question: the SAME script as the user's message. Roman Urdu (Urdu written in English letters, e.g. 'Rana ko 10 urea bhej do') "
    "gets Roman Urdu in English letters -- never Urdu script. Urdu script gets Urdu script. English gets English."
)


class TextToolResults(AgentMiddleware):
    """A tool result always goes back to the model as text. A tool that returns an empty list otherwise becomes a
    ToolMessage with content [] -- a valid LangChain content list, but OpenAI-compatible APIs (Groq) reject it with
    a 400 ('messages.N.content must be a string or a non-empty array'), which ended the turn."""

    def wrap_tool_call(self, request, handler):
        out = handler(request)
        if isinstance(out, ToolMessage) and not isinstance(out.content, str):
            try:
                text = json.dumps(out.content, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                text = str(out.content)
            out = out.model_copy(update={"content": text})
        return out


def is_real_model(model) -> bool:
    from munshi.llm.stub_model import StubToolCallingModel
    return model is not None and not isinstance(model, StubToolCallingModel)


def build_specialist(name: str, title: str, model: BaseChatModel, role_tool_map: dict[str, list[BaseTool]], role_prompt_map: dict[str, str],
                     checkpointer=None, repo=None, guarded: bool | None = None) -> AgentBundle:
    """A specialist = one create_agent graph with role-gated tools/prompt and
    HITL middleware generated from the risk registry. `checkpointer` is shared
    across specialists (thread ids are namespaced by the platform); None means
    an in-memory saver, fine for tests and the offline demo.

    With a real model (anything but the offline stub) and a `repo`, the graph also gets
    agents.guard.EntityGuard -- code checks every tool call's customer, supplier, items,
    amounts and references against the user's message before the approval gate sees it --
    and the model-only working rules (look names up, never ask for an ID, answer in the
    user's script). The guard is listed after the approval middleware so its after_model
    hook runs before the gate's. `guarded` forces the guard on or off (default: on for a real model)."""
    from munshi.domain.models import business_today
    all_tools = list({t.name: t for ts in role_tool_map.values() for t in ts}.values())
    real = is_real_model(model)
    rules = HOUSE_RULES.format(today=business_today().isoformat()) + (MODEL_RULES if real else "")
    role_prompt_map = {r: p + rules for r, p in role_prompt_map.items()}
    gate, prompt = build_role_gated_middleware(role_tool_map, role_prompt_map)
    cp = checkpointer or InMemorySaver()
    middleware = [gate, prompt, build_hitl_middleware()] + ([TextToolResults()] if real else [])
    if (real if guarded is None else guarded) and repo is not None:
        from munshi.agents.guard import EntityGuard
        middleware.append(EntityGuard(repo))
    agent = create_agent(model, tools=all_tools, state_schema=MunshiState, middleware=middleware, checkpointer=cp)
    return AgentBundle(name, title, agent, cp, {r: [t.name for t in ts] for r, ts in role_tool_map.items()})
