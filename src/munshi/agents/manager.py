"""The Manager: routes each message to one specialist and never acts."""
from __future__ import annotations
from typing import Literal
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from munshi.llm.stub_model import Rule, StubToolCallingModel, contains
from munshi.safety.auth import MunshiState

Specialist = Literal["order", "godown", "delivery", "hisaab", "wasooli"]


@tool
def route_to_order() -> str:
    """Route to the Order Munshi: taking, confirming or looking up customer orders."""
    return "ROUTE:order"

@tool
def route_to_godown() -> str:
    """Route to the Godown Munshi: stock, allocation, dispatch planning, loading."""
    return "ROUTE:godown"

@tool
def route_to_delivery() -> str:
    """Route to the Delivery Munshi: a driver's stops and closing deliveries with OTP."""
    return "ROUTE:delivery"

@tool
def route_to_hisaab() -> str:
    """Route to the Hisaab Munshi: cash deposits, reconciliation, khata, credit notes, today's digest."""
    return "ROUTE:hisaab"

@tool
def route_to_wasooli() -> str:
    """Route to the Wasooli Munshi: receivables aging, reminders, promises to pay."""
    return "ROUTE:wasooli"


_ROUTES = [route_to_order, route_to_godown, route_to_delivery, route_to_hisaab, route_to_wasooli]


def _stub() -> StubToolCallingModel:
    return StubToolCallingModel(rules=[
        Rule(contains("remind", "reminder", "overdue", "aging", "collections", "promise", "wasooli", "who owes"), "route_to_wasooli", lambda t: {}),
        Rule(contains("deposit", "handed", "counted", "credit note", "refund", "digest", "close the day", "summary", "reconcile"), "route_to_hisaab", lambda t: {}),
        Rule(contains("stop", "stops", "delivered", "otp", "driver"), "route_to_delivery", lambda t: {}),
        Rule(contains("stock", "allocate", "dispatch", "plan", "load", "godown", "restock", "write off", "damaged", "approve dsp", "vehicle", "route"), "route_to_godown", lambda t: {}),
        Rule(contains("order", "confirm", "khata", "balance", "bhej", "chahiye", "want", "bags", "bori", "urea", "dap"), "route_to_order", lambda t: {}),
    ], fallback_text="Is this about an order, the godown, a delivery, cash/khata, or collections?")


def build_manager(model: BaseChatModel | None = None):
    return create_agent(model or _stub(), tools=_ROUTES, state_schema=MunshiState)


def classify(manager, text: str, role: str = "clerk") -> Specialist | None:
    result = manager.invoke({"messages": [HumanMessage(text)], "role": role})
    for m in result["messages"]:
        if isinstance(m, ToolMessage) and isinstance(m.content, str) and m.content.startswith("ROUTE:"):
            return m.content.split(":", 1)[1]  # type: ignore[return-value]
    return None
