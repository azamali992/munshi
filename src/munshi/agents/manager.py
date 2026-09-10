"""The Manager: routes each message to one specialist and never acts."""
from __future__ import annotations

from typing import Literal

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

from munshi.llm.stub_model import Rule, StubToolCallingModel, contains
from munshi.safety.auth import MunshiState

Specialist = Literal["order", "godown", "delivery", "hisaab", "khareed", "wasooli", "report"]


@tool
def route_to_order() -> str:
    """Route to the Order Munshi: taking, confirming, cancelling or looking up customer orders."""
    return "ROUTE:order"

@tool
def route_to_godown() -> str:
    """Route to the Godown Munshi: stock levels, allocation, dispatch planning, loading, transfers, adjustments."""
    return "ROUTE:godown"

@tool
def route_to_delivery() -> str:
    """Route to the Delivery Munshi: a driver's stops and closing deliveries with OTP."""
    return "ROUTE:delivery"

@tool
def route_to_hisaab() -> str:
    """Route to the Hisaab Munshi: driver cash deposits, payments received at the office, expenses, cashbook, khata, credit notes, today's digest."""
    return "ROUTE:hisaab"

@tool
def route_to_khareed() -> str:
    """Route to the Khareed Munshi: stock received from suppliers, supplier bills and payments, what we owe."""
    return "ROUTE:khareed"

@tool
def route_to_wasooli() -> str:
    """Route to the Wasooli Munshi: receivables aging, reminders, promises to pay."""
    return "ROUTE:wasooli"

@tool
def route_to_report() -> str:
    """Route to the Report Munshi: sales, profit, margin, collections, stock valuation, slow stock, top customers, a product's movement history."""
    return "ROUTE:report"


_ROUTES = [route_to_order, route_to_godown, route_to_delivery, route_to_hisaab, route_to_khareed, route_to_wasooli, route_to_report]

CLARIFY = "Is this about an order, the godown, a delivery, cash/khata, a supplier, collections, or a report?"


def _stub() -> StubToolCallingModel:
    return StubToolCallingModel(rules=[
        Rule(contains("credit note", "refund", "waive"), "route_to_hisaab", lambda t: {}),
        Rule(contains("restock", "write off", "write-off", "damaged", "transfer", "allocate", "dispatch", "approve dsp"), "route_to_godown", lambda t: {}),
        Rule(contains("sales report", "profit", "munafa", "margin", "valuation", "slow stock", "dead stock", "top customers", "collection report", "revenue", "bikri", "ledger", "movement"), "route_to_report", lambda t: {}),
        Rule(contains("supplier", "suppliers", "payable", "payables", "purchase", "bought", "khareed", "fauji", "engro", "arrived", "from"), "route_to_khareed", lambda t: {}),
        Rule(contains("remind", "reminder", "overdue", "aging", "collections", "promise", "wasooli", "who owes", "broken"), "route_to_wasooli", lambda t: {}),
        Rule(contains("deposit", "handed", "counted", "credit note", "refund", "digest", "close the day", "summary", "reconcile", "expense", "kharcha", "diesel", "fuel", "salary", "cashbook", "paid", "payment", "jazzcash", "easypaisa", "cheque", "received"), "route_to_hisaab", lambda t: {}),
        Rule(contains("stop", "stops", "delivered", "otp", "driver"), "route_to_delivery", lambda t: {}),
        Rule(contains("stock", "allocate", "dispatch", "plan", "load", "godown", "restock", "write off", "damaged", "approve dsp", "vehicle", "route", "transfer"), "route_to_godown", lambda t: {}),
        Rule(contains("order", "confirm", "cancel", "khata", "balance", "bhej", "chahiye", "want", "bags", "bori", "urea", "dap"), "route_to_order", lambda t: {}),
    ], fallback_text=CLARIFY)


def build_manager(model: BaseChatModel | None = None):
    return create_agent(model or _stub(), tools=_ROUTES, state_schema=MunshiState,
                        system_prompt="You are the Manager at a distribution business. Read the message and call exactly one route_to_* tool for the munshi who handles it. Never do the work yourself.")


def classify(manager, text: str, role: str = "clerk") -> Specialist | None:
    result = manager.invoke({"messages": [HumanMessage(text)], "role": role})
    for m in result["messages"]:
        if isinstance(m, ToolMessage) and isinstance(m.content, str) and m.content.startswith("ROUTE:"):
            return m.content.split(":", 1)[1]  # type: ignore[return-value]
    return None
