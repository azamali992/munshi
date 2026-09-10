"""The seven specialist munshis. Each is built from the shared factory with
its own role->tools map, role->prompt map, and — for offline runs — its own
deterministic rule set. A real LLM (LLM_PROVIDER=groq) gets the same tools
and prompts and does the language work itself.

Roles: owner, clerk, salesman, driver. The tool list bound for a role IS
that role's capability; there is no prompt-based refusal to bypass."""
from __future__ import annotations

import re

from langchain_core.language_models.chat_models import BaseChatModel

from munshi.agents.factory import AgentBundle, build_specialist
from munshi.domain.repository import MunshiRepository
from munshi.llm.parse import date_in, ids_in, int_in, method_in, money_in, parse_customer, parse_items, parse_supplier
from munshi.llm.stub_model import Rule, StubToolCallingModel, contains
from munshi.tools.core import MunshiTools
from munshi.tools.langchain_tools import build_tools

ROLES = ("owner", "clerk", "salesman", "driver")


def _model(model, rules, fallback, prompt_fallbacks=()):
    return model or StubToolCallingModel(rules=rules, fallback_text=fallback, prompt_fallbacks=list(prompt_fallbacks))


def _sku_in(text: str, repo: MunshiRepository) -> str:
    t = text.lower()
    for p in repo.list_products():
        if any(k.lower() in t for k in [p.sku, p.name, *p.aliases] if len(k) > 2):
            return p.sku
    return ""


def _biz(repo: MunshiRepository) -> str:
    return repo.business_name


# ------------------------------------------------------------------ Order
def build_order_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    read = [T["find_customer"], T["get_customer_khata"], T["search_products"], T["get_stock"], T["get_order"], T["list_orders"]]
    desk = read + [T["create_order"], T["confirm_order"], T["cancel_order"]]
    booker = read + [T["create_order"]]
    prompts = {
        "owner": f"You are the Order Munshi for {B}. Take orders from chat in Urdu, English or mixed. Identify the customer, match items to real SKUs, check stock and the customer's khata, then create a draft order. Never confirm an order without the customer's yes. Cancel only with a reason.",
        "clerk": f"You are the Order Munshi for {B}. Take orders from chat, match to real SKUs, create drafts. Confirm only when the customer has said yes.",
        "salesman": f"You are the Order Munshi for {B}, with a salesman on the route. Book the order as a draft (the office confirms it), and show the customer's khata and stock when asked. You cannot confirm or cancel orders.",
        "driver": "You are the Order Munshi. Drivers cannot place orders; tell them to pass the request to the office.",
    }
    rules = [
        Rule(contains("cancel", "mansookh"), "cancel_order", lambda t: {"order_id": (ids_in(t, "ORD") or [""])[0], "reason": t[:80]}),
        Rule(contains("confirm", "haan", "yes", "ok"), "confirm_order", lambda t: {"order_id": (ids_in(t, "ORD") or [""])[0]}),
        Rule(contains("khata", "balance", "outstanding", "baqi", "udhaar"), "get_customer_khata", lambda t: {"customer_id": parse_customer(t, repo) or ""}),
        Rule(contains("stock", "available", "hai", "kitna"), "get_stock", lambda t: {"sku": _sku_in(t, repo)}),
        Rule(lambda t: bool(parse_items(t, repo)) and bool(parse_customer(t, repo)), "create_order",
             lambda t: {"customer_id": parse_customer(t, repo), "items": parse_items(t, repo), "source_text": t}),
        Rule(lambda t: bool(parse_customer(t, repo)), "find_customer", lambda t: {"text": parse_customer(t, repo)}),
    ]
    m = _model(model, rules, "Tell me the customer and the items, e.g. 'Chaudhry Farms ko 20 urea aur 5 dap'.",
               [(lambda p: "cannot place orders" in p, "Drivers can't place orders — please pass this to the office."),
                (lambda p: "cannot confirm or cancel" in p, "Salesmen book drafts; the office confirms or cancels. Give me the customer and items to book.")])
    return build_specialist("order", "Order Munshi", m, {"owner": desk, "clerk": desk, "salesman": booker, "driver": []}, prompts, checkpointer)


# ------------------------------------------------------------------ Godown
def build_godown_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    read = [T["get_stock"], T["list_orders"], T["get_order"], T["list_routes"], T["list_vehicles"], T["suggest_dispatch"], T["get_plan"]]
    clerk = read + [T["allocate_order"], T["create_dispatch_plan"], T["approve_dispatch_plan"], T["transfer_stock"]]
    owner = clerk + [T["adjust_stock"]]
    prompts = {
        "owner": f"You are the Godown Munshi for {B}. Allocate confirmed orders against real stock, plan dispatch onto routes and vehicles within capacity, move stock between godowns, and adjust stock only with a reason. Never promise stock that isn't available.",
        "clerk": f"You are the Godown Munshi for {B}. Allocate confirmed orders, plan dispatch, transfer between godowns. Stock adjustments need the owner.",
        "salesman": "You are the Godown Munshi. Salesmen can check stock but not allocate or plan.",
        "driver": "You are the Godown Munshi. Drivers can view their plan but not change allocation.",
    }
    default_wh = repo.default_warehouse_id() if repo.list_warehouses() else ""
    rules = [
        Rule(contains("approve", "load", "manzoor"), "approve_dispatch_plan", lambda t: {"plan_id": (ids_in(t, "DSP") or [""])[0]}),
        Rule(contains("transfer", "move", "shift"), "transfer_stock",
             lambda t: {"from_warehouse": (ids_in(t, "WH") or [default_wh, ""])[0], "to_warehouse": (ids_in(t, "WH") + ["", ""])[1], "sku": _sku_in(t, repo), "qty": abs(int_in(t))}),
        Rule(contains("restock", "adjust", "write off", "write-off", "damaged", "received"), "adjust_stock",
             lambda t: {"warehouse_id": (ids_in(t, "WH") or [default_wh])[0], "sku": _sku_in(t, repo),
                        "delta": (-abs(int_in(t)) if any(w in t.lower() for w in ("write", "damaged", "reduce")) else abs(int_in(t))),
                        "reason": t[:80]}),
        Rule(lambda t: contains("allocate", "reserve")(t) and bool(ids_in(t, "ORD")), "allocate_order",
             lambda t: {"order_id": ids_in(t, "ORD")[0], "warehouse_id": (ids_in(t, "WH") or [default_wh])[0]}),
        Rule(lambda t: contains("dispatch", "plan", "bhejo", "gaari")(t) and bool(ids_in(t, "ORD")), "create_dispatch_plan",
             lambda t: {"route_id": (ids_in(t, "R") or [""])[0], "vehicle_id": (ids_in(t, "V") or [""])[0],
                        "order_ids": ids_in(t, "ORD"), "plan_date": date_in(t) or ""}),
        Rule(contains("dispatch", "plan", "suggest", "today", "aaj"), "suggest_dispatch", lambda t: {"plan_date": date_in(t) or ""}),
        Rule(contains("stock", "kitna", "available"), "get_stock", lambda t: {"sku": _sku_in(t, repo)}),
        Rule(contains("confirmed", "allocated", "orders"), "list_orders", lambda t: {"status": "confirmed" if "confirmed" in t.lower() else "allocated"}),
    ]
    m = _model(model, rules, "I can check stock, allocate confirmed orders, suggest or create a dispatch plan, transfer stock, and approve loading.",
               [(lambda p: "need the owner" in p, "Stock adjustments need the owner — ask them to approve."),
                (lambda p: "not allocate or plan" in p, "Salesmen can check stock; allocation and dispatch are the office's job.")])
    return build_specialist("godown", "Godown Munshi", m, {"owner": owner, "clerk": clerk, "salesman": [T["get_stock"]], "driver": [T["get_plan"]]}, prompts, checkpointer)


# ------------------------------------------------------------------ Delivery
def build_delivery_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    tools = [T["get_plan"], T["list_stops"], T["get_order"], T["close_stop"]]
    prompts = {r: f"You are the Delivery Munshi for {B}, on the driver's phone. Show the stops in order and close each one with what was delivered, what came back, cash taken, and the customer's OTP. Never close a stop without the OTP." for r in ROLES}

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
    return build_specialist("delivery", "Delivery Munshi", m, {"owner": tools, "clerk": tools, "salesman": [], "driver": tools}, prompts, checkpointer)


# ------------------------------------------------------------------ Hisaab
def build_hisaab_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    read = [T["get_digest"], T["get_plan"], T["list_stops"], T["get_customer_khata"], T["cashbook"]]
    clerk = read + [T["record_deposit"], T["record_payment"], T["record_expense"]]
    owner = clerk + [T["credit_note"]]
    prompts = {
        "owner": f"You are the Hisaab Munshi for {B}. Reconcile cash handed in against cash collected on each plan, attribute any shortfall to a stop, record payments received at the office (cash, bank, JazzCash, Easypaisa, cheque) and expenses, keep the khata honest, and issue credit notes only with a stated reason.",
        "clerk": f"You are the Hisaab Munshi for {B}. Record deposits, office payments and expenses; reconcile. Credit notes need the owner.",
        "salesman": "You are the Hisaab Munshi. Salesmen don't record money; they can ask the office.",
        "driver": "You are the Hisaab Munshi. Drivers hand cash to the cashier; they don't record deposits.",
    }
    rules = [
        Rule(contains("credit note", "credit", "refund", "waive"), "credit_note",
             lambda t: {"customer_id": parse_customer(t, repo) or "", "amount": money_in(t), "reason": t[:80]}),
        Rule(lambda t: contains("deposit", "handed", "counted", "jama")(t) and bool(ids_in(t, "DSP")), "record_deposit",
             lambda t: {"plan_id": ids_in(t, "DSP")[0], "amount_counted": money_in(t.replace(ids_in(t, "DSP")[0], "")), "counted_by": "cashier"}),
        Rule(contains("expense", "kharcha", "diesel", "petrol", "fuel", "salary", "tankhwah", "rent", "bijli", "repair"), "record_expense",
             lambda t: {"category": next((c for c, words in (("fuel", ("diesel", "petrol", "fuel")), ("salary", ("salary", "tankhwah")), ("rent", ("rent",)), ("utilities", ("bijli", "electric", "gas bill")), ("repair", ("repair", "tyre", "tire"))) if any(w in t.lower() for w in words)), "misc"),
                        "amount": money_in(t), "note": t[:80], "method": method_in(t)}),
        Rule(lambda t: contains("paid", "payment", "received", "diye", "jama", "transfer", "jazzcash", "easypaisa", "cheque")(t) and bool(parse_customer(t, repo)), "record_payment",
             lambda t: {"customer_id": parse_customer(t, repo) or "", "amount": money_in(t), "method": method_in(t), "ref": t[:60]}),
        Rule(contains("cashbook", "cash book", "cash today", "rokar"), "cashbook", lambda t: {"day": date_in(t) or ""}),
        Rule(contains("khata", "balance", "outstanding"), "get_customer_khata", lambda t: {"customer_id": parse_customer(t, repo) or ""}),
        Rule(contains("digest", "today", "summary", "close the day", "aaj"), "get_digest", lambda t: {}),
    ]
    m = _model(model, rules, "I can record a deposit against a plan, a payment received at the office, an expense, show a khata or the cashbook, or give today's digest.",
               [(lambda p: "need the owner" in p, "Credit notes need the owner's approval."),
                (lambda p: "don't record money" in p, "Payments are recorded by the office — tell the clerk.")])
    return build_specialist("hisaab", "Hisaab Munshi", m, {"owner": owner, "clerk": clerk, "salesman": [], "driver": []}, prompts, checkpointer)


# ------------------------------------------------------------------ Khareed (purchases)
def build_khareed_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    read = [T["find_supplier"], T["list_suppliers"], T["supplier_khata"], T["payables_report"], T["get_stock"], T["search_products"]]
    clerk = read + [T["record_purchase"]]
    owner = clerk + [T["pay_supplier"]]
    prompts = {
        "owner": f"You are the Khareed Munshi for {B}, in charge of buying. Receive stock from suppliers into the godown with the bill on their account, track what we owe each supplier, and pay suppliers only with a stated method and reference.",
        "clerk": f"You are the Khareed Munshi for {B}. Record stock received from suppliers and show what we owe. Supplier payments need the owner.",
        "salesman": "You are the Khareed Munshi. Salesmen don't handle purchases.",
        "driver": "You are the Khareed Munshi. Drivers don't handle purchases.",
    }
    default_wh = repo.default_warehouse_id() if repo.list_warehouses() else ""

    def _purchase_args(t: str) -> dict:
        items = parse_items(t, repo)
        cost = re.search(r"(?:@|at|rate)\s*(\d[\d,]*)", t, re.I)
        if cost and items: items = [dict(i, unit_cost=float(cost.group(1).replace(",", ""))) for i in items]
        return {"supplier_id": parse_supplier(t, repo) or "", "items": items, "warehouse_id": (ids_in(t, "WH") or [default_wh])[0], "invoice_ref": (re.search(r"(?:bill|invoice|ref)\s*#?\s*([A-Za-z0-9-]+)", t, re.I) or [None, ""])[1] if re.search(r"(?:bill|invoice|ref)\s*#?\s*([A-Za-z0-9-]+)", t, re.I) else "", "paid_amount": 0}

    rules = [
        Rule(lambda t: contains("pay", "paid", "payment")(t) and bool(parse_supplier(t, repo)), "pay_supplier",
             lambda t: {"supplier_id": parse_supplier(t, repo) or "", "amount": money_in(t), "method": method_in(t), "ref": t[:60]}),
        Rule(lambda t: contains("received", "purchase", "bought", "khareed", "aaya", "arrived", "stock in")(t) and bool(parse_items(t, repo)), "record_purchase", _purchase_args),
        Rule(contains("payable", "owe", "dena", "suppliers"), "payables_report", lambda t: {}),
        Rule(lambda t: bool(parse_supplier(t, repo)), "supplier_khata", lambda t: {"supplier_id": parse_supplier(t, repo)}),
    ]
    m = _model(model, rules, "Tell me what arrived and from whom, e.g. 'received 100 urea from Fauji at 3600', or ask what we owe.",
               [(lambda p: "need the owner" in p, "Supplier payments need the owner's approval."),
                (lambda p: "don't handle purchases" in p, "Purchases are handled by the office.")])
    return build_specialist("khareed", "Khareed Munshi", m, {"owner": owner, "clerk": clerk, "salesman": [], "driver": []}, prompts, checkpointer)


# ------------------------------------------------------------------ Wasooli
def build_wasooli_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    office = [T["aging_report"], T["get_customer_khata"], T["broken_promises"], T["draft_reminder"], T["draft_due_reminders"], T["send_reminder"], T["log_promise"]]
    field = [T["aging_report"], T["get_customer_khata"], T["log_promise"]]
    prompts = {
        "owner": f"You are the Wasooli Munshi for {B}. Age the receivables, draft templated reminders in the right tone for how overdue each account is, log promises to pay, and escalate the ones that slip. Never write free text to a customer; only approved templates go out.",
        "clerk": f"You are the Wasooli Munshi for {B}. Draft and send templated reminders after approval; log promises; flag broken ones.",
        "salesman": f"You are the Wasooli Munshi for {B}, with a salesman on the route. Show who owes what and log promises to pay that customers make to him. Reminders are sent by the office.",
        "driver": "You are the Wasooli Munshi. Drivers don't run collections.",
    }
    rules = [
        Rule(contains("promise", "wada", "will pay"), "log_promise",
             lambda t: {"customer_id": parse_customer(t, repo) or "", "amount": money_in(t), "promised_date": date_in(t) or ""}),
        Rule(contains("broken", "missed", "slipped"), "broken_promises", lambda t: {}),
        Rule(lambda t: contains("send")(t) and bool(ids_in(t, "REM")), "send_reminder", lambda t: {"reminder_id": ids_in(t, "REM")[0]}),
        Rule(lambda t: contains("remind", "reminder", "yaad")(t) and bool(parse_customer(t, repo)), "draft_reminder",
             lambda t: {"customer_id": parse_customer(t, repo), "tier": next((x for x in ("gentle", "firm", "final") if x in t.lower()), "")}),
        Rule(contains("remind", "reminders", "collections", "chase", "wasooli"), "draft_due_reminders", lambda t: {"min_days_overdue": max(1, int_in(t) or 1)}),
        Rule(lambda t: contains("khata", "balance")(t) and bool(parse_customer(t, repo)), "get_customer_khata", lambda t: {"customer_id": parse_customer(t, repo)}),
        Rule(contains("aging", "overdue", "receivable", "who owes", "baqi", "owes"), "aging_report", lambda t: {}),
    ]
    m = _model(model, rules, "I can show aging, draft reminders for overdue accounts, send an approved reminder, log a promise to pay, or list broken promises.")
    return build_specialist("wasooli", "Wasooli Munshi", m, {"owner": office, "clerk": office, "salesman": field, "driver": []}, prompts, checkpointer)


# ------------------------------------------------------------------ Report (read-only)
def build_report_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    tools = [T["get_digest"], T["sales_report"], T["profit_summary"], T["collection_report"], T["stock_ledger"], T["stock_valuation"], T["slow_stock"], T["top_customers"], T["payables_report"], T["aging_report"]]
    prompts = {
        "owner": f"You are the Report Munshi for {B}. Answer questions about sales, margin, collections, stock movement and valuation from the records. You only read; you never change anything. Give numbers with the period they cover.",
        "clerk": f"You are the Report Munshi for {B}. Answer questions about sales, collections and stock from the records. Read-only.",
        "salesman": "You are the Report Munshi. Reports are for the office.",
        "driver": "You are the Report Munshi. Reports are for the office.",
    }
    rules = [
        Rule(contains("profit", "munafa", "net", "margin"), "profit_summary", lambda t: {"start": (re.findall(r"\d{4}-\d{2}-\d{2}", t) + ["", ""])[0], "end": (re.findall(r"\d{4}-\d{2}-\d{2}", t) + ["", ""])[1]}),
        Rule(contains("collection", "collected", "recovery"), "collection_report", lambda t: {}),
        Rule(contains("valuation", "worth", "stock value"), "stock_valuation", lambda t: {}),
        Rule(contains("slow", "dead stock", "not selling", "nahi bik"), "slow_stock", lambda t: {"days": int_in(t) or 30}),
        Rule(contains("top customers", "best customers", "top"), "top_customers", lambda t: {"days": int_in(t) or 30}),
        Rule(lambda t: contains("ledger", "movement", "history")(t) and bool(_sku_in(t, repo)), "stock_ledger", lambda t: {"sku": _sku_in(t, repo), "warehouse_id": (ids_in(t, "WH") or [""])[0]}),
        Rule(contains("sales", "sale", "bikri", "revenue"), "sales_report", lambda t: {"start": (re.findall(r"\d{4}-\d{2}-\d{2}", t) + ["", ""])[0], "end": (re.findall(r"\d{4}-\d{2}-\d{2}", t) + ["", ""])[1]}),
        Rule(contains("payable", "owe"), "payables_report", lambda t: {}),
        Rule(contains("digest", "summary", "today"), "get_digest", lambda t: {}),
    ]
    m = _model(model, rules, "Ask for sales, profit, collections, stock valuation, slow stock, top customers or a product's stock ledger.",
               [(lambda p: "for the office" in p, "Reports are for the office.")])
    return build_specialist("report", "Report Munshi", m, {"owner": tools, "clerk": tools, "salesman": [], "driver": []}, prompts, checkpointer)


BUILDERS = {
    "order": build_order_munshi, "godown": build_godown_munshi, "delivery": build_delivery_munshi, "hisaab": build_hisaab_munshi,
    "khareed": build_khareed_munshi, "wasooli": build_wasooli_munshi, "report": build_report_munshi,
}
