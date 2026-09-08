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
    checkpointer: InMemorySaver


def build_specialist(name: str, title: str, model: BaseChatModel, role_tool_map: dict[str, list[BaseTool]], role_prompt_map: dict[str, str]) -> AgentBundle:
    all_tools = list({t.name: t for ts in role_tool_map.values() for t in ts}.values())
    gate, prompt = build_role_gated_middleware(role_tool_map, role_prompt_map)
    cp = InMemorySaver()
    agent = create_agent(model, tools=all_tools, state_schema=MunshiState,
                         middleware=[gate, prompt, build_hitl_middleware()], checkpointer=cp)
    return AgentBundle(name, title, agent, cp)
