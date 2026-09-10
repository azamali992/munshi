from __future__ import annotations

from dataclasses import dataclass

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
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
    "Amounts are in Pakistani rupees. Reply in the user's language (Urdu, English or mixed), in one or two short sentences, and say plainly when something needs another role's approval. "
    "Never write a message to a customer yourself; only the templated reminders, codes and receipts go out."
)


def build_specialist(name: str, title: str, model: BaseChatModel, role_tool_map: dict[str, list[BaseTool]], role_prompt_map: dict[str, str],
                     checkpointer=None) -> AgentBundle:
    """A specialist = one create_agent graph with role-gated tools/prompt and
    HITL middleware generated from the risk registry. `checkpointer` is shared
    across specialists (thread ids are namespaced by the platform); None means
    an in-memory saver, fine for tests and the offline demo."""
    from datetime import date
    all_tools = list({t.name: t for ts in role_tool_map.values() for t in ts}.values())
    role_prompt_map = {r: p + HOUSE_RULES.format(today=date.today().isoformat()) for r, p in role_prompt_map.items()}
    gate, prompt = build_role_gated_middleware(role_tool_map, role_prompt_map)
    cp = checkpointer or InMemorySaver()
    agent = create_agent(model, tools=all_tools, state_schema=MunshiState,
                         middleware=[gate, prompt, build_hitl_middleware()], checkpointer=cp)
    return AgentBundle(name, title, agent, cp, {r: [t.name for t in ts] for r, ts in role_tool_map.items()})
