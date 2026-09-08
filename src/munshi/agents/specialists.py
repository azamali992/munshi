"""The five specialist munshis. Each is built from the shared factory with
its own role->tools map, role->prompt map, and — for offline runs — its own
deterministic rule set. A real LLM (LLM_PROVIDER=groq) gets the same tools
and prompts and does the language work itself."""
from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from munshi.agents.factory import AgentBundle, build_specialist
from munshi.domain.repository import MunshiRepository
from munshi.llm.parse import date_in, ids_in, int_in, money_in, parse_customer, parse_items
from munshi.llm.stub_model import Rule, StubToolCallingModel, contains
from munshi.tools.core import MunshiTools
from munshi.tools.langchain_tools import build_tools

BUSINESS = "Sultan Traders, an agri-input distributor"


def _model(model, rules, fallback, prompt_fallbacks=()):
    return model or StubToolCallingModel(rules=rules, fallback_text=fallback, prompt_fallbacks=list(prompt_fallbacks))


# ------------------------------------------------------------------ Order
def build_order_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None) -> AgentBundle:
    T = build_tools(ops)
    read = [T["find_customer"], T["get_customer_khata"], T["search_products"], T["get_stock"], T["get_order"], T["list_orders"]]
    act = read + [T["create_order"], T["confirm_order"]]
    prompts = {
        "owner": f"You are the Order Munshi for {BUSINESS}. Take orders from chat in Urdu, English or mixed. Identify the customer, match items to real SKUs, check stock and the customer's khata, then create a draft order. Never confirm an order without the customer's yes.",
        "clerk": f"You are the Order Munshi for {BUSINESS}. Take orders from chat, match to real SKUs, create drafts. Confirm only when the customer has said yes.",
        "driver": "You are the Order Munshi. Drivers cannot place orders; tell them to pass the request to the office.",
    }
    rules = [
        Rule(contains("confirm", "haan", "yes", "ok"), "confirm_order",
             lambda t: {"order_id": (ids_in(t, "ORD") or [""])[0]}),
        Rule(contains("khata", "balance", "outstanding", "baqi", "udhaar"), "get_customer_khata",
             lambda t: {"customer_id": parse_customer(t, repo) or ""}),
        Rule(contains("stock", "available", "hai", "kitna"), "get_stock",
             lambda t: {"sku": (parse_items(t + " 1 ", repo) or [{"sku": ""}])[0]["sku"] if parse_items("1 " + t, repo) else _sku_in(t, repo)}),
        Rule(lambda t: bool(parse_items(t, repo)) and bool(parse_customer(t, repo)), "create_order",
             lambda t: {"customer_id": parse_customer(t, repo), "items": parse_items(t, repo), "source_text": t}),
        Rule(lambda t: bool(parse_customer(t, repo)), "find_customer", lambda t: {"text": parse_customer(t, repo)}),
    ]
    m = _model(model, rules, "Tell me the customer and the items, e.g. 'Chaudhry Farms ko 20 urea aur 5 dap'.",
               [(lambda p: "cannot place orders" in p, "Drivers can't place orders — please pass this to the office.")])
    return build_specialist("order", "Order Munshi", m, {"owner": act, "clerk": act, "driver": []}, prompts)


def _sku_in(text: str, repo: MunshiRepository) -> str:
    t = text.lower()
    for p in repo.list_products():
        if any(k.lower() in t for k in [p.sku, p.name, *p.aliases] if len(k) > 2):
            return p.sku
    return ""


# ------------------------------------------------------------------ Godown
def build_godown_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None) -> AgentBundle:
    T = build_tools(ops)
    read = [T["get_stock"], T["list_orders"], T["get_order"], T["list_routes"], T["list_vehicles"], T["suggest_dispatch"], T["get_plan"]]
    clerk = read + [T["allocate_order"], T["create_dispatch_plan"], T["approve_dispatch_plan"]]
    owner = clerk + [T["adjust_stock"]]
    prompts = {
        "owner": f"You are the Godown Munshi for {BUSINESS}. Allocate confirmed orders against real stock, plan dispatch onto routes and vehicles within capacity, and adjust stock only with a reason. Never promise stock that isn't available.",
        "clerk": f"You are the Godown Munshi for {BUSINESS}. Allocate confirmed orders and plan dispatch. Stock adjustments need the owner.",
        "driver": "You are the Godown Munshi. Drivers can view their plan but not change allocation.",
    }
    rules = [
        Rule(contains("approve", "load", "manzoor"), "approve_dispatch_plan", lambda t: {"plan_id": (ids_in(t, "DSP") or [""])[0]}),
        Rule(contains("restock", "adjust", "write off", "write-off", "damaged", "received"), "adjust_stock",
             lambda t: {"warehouse_id": (ids_in(t, "WH") or ["WH-MULTAN"])[0], "sku": _sku_in(t, repo),
                        "delta": (-abs(int_in(t)) if any(w in t.lower() for w in ("write", "damaged", "reduce")) else abs(int_in(t))),
                        "reason": t[:80]}),
        Rule(lambda t: contains("allocate", "reserve")(t) and bool(ids_in(t, "ORD")), "allocate_order",
             lambda t: {"order_id": ids_in(t, "ORD")[0], "warehouse_id": (ids_in(t, "WH") or ["WH-MULTAN"])[0]}),
        Rule(lambda t: contains("dispatch", "plan", "bhejo", "gaari")(t) and bool(ids_in(t, "ORD")), "create_dispatch_plan",
             lambda t: {"route_id": (ids_in(t, "R") or [""])[0], "vehicle_id": (ids_in(t, "V") or ["V-01"])[0],
                        "order_ids": ids_in(t, "ORD"), "plan_date": date_in(t) or ""}),
        Rule(contains("dispatch", "plan", "suggest", "today", "aaj"), "suggest_dispatch", lambda t: {"plan_date": date_in(t) or ""}),
        Rule(contains("stock", "kitna", "available"), "get_stock", lambda t: {"sku": _sku_in(t, repo)}),
        Rule(contains("confirmed", "allocated", "orders"), "list_orders", lambda t: {"status": "confirmed" if "confirmed" in t.lower() else "allocated"}),
    ]
    m = _model(model, rules, "I can check stock, allocate confirmed orders, suggest or create a dispatch plan, and approve loading.",
               [(lambda p: "need the owner" in p, "Stock adjustments need the owner — ask them to approve.")])
    return build_specialist("godown", "Godown Munshi", m, {"owner": owner, "clerk": clerk, "driver": [T["get_plan"]]}, prompts)


# ------------------------------------------------------------------ Delivery
def build_delivery_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None) -> AgentBundle:
    T = build_tools(ops)
    tools = [T["get_plan"], T["list_stops"], T["get_order"], T["close_stop"]]
    prompts = {r: f"You are the Delivery Munshi for {BUSINESS}, on the driver's phone. Show the stops in order and close each one with what was delivered, what came back, cash taken, and the customer's OTP. Never close a stop without the OTP." for r in ("owner", "clerk", "driver")}
    import re

    def _close_args(t: str) -> dict:
        stop = (ids_in(t, "STP") or [""])[0]
        try:
            st = repo.get_stop(stop) if stop else None
            order = repo.get_order(st.order_id) if st else None
        except Exception:          # unknown stop: let the tool report it, don't crash the model
            st = order = None
        delivered = [{"sku": i.sku, "qty": i.qty} for i in order.items] if (order and "all" in t.lower()) else parse_items(t, repo)
        otp = re.search(r"otp\D*(\d{4})", t, re.I)
        return {"stop_id": stop, "delivered_items": delivered, "returned_items": [],
                "cash_collected": money_in(re.sub(r"otp\D*\d{4}", "", t, flags=re.I).replace(stop, "")), "otp": otp.group(1) if otp else ""}

    rules = [
        Rule(lambda t: contains("close", "delivered", "deliver")(t) and bool(ids_in(t, "STP")), "close_stop", _close_args),
        Rule(lambda t: bool(ids_in(t, "DSP")), "list_stops", lambda t: {"plan_id": ids_in(t, "DSP")[0]}),
    ]
    m = _model(model, rules, "Tell me the plan ID to see stops, or close a stop: 'close STP-XXXX delivered all, cash 50000, OTP 1234'.")
    return build_specialist("delivery", "Delivery Munshi", m, {"owner": tools, "clerk": tools, "driver": tools}, prompts)


# ------------------------------------------------------------------ Hisaab
def build_hisaab_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None) -> AgentBundle:
    T = build_tools(ops)
    read = [T["get_digest"], T["get_plan"], T["list_stops"], T["get_customer_khata"]]
    clerk = read + [T["record_deposit"]]
    owner = clerk + [T["credit_note"]]
    prompts = {
        "owner": f"You are the Hisaab Munshi for {BUSINESS}. Reconcile cash handed in against cash collected on each plan, attribute any shortfall to a stop, keep the khata honest, and issue credit notes only with a stated reason.",
        "clerk": f"You are the Hisaab Munshi for {BUSINESS}. Record deposits and reconcile. Credit notes need the owner.",
        "driver": "You are the Hisaab Munshi. Drivers hand cash to the cashier; they don't record deposits.",
    }
    rules = [
        Rule(contains("credit note", "credit", "refund", "waive"), "credit_note",
             lambda t: {"customer_id": parse_customer(t, repo) or "", "amount": money_in(t), "reason": t[:80]}),
        Rule(lambda t: contains("deposit", "handed", "counted", "cash", "jama")(t) and bool(ids_in(t, "DSP")), "record_deposit",
             lambda t: {"plan_id": ids_in(t, "DSP")[0], "amount_counted": money_in(t.replace(ids_in(t, "DSP")[0], "")), "counted_by": "cashier"}),
        Rule(contains("khata", "balance", "outstanding"), "get_customer_khata", lambda t: {"customer_id": parse_customer(t, repo) or ""}),
        Rule(contains("digest", "today", "summary", "close the day", "aaj"), "get_digest", lambda t: {}),
    ]
    m = _model(model, rules, "I can record a deposit against a plan, show a khata, or give today's digest.",
               [(lambda p: "need the owner" in p, "Credit notes need the owner's approval.")])
    return build_specialist("hisaab", "Hisaab Munshi", m, {"owner": owner, "clerk": clerk, "driver": []}, prompts)


# ------------------------------------------------------------------ Wasooli
def build_wasooli_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None) -> AgentBundle:
    T = build_tools(ops)
    tools = [T["aging_report"], T["get_customer_khata"], T["draft_reminder"], T["draft_due_reminders"], T["send_reminder"], T["log_promise"]]
    prompts = {
        "owner": f"You are the Wasooli Munshi for {BUSINESS}. Age the receivables, draft templated reminders in the right tone for how overdue each account is, log promises to pay, and escalate the ones that slip. Never write free text to a customer; only approved templates go out.",
        "clerk": f"You are the Wasooli Munshi for {BUSINESS}. Draft and send templated reminders after approval; log promises.",
        "driver": "You are the Wasooli Munshi. Drivers don't run collections.",
    }
    rules = [
        Rule(contains("promise", "wada", "will pay"), "log_promise",
             lambda t: {"customer_id": parse_customer(t, repo) or "", "amount": money_in(t), "promised_date": date_in(t) or ""}),
        Rule(lambda t: contains("send")(t) and bool(ids_in(t, "REM")), "send_reminder", lambda t: {"reminder_id": ids_in(t, "REM")[0]}),
        Rule(lambda t: contains("remind", "reminder", "yaad")(t) and bool(parse_customer(t, repo)), "draft_reminder",
             lambda t: {"customer_id": parse_customer(t, repo), "tier": next((x for x in ("gentle", "firm", "final") if x in t.lower()), "")}),
        Rule(contains("remind", "reminders", "collections", "chase", "wasooli"), "draft_due_reminders", lambda t: {"min_days_overdue": max(1, int_in(t) or 1)}),
        Rule(contains("aging", "overdue", "receivable", "who owes", "baqi"), "aging_report", lambda t: {}),
    ]
    m = _model(model, rules, "I can show aging, draft reminders for overdue accounts, send an approved reminder, or log a promise to pay.")
    return build_specialist("wasooli", "Wasooli Munshi", m, {"owner": tools, "clerk": tools, "driver": []}, prompts)
