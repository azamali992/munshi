"""The seven specialist munshis, plus the help desk. Each is built from the shared
factory with its own role->tools map, role->prompt map, and -- for offline runs --
its own deterministic rule set. A real LLM (LLM_PROVIDER=groq) gets the same tools
and prompts and does the language work itself.

Roles: owner, clerk, salesman, driver. The tool list bound for a role IS
that role's capability; there is no prompt-based refusal to bypass.

Offline rules follow one principle: a rule only fires when the language layer
(llm/parse.py, llm/resolve.py) is sure -- the customer resolved with a clear lead,
every product and quantity accounted for, the amount unambiguous. Otherwise no
tool is called and the specialist's fallback asks ONE question naming what it
needs ("Which customer -- Chaudhry Farms (C-002) or Chaudhry Traders (C-011)?")."""
from __future__ import annotations

import re
from datetime import timedelta

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage

from munshi.agents.factory import AgentBundle, build_specialist, is_real_model
from munshi.domain.repository import MunshiRepository
from munshi.llm import memory as MEM
from munshi.llm import replies as RP
from munshi.llm.answers import lang_of
from munshi.llm.followup import PRONOUN
from munshi.llm.parse import (
    amount_in,
    analyse_close,
    analyse_order,
    catalogue,
    customer_resolution,
    date_in,
    ids_in,
    int_in,
    is_bounce,
    method_in,
    prepare,
    sku_in,
    supplier_resolution,
    warehouses_in,
)
from munshi.llm.stub_model import NotUnderstood, Rule, StubToolCallingModel, contains, history, memo
from munshi.llm.text import fold, is_urdu
from munshi.tools.core import MunshiTools
from munshi.tools.langchain_tools import build_tools

ROLES = ("owner", "clerk", "salesman", "driver")


def _model(model, rules, fallback, prompt_fallbacks=(), fallback_fn=None):
    return model or StubToolCallingModel(rules=rules, fallback_text=fallback, prompt_fallbacks=list(prompt_fallbacks), fallback_fn=fallback_fn)


def _biz(repo: MunshiRepository) -> str:
    return repo.business_name


def _lookups(model, *tools) -> list:
    """Read-only name lookups a real model needs to turn a name into an ID itself (find_customer, search_products...).
    The offline rules resolve names in code and never call them, so they are bound only for a real model."""
    return list(tools) if is_real_model(model) else []


# A reversal is only ever asked for against a named entry: the words alone are not enough.
_REVERSE = contains("reverse", "reversal", "undo", "bounced", "bounce")


def _khata_ids(text: str, repo: MunshiRepository) -> list[str]:
    """Customer-khata entry ids in the text: receipts, credit notes, opening balances, invoices (the business's own prefix too)."""
    prefixes = ["RCP", "CRN", "OPB", "REV", "INV", (repo.setting("invoice_prefix") or "INV").strip().upper()]
    return [i for p in dict.fromkeys(prefixes) for i in ids_in(text, p)]


# ------------------------------------------------------------------ shared language helpers (memoised per model call)
def _order(text, repo):
    return memo(("order", text), lambda: analyse_order(text, repo))


def _cust(text, repo):
    return memo(("cust", text), lambda: customer_resolution(text, repo))


def _supp(text, repo):
    return memo(("supp", text), lambda: supplier_resolution(text, repo))


def _amount(text):
    return memo(("amt", text), lambda: amount_in(text))


def _sku(text, repo):
    return memo(("sku", text), lambda: sku_in(text, repo))


# A pronoun or possessive pointing back at someone already mentioned ('isko', 'uska', 'their', 'اس نے'). The SAME
# predicate is the guard's (agents/guard.py), so the offline rules and a real model lean on history identically.
_PRONOUN = PRONOUN


def _roman(t: str) -> bool:
    return lang_of(t) == "ru"


def _recent(pattern: str, key: str | None = None, lookback: int = 8) -> str:
    """The most recent ID matching `pattern` in this thread's last few messages -- from tool results, or from
    the `key` argument of a tool call. Used only for a pronoun ('isko confirm kar do', 'uska balance')."""
    msgs = history()[:-1][-lookback:]
    for m in reversed(msgs):
        if key and isinstance(m, AIMessage):
            for c in reversed(m.tool_calls or []):
                v = str((c.get("args") or {}).get(key) or "")
                if re.fullmatch(pattern, v):
                    return v
        if isinstance(m, ToolMessage):
            found = re.findall(pattern, str(m.content))
            if found:
                return found[0]
    return ""


def _order_ref(text: str, repo) -> str:
    """The order a confirm/cancel is about: an ID in the text, else -- only for a pronoun ('isko', 'ye wala') -- the
    last order on this thread, and only if it belongs to the customer the message names (when it names one)."""
    ids = ids_in(text, "ORD")
    if ids:
        return ids[0]
    if not _PRONOUN(text):
        return ""
    oid = _recent(r"ORD-[A-Z0-9]{8}", "order_id")
    named = _cust(text, repo)
    if oid and named.status != "none":
        try:
            if not named.ok or repo.get_order(oid).customer_id != named.id:
                return ""
        except Exception:
            return ""
    return oid


def _customer_or_recent(text: str, repo) -> str:
    """The customer the message names; else, only for a pronoun / possessive ('uska balance', 'their khata'), the last one
    this specialist's thread dealt with. (The platform's topic memory has usually already added the ID to the text.)"""
    r = _cust(text, repo)
    if r.ok:
        return r.id or ""
    if r.status == "none" and _PRONOUN(text):
        return _recent(r"C-\d{3,}", "customer_id")
    return ""


def _period(text: str) -> tuple[str, str]:
    """('start', 'end') for 'aaj', 'kal', 'is hafte', 'pichle hafte', 'is mahine', 'pichle mahine'; ('', '') = the tool's default."""
    iso = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    if iso:
        return (iso + [""])[0], (iso + ["", ""])[1]
    from munshi.domain.models import business_today
    today = business_today()
    t = fold(text)
    if re.search(r"\b(aaj|today)\b|اج", t):
        return today.isoformat(), today.isoformat()
    if re.search(r"\b(yesterday|kal)\b", t):
        y = today - timedelta(days=1)
        return y.isoformat(), y.isoformat()
    if re.search(r"\b(pichl[ea]y? hafte|last week|pichle haftay)\b", t):
        mon = today - timedelta(days=today.weekday() + 7)
        return mon.isoformat(), (mon + timedelta(days=6)).isoformat()
    if re.search(r"\b(is hafte|this week|is haftay)\b", t):
        return (today - timedelta(days=today.weekday())).isoformat(), today.isoformat()
    if re.search(r"\b(pichl[ea]y? mahine|last month)\b", t):
        first = today.replace(day=1)
        prev_end = first - timedelta(days=1)
        return prev_end.replace(day=1).isoformat(), prev_end.isoformat()
    if re.search(r"\b(is mahine|this month|is maah)\b|اس مہینی", t):
        return today.replace(day=1).isoformat(), today.isoformat()
    return "", ""


def _urdu_didnt(text: str) -> str | None:
    """The explicit "didn't understand" outcome: Urdu wording for Urdu script, else the specialist's generic line."""
    return NotUnderstood(RP.t("didnt", True)) if is_urdu(text) else None


_KHATA = contains("khata", "khaata", "account", "balance", "outstanding", "baqi", "baaki", "udhaar", "udhar", "hisaab", "hisab", "owe", "owes", "dena hai", "dene hain",
                  "kitne paise", "کھاتہ", "کھاتا", "حساب", "بیلنس", "باقی", "ادھار")
_STOCKQ = contains("stock", "available", "kitna", "kitni", "kitne", "bachi", "pari", "padi", "how much", "how many", "hai", "اسٹاک", "سٹاک", "کتنا", "کتنی")


# ------------------------------------------------------------------ Order
def build_order_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None, guarded: bool | None = None) -> AgentBundle:
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
    list_words = contains("orders", "order list", "list", "pending", "draft", "drafts", "dikhao", "dikha", "kaun se", "konse", "latest", "recent",
                          "naye", "new orders", "aaj ke", "kitne", "bane", "aaye", "aye", "آرڈرز")
    order_verb = contains("naya order", "new order", "order likho", "order likh", "order lo", "order le lo", "order dena", "order hai", "bhejna",
                          "bhejna hai", "maal bhejna", "order book", "book karo", "نیا آرڈر")

    def _list_status(t: str) -> str:
        f = fold(t)
        for s in ("draft", "confirmed", "allocated", "dispatched", "delivered", "short", "cancelled"):
            if re.search(rf"\b{s}\b", f):
                return s
        return ""

    def _list_args(t: str) -> dict:
        """list_orders filters read from the message: status, one product, one customer, today."""
        a: dict = {"status": _list_status(t)}
        if _sku(t, repo):
            a["sku"] = _sku(t, repo)
        c = _cust(t, repo)
        if c.ok and c.other is None:
            a["customer_id"] = c.id
        if contains("aaj", "aj", "today", "todays", "آج")(t):
            a["days"] = 1
        return a

    def _is_list(t: str) -> bool:
        op = _order(t, repo)
        c = _cust(t, repo)
        return (list_words(t) and contains("order", "orders", "آرڈر", "آرڈرز")(t) and not op.items and c.status != "ambiguous"
                and not order_verb(t) and not cancel_w(t) and not confirm_w(t))

    repeat = contains("dobara", "phir se", "repeat", "pichli dafa", "pichla", "pichle", "last time", "same order", "wahi order", "دوبارہ")

    cancel_w = contains("cancel", "mansookh", "منسوخ")
    confirm_w = lambda t: contains("confirm", "pakka", "کنفرم", "پکا")(t) or (contains("haan", "yes", "ok")(t) and bool(ids_in(t, "ORD")))  # noqa: E731

    def _repeat(t: str):
        """The repeat plan (llm.memory.repeat_plan) for a "same order again" request naming one customer, else None."""
        def plan():
            op, c = _order(t, repo), _cust(t, repo)
            if not MEM.asks_repeat(t) or op.items or op.problems or not c.ok or c.other is not None:
                return None
            return MEM.repeat_plan(repo, c.id or "")
        return memo(("repeat", t), plan)

    def _repeat_reply(t: str, urdu: bool) -> str:
        c = _cust(t, repo)
        if not c.ok or c.other is not None:
            return RP.ask_customer(c, urdu) if c.status != "ok" else RP.t("two_customers", urdu, a=c.name, b=c.other.name)
        p = _repeat(t)
        if p is None or p.status == "none":
            return f"I have no confirmed order for {c.name} to repeat yet -- tell me the items and quantities, e.g. '{c.name} ko 20 urea aur 5 dap'."
        if p.status == "open":
            return (f"{c.name}'s latest order {p.order_id} ({p.lines}) is still {p.order_status} -- repeating it now could double the order, "
                    f"so I haven't made a card. If you do want the same again, send the items as a new order.")
        return (f"{c.name}'s last order {p.order_id} had {p.problem}, which isn't sold any more -- tell me the items and quantities for this one.")

    def _open_orders(cid: str) -> str:
        rows = [o for o in repo.list_orders(customer_id=cid, limit=10) if o.status in ("draft", "confirmed", "allocated")][:4]
        return "; ".join(f"{o.order_id} ({o.status}, " + ", ".join(f"{i.qty} x {i.sku}" for i in o.items) + ")" for o in rows)

    rules = [
        Rule(lambda t: cancel_w(t) and bool(_order_ref(t, repo)), "cancel_order", lambda t: {"order_id": _order_ref(t, repo), "reason": t[:80]}),
        Rule(lambda t: confirm_w(t) and bool(_order_ref(t, repo)), "confirm_order", lambda t: {"order_id": _order_ref(t, repo)}),
        Rule(lambda t: bool(ids_in(t, "ORD")), "get_order", lambda t: {"order_id": ids_in(t, "ORD")[0]}),
        Rule(_is_list, "list_orders", _list_args),
        Rule(lambda t: _KHATA(t) and bool(_customer_or_recent(t, repo)), "get_customer_khata", lambda t: {"customer_id": _customer_or_recent(t, repo)}),
        Rule(lambda t: contains("rate", "price", "qeemat", "bhao", "قیمت", "ریٹ")(t) and bool(_sku(t, repo)), "search_products", lambda t: {"text": _sku(t, repo)}),
        Rule(lambda t: _STOCKQ(t) and bool(_sku(t, repo)) and _cust(t, repo).status == "none" and not _order(t, repo).items, "get_stock",
             lambda t: {"sku": _sku(t, repo)}),
        Rule(lambda t: _order(t, repo).ready, "create_order",
             lambda t: {"customer_id": _order(t, repo).customer.id, "items": _order(t, repo).items, "source_text": t}),
        # 'Haji Sons ko wahi order dobara' / 'pichla order repeat karo' (the customer from the message or the conversation):
        # the lines of their last confirmed, delivered order, on a card -- a human still approves what is shown
        Rule(lambda t: _repeat(t) is not None and _repeat(t).status == "ok", "create_order",
             lambda t: {"customer_id": _cust(t, repo).id, "items": _repeat(t).items, "source_text": t}),
        # just a customer ('aur Haji Sons ka?'): their khata -- a read, and only for a confident match
        Rule(lambda t: _cust(t, repo).ok and not _order(t, repo).items and not _order(t, repo).problems and not repeat(t)
             and not cancel_w(t) and not confirm_w(t) and not order_verb(t), "get_customer_khata",
             lambda t: {"customer_id": _cust(t, repo).id}),
    ]

    def fallback(t: str, system: str) -> str | None:
        urdu = is_urdu(t)
        op = _order(t, repo)
        if cancel_w(t) or confirm_w(t):
            verb = "cancel" if cancel_w(t) else "confirm"
            opts = _open_orders(op.customer.id) if op.customer.ok else ""
            where = f" {op.customer.name}'s open orders: {opts}." if opts else f" Say its ID, e.g. '{verb} ORD-...'."
            return RP.t("which_order", False, verb=verb, where=where)
        if MEM.asks_repeat(t) and not op.items and not op.problems:
            return _repeat_reply(t, urdu)
        if order_verb(t) and op.customer.ok and not op.items and not op.problems:
            return RP.t("no_items", urdu)
        if repeat(t) and not op.items:
            return "Tell me the items and quantities (the customer's earlier orders are in Orders)."
        if _KHATA(t) and not op.items:
            return RP.ask_customer(op.customer, urdu)
        if op.items or op.problems or op.unknown or op.dropped:
            return RP.ask_order(op, urdu)
        if op.customer.status == "ambiguous":
            return RP.ask_customer(op.customer, urdu)
        if op.customer.ok:
            return RP.t("no_items", urdu)
        return _urdu_didnt(t)

    m = _model(model, rules, "Tell me the customer and the items, e.g. 'Chaudhry Farms ko 20 urea aur 5 dap'.",
               [(lambda p: "cannot place orders" in p, "Drivers can't place orders or look up khata — please pass this to the office."),
                (lambda p: "cannot confirm or cancel" in p, "Salesmen book drafts; the office confirms or cancels. Give me the customer and items to book.")],
               fallback)
    return build_specialist("order", "Order Munshi", m, {"owner": desk, "clerk": desk, "salesman": booker, "driver": []}, prompts, checkpointer, repo=repo, guarded=guarded)


# ------------------------------------------------------------------ Godown
def build_godown_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None, guarded: bool | None = None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    read = [T["get_stock"], T["list_orders"], T["get_order"], T["list_routes"], T["list_vehicles"], T["suggest_dispatch"], T["get_plan"]] + _lookups(model, T["search_products"])
    clerk = read + [T["allocate_order"], T["create_dispatch_plan"], T["approve_dispatch_plan"], T["transfer_stock"]]
    owner = clerk + [T["adjust_stock"]]
    prompts = {
        "owner": f"You are the Godown Munshi for {B}. Allocate confirmed orders against real stock, plan dispatch onto routes and vehicles within capacity, move stock between godowns, and adjust stock only with a reason. Never promise stock that isn't available.",
        "clerk": f"You are the Godown Munshi for {B}. Allocate confirmed orders, plan dispatch, transfer between godowns. Stock adjustments need the owner.",
        "salesman": "You are the Godown Munshi. Salesmen can check stock but not allocate or plan.",
        "driver": "You are the Godown Munshi. Drivers can view their plan but not change allocation.",
    }
    default_wh = repo.default_warehouse_id() if repo.list_warehouses() else ""
    write_off = contains("write off", "write-off", "writeoff", "damaged", "reduce", "phat", "phati", "kharab", "chori", "lost", "toot", "expired")

    def _transfer(t: str) -> dict | None:
        whs = warehouses_in(t, repo)
        op = _order(t, repo)
        if len(whs) != 2 or len(op.items) != 1 or [p for p in op.problems if not p.startswith("two customers")]:
            return None
        f = fold(t)
        src, dst = whs
        second_name = next((w for w in repo.list_warehouses() if w.warehouse_id == dst), None)
        if second_name and re.search(rf"\b{re.escape(fold(second_name.name.split()[0]))}\s+se\b|\bfrom\s+{re.escape(fold(dst))}", f):
            src, dst = dst, src
        return {"from_warehouse": src, "to_warehouse": dst, "sku": op.items[0]["sku"], "qty": op.items[0]["qty"]}

    def _qty(t: str) -> int:
        """The one quantity the message states (IDs, SKU codes, weights like '50kg', dates, prices masked); 0 if none or several."""
        toks = memo(("prep", t), lambda: prepare(t, catalogue(repo))).tokens
        nums = {float(x) for x in toks if re.fullmatch(r"\d+(?:\.\d+)?", x)}
        if not nums and "xnegx" in toks:              # 'adjust urea -5'
            return int_in(t)
        if len(nums) != 1:
            return 0
        n = next(iter(nums))
        return int(n) if float(n).is_integer() else 0

    all_stock_words = contains("stock", "stocks", "inventory", "stock count", "maal", "products", "product", "items", "اسٹاک", "سٹاک", "مال")

    def _all_stock(t: str) -> bool:
        return (all_stock_words(t) and not _sku(t, repo) and not ids_in(t, "ORD") and not ids_in(t, "DSP") and _cust(t, repo).status == "none"
                and not contains("transfer", "move", "shift", "allocate", "dispatch", "plan", "load")(t))

    def _adjust(t: str) -> dict | None:
        sku, n = _sku(t, repo), _qty(t)
        if not sku or n == 0:
            return None
        whs = warehouses_in(t, repo)
        if len(whs) > 1:
            return None
        return {"warehouse_id": (whs or [default_wh])[0], "sku": sku, "delta": -abs(n) if write_off(t) else n, "reason": t[:80]}

    # stock coming in, or the count going up: a WRITE. It never gets a stock read back.
    arrived = contains("aaye", "aaya", "aayi", "aye", "aya", "ayi", "ayein", "aayein", "aaein", "aain", "aa gaye", "aa gaya", "aa gayi", "arrived",
                       "came in", "pohanch gaya", "pohnch gaya", "stock in", "آئے", "آیا", "آئی")
    increase = contains("increase", "brha", "barha", "bara do", "badha", "barhao", "brhao", "barhado", "brhado", "add", "plus", "correction",
                        "adjust", "count theek", "sahi karo", "بڑھا")
    write_shaped = lambda t: (arrived(t) or increase(t) or write_off(t) or contains("restock")(t)) and bool(_qty(t) or _sku(t, repo))  # noqa: E731

    def _stock_kind(t: str) -> str:
        """Stock arrived with no supplier named: a purchase (which supplier, what price?) or a correction? One question."""
        urdu, roman = is_urdu(t), _roman(t)
        sku, n = _sku(t, repo), _qty(t)
        if not sku or n <= 0:
            return RP.t("stock_in_what", urdu, roman)
        whs = warehouses_in(t, repo)
        wid = (whs or [default_wh])[0]
        try:
            p = repo.get_product(sku)
            pname, price = p.name, f"{p.cost_price:g}"
        except Exception:
            pname, price = sku, "3600"
        try:
            gname = repo.get_warehouse(wid).name
        except Exception:
            gname = wid
        return RP.t("stock_kind", urdu, roman, qty=f"{n:,}", product=pname, godown=gname, price=price)

    rules = [
        Rule(contains("approve", "load", "loading", "manzoor"), "approve_dispatch_plan", lambda t: {"plan_id": (ids_in(t, "DSP") or [""])[0]}),
        Rule(lambda t: contains("transfer", "move", "shift")(t) and _transfer(t) is not None, "transfer_stock", lambda t: _transfer(t)),
        Rule(lambda t: (contains("restock", "adjust", "write off", "write-off", "damaged", "received", "phat", "phati", "kharab", "chori")(t) or increase(t))
             and _adjust(t) is not None, "adjust_stock", lambda t: _adjust(t)),
        Rule(lambda t: contains("allocate", "reserve")(t) and bool(ids_in(t, "ORD")), "allocate_order",
             lambda t: {"order_id": ids_in(t, "ORD")[0], "warehouse_id": (warehouses_in(t, repo) or [default_wh])[0]}),
        Rule(lambda t: contains("dispatch", "plan", "bhejo", "gaari")(t) and bool(ids_in(t, "ORD")), "create_dispatch_plan",
             lambda t: {"route_id": (ids_in(t, "R") or [""])[0], "vehicle_id": (ids_in(t, "V") or [""])[0],
                        "order_ids": ids_in(t, "ORD"), "plan_date": date_in(t) if re.search(r"\d{4}-\d{2}-\d{2}", t) else ""}),
        Rule(lambda t: contains("dispatch", "suggest", "today", "aaj")(t) and not write_shaped(t) and not _all_stock(t), "suggest_dispatch",
             lambda t: {"plan_date": (re.findall(r"\d{4}-\d{2}-\d{2}", t) or [""])[0]}),
        Rule(lambda t: bool(_sku(t, repo)) and (_STOCKQ(t) or contains("godown", "گودام")(t)) and not write_shaped(t), "get_stock", lambda t: {"sku": _sku(t, repo)}),
        # a stock question about no one product: every product's stock ('stocks kitne baqi hein', 'aj ka stock count', 'for all the products?')
        Rule(lambda t: _all_stock(t) and not write_shaped(t), "get_stock", lambda t: {"sku": ""}),
        Rule(contains("confirmed", "allocated", "orders"), "list_orders", lambda t: {"status": "confirmed" if "confirmed" in t.lower() else "allocated"}),
    ]

    def fallback(t: str, system: str) -> str | None:
        urdu = is_urdu(t)
        if contains("transfer", "move", "shift")(t):
            return "Which product, how many, and from which godown to which? e.g. 'transfer 20 urea WH-MULTAN WH-VEHARI'."
        if arrived(t) or (increase(t) and not _adjust(t)):
            return _stock_kind(t) if arrived(t) else RP.t("stock_in_what", urdu, _roman(t))
        if _STOCKQ(t) and not _sku(t, repo):
            return RP.t("which_product", urdu)
        if contains("restock", "adjust", "write off", "damaged")(t):
            return "Which product and how many, with the reason? e.g. 'write off 5 urea, bags torn'."
        return _urdu_didnt(t)

    m = _model(model, rules, "I can check stock, allocate confirmed orders, suggest or create a dispatch plan, transfer stock, and approve loading.",
               [(lambda p: "need the owner" in p, "Stock adjustments need the owner — ask them to approve."),
                (lambda p: "not allocate or plan" in p, "Salesmen can check stock; allocation and dispatch are the office's job."),
                (lambda p: "view their plan" in p, "Drivers can see their stops with the Delivery Munshi: ask 'mera agla stop kaun sa hai'.")],
               fallback)
    return build_specialist("godown", "Godown Munshi", m, {"owner": owner, "clerk": clerk, "salesman": [T["get_stock"]], "driver": [T["get_plan"]]}, prompts, checkpointer, repo=repo, guarded=guarded)


# ------------------------------------------------------------------ Delivery
def build_delivery_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None, guarded: bool | None = None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    tools = [T["get_plan"], T["list_stops"], T["get_order"], T["close_stop"]]
    prompts = {r: f"You are the Delivery Munshi for {B}, on the driver's phone. Show the stops in order and close each one with what was delivered, what came back, cash taken, and the customer's OTP. Never close a stop without the OTP." for r in ROLES}
    close_words = contains("close", "delivered", "deliver", "de diya", "diya", "band", "utar", "utar diya", "pohncha", "ڈیلیور", "دے دیا")

    def _close(t: str):
        return memo(("close", t), lambda: analyse_close(t, repo))

    def _close_args(t: str) -> dict:
        c = _close(t)
        delivered = c.delivered
        if delivered is None:                       # 'delivered all' / 'sab de diya': what was loaded for the stop, less anything returned
            try:
                order = repo.get_order(repo.get_stop(c.stop_id).order_id)
                back = {}
                for r in c.returned:
                    back[r["sku"]] = back.get(r["sku"], 0) + r["qty"]
                delivered = [{"sku": i.sku, "qty": i.qty - back.get(i.sku, 0)} for i in order.items if i.qty - back.get(i.sku, 0) > 0]
            except Exception:                      # unknown stop: let the tool report it, don't crash the model
                delivered = []
        return {"stop_id": c.stop_id, "delivered_items": delivered, "returned_items": c.returned, "cash_collected": c.cash, "otp": c.otp}

    def _todays_plans() -> list:
        from munshi.domain.models import business_today
        return [p for p in repo.list_plans(plan_date=business_today().isoformat()) if p.status in ("approved", "loaded")]

    next_words = contains("agla", "next", "kahan", "stops", "stop", "plan", "aaj", "mera", "اگلا", "اسٹاپ")
    rules = [
        Rule(lambda t: close_words(t) and bool(ids_in(t, "STP")) and not _close(t).problems, "close_stop", _close_args),
        Rule(lambda t: bool(ids_in(t, "DSP")), "list_stops", lambda t: {"plan_id": ids_in(t, "DSP")[0]}),
        Rule(lambda t: next_words(t) and not ids_in(t, "STP") and not contains("close", "band", "otp")(t) and len(_todays_plans()) == 1, "list_stops",
             lambda t: {"plan_id": _todays_plans()[0].plan_id}),
    ]

    def fallback(t: str, system: str) -> str | None:
        stop = (ids_in(t, "STP") or [""])[0]
        if stop and close_words(t):
            probs = _close(t).problems
            if probs:
                txt = f"To close {stop} I need {probs[0]}. e.g. 'close {stop} delivered all, cash 50000, otp 1234'."
                return RP.Ask(txt, "otp") if probs == ["the customer's OTP code"] else txt
        if next_words(t) and not stop:
            plans = _todays_plans()
            if not plans:
                return "No dispatch plan is loaded for today yet -- ask the office."
            if len(plans) > 1:
                opts = ", ".join(f"{p.plan_id} ({p.vehicle_id}, {p.route_id})" for p in plans)
                return f"Which vehicle are you on? Today's plans: {opts}. Send 'stops for DSP-...'."
        if contains("close", "band", "otp")(t) and not stop:
            return "I close one stop at a time, each with its own customer's OTP. Which stop? Send 'close STP-... delivered all, cash 50000, otp 1234'."
        return _urdu_didnt(t)

    m = _model(model, rules, "Tell me the plan ID to see stops, or close a stop: 'close STP-XXXX delivered all, cash 50000, OTP 1234'.", (), fallback)
    return build_specialist("delivery", "Delivery Munshi", m, {"owner": tools, "clerk": tools, "salesman": [], "driver": tools}, prompts, checkpointer, repo=repo, guarded=guarded)


# ------------------------------------------------------------------ Hisaab
_EXPENSE_CATS = (("fuel", ("diesel", "petrol", "fuel", "cng")), ("salary", ("salary", "tankhwah", "tankha")), ("rent", ("rent", "kiraya", "kiraaya")),
                 ("utilities", ("bijli", "electric", "electricity", "gas bill", "pani ka bill", "internet")),
                 ("repair", ("repair", "tyre", "tire", "puncture", "mistri", "marammat", "مرمت")),
                 ("loading", ("mazdoor", "mazdoori", "labour", "labor", "loading", "palledar")),
                 ("food", ("khana", "lunch", "food", "chai", "chai pani")))


def _expense_category(t: str) -> str:
    return next((c for c, ws in _EXPENSE_CATS if contains(*ws)(t)), "misc")


def build_hisaab_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None, guarded: bool | None = None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    read = [T["get_digest"], T["get_plan"], T["list_stops"], T["get_customer_khata"], T["cashbook"]] + _lookups(model, T["find_customer"])
    clerk = read + [T["record_deposit"], T["record_payment"], T["record_expense"]]
    owner = clerk + [T["credit_note"], T["reverse_ledger_entry"], T["reverse_expense"]]
    prompts = {
        "owner": f"You are the Hisaab Munshi for {B}. Reconcile cash handed in against cash collected on each plan, attribute any shortfall to a stop, record payments received at the office (cash, bank, JazzCash, Easypaisa, cheque) and expenses, keep the khata honest, and issue credit notes only with a stated reason. Nothing is ever edited or deleted: a mis-keyed khata entry, a bounced cheque or a wrong expense is cancelled by reversing that one entry, by its ID, with a stated reason.",
        "clerk": f"You are the Hisaab Munshi for {B}. Record deposits, office payments and expenses; reconcile. Credit notes and reversals need the owner.",
        "salesman": "You are the Hisaab Munshi. Salesmen don't record money; they can ask the office.",
        "driver": "You are the Hisaab Munshi. Drivers hand cash to the cashier; they don't record deposits.",
    }
    pay_words = contains("paid", "payment", "received", "diye", "diya", "di", "jama", "transfer", "jazzcash", "jazz cash", "easypaisa", "easy paisa", "cheque",
                         "check", "bank", "cash", "bheje", "bheja", "dale", "daale", "mile", "mila", "aaya", "aaye", "aayi", "wasool", "vasool",
                         "جمع", "ادائیگی", "بینک", "وصول")
    expense_words = contains("expense", "kharcha", "kharch", "diesel", "petrol", "fuel", "salary", "tankhwah", "rent", "kiraya", "bijli", "repair",
                             "mazdoor", "mazdoori", "labour", "tyre", "puncture", "marammat", "chai", "chai pani", "خرچہ", "مرمت")
    credit_words = contains("credit note", "credit", "refund", "waive", "maaf")

    def _one_customer(t):
        r = _cust(t, repo)
        return r.ok and r.other is None

    def _amt_ok(t):
        return _amount(t).amount is not None

    def _deposit_amount(t):
        return amount_in(t.replace(ids_in(t, "DSP")[0], " ") if ids_in(t, "DSP") else t)

    rules = [
        Rule(lambda t: _REVERSE(t) and bool(ids_in(t, "EXP")), "reverse_expense", lambda t: {"expense_id": ids_in(t, "EXP")[0], "reason": t[:120]}),
        Rule(lambda t: _REVERSE(t) and bool(_khata_ids(t, repo)), "reverse_ledger_entry", lambda t: {"entry_id": _khata_ids(t, repo)[0], "reason": t[:120]}),
        Rule(lambda t: credit_words(t) and _one_customer(t) and _amt_ok(t), "credit_note",
             lambda t: {"customer_id": _cust(t, repo).id, "amount": _amount(t).amount, "reason": t[:80]}),
        Rule(lambda t: contains("deposit", "handed", "counted", "jama")(t) and bool(ids_in(t, "DSP")) and _deposit_amount(t).amount is not None, "record_deposit",
             lambda t: {"plan_id": ids_in(t, "DSP")[0], "amount_counted": _deposit_amount(t).amount, "counted_by": "cashier"}),
        Rule(lambda t: expense_words(t) and not _cust(t, repo).ok and _amt_ok(t), "record_expense",
             lambda t: {"category": _expense_category(t), "amount": _amount(t).amount, "note": t[:80], "method": method_in(t)}),
        Rule(lambda t: pay_words(t) and not is_bounce(t) and not credit_words(t) and _one_customer(t) and _amt_ok(t), "record_payment",
             lambda t: {"customer_id": _cust(t, repo).id, "amount": _amount(t).amount, "method": method_in(t), "ref": t[:60]}),
        Rule(contains("cashbook", "cash book", "cash today", "rokar", "کیش بک"), "cashbook", lambda t: {"day": (re.findall(r"\d{4}-\d{2}-\d{2}", t) or [""])[0]}),
        Rule(lambda t: _KHATA(t) and not credit_words(t) and bool(_customer_or_recent(t, repo)), "get_customer_khata",
             lambda t: {"customer_id": _customer_or_recent(t, repo)}),
        Rule(lambda t: contains("digest", "today", "summary", "close the day", "aaj", "hisaab", "hisab")(t) and not credit_words(t), "get_digest", lambda t: {}),
    ]

    def fallback(t: str, system: str) -> str | None:
        urdu = is_urdu(t)
        if is_bounce(t):
            return RP.t("bounce", urdu)
        r = _cust(t, repo)
        if credit_words(t) and r.ok and not _amt_ok(t):
            return f"How much should the credit note to {r.name} be, and why? e.g. 'credit note {r.name} 5000 damaged bags'. The owner approves it."
        if pay_words(t) or credit_words(t) or _KHATA(t):
            if r.status == "ambiguous":
                return RP.ask_customer(r, urdu)
            if r.ok and r.other is not None:
                return RP.t("two_customers", urdu, a=r.name, b=r.other.name)
            if r.ok and not _amt_ok(t):
                if _amount(t).problem != "how much?":
                    return f"{_amount(t).problem.capitalize()}."
                return RP.t("how_much_from", urdu, _roman(t), name=r.name) if pay_words(t) else RP.t("how_much", urdu)
            if not r.ok and (pay_words(t) or credit_words(t)):
                return RP.ask_customer(r, urdu)
        if expense_words(t) and not _amt_ok(t):
            return RP.t("how_much", urdu)
        return _urdu_didnt(t)

    m = _model(model, rules, "I can record a deposit against a plan, a payment received at the office, an expense, show a khata or the cashbook, or give today's digest.",
               [(lambda p: "need the owner" in p, "Credit notes and reversals need the owner's approval."),
                (lambda p: "don't record money" in p, "Payments are recorded by the office — tell the clerk."),
                (lambda p: "don't record deposits" in p, "Drivers hand the cash to the cashier; the office records it.")],
               fallback)
    return build_specialist("hisaab", "Hisaab Munshi", m, {"owner": owner, "clerk": clerk, "salesman": [], "driver": []}, prompts, checkpointer, repo=repo, guarded=guarded)


# ------------------------------------------------------------------ Khareed (purchases)
def build_khareed_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None, guarded: bool | None = None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    read = [T["find_supplier"], T["list_suppliers"], T["supplier_khata"], T["payables_report"], T["get_stock"], T["search_products"]]
    clerk = read + [T["record_purchase"]]
    owner = clerk + [T["pay_supplier"], T["reverse_purchase"], T["reverse_supplier_entry"]]
    prompts = {
        "owner": f"You are the Khareed Munshi for {B}, in charge of buying. Receive stock from suppliers into the godown with the bill on their account, track what we owe each supplier, and pay suppliers only with a stated method and reference. Nothing is ever edited or deleted: a mis-keyed purchase is undone with reverse_purchase (the goods go back too), a bounced or misdirected supplier payment with reverse_supplier_entry, each by its ID and with a stated reason.",
        "clerk": f"You are the Khareed Munshi for {B}. Record stock received from suppliers and show what we owe. Supplier payments and reversals need the owner.",
        "salesman": "You are the Khareed Munshi. Salesmen don't handle purchases.",
        "driver": "You are the Khareed Munshi. Drivers don't handle purchases.",
    }
    default_wh = repo.default_warehouse_id() if repo.list_warehouses() else ""
    pay = lambda t: contains("pay", "paid", "payment", "de do", "dedo", "de dein", "transfer", "ada", "ادائیگی")(t) and not contains("received", "aaya", "aayi", "aaye")(t)  # noqa: E731
    arrived = contains("received", "purchase", "bought", "khareed", "aaya", "aayi", "aaye", "aai", "aya", "ayi", "aye", "ayein", "aayein", "aaein", "arrived", "stock in",
                       "aa gaya", "aa gaye", "aa gayi", "آئی", "آیا", "آئے")

    def _purchase_args(t: str) -> dict:
        op = _order(t, repo)
        from munshi.llm.parse import catalogue, prepare
        prices = prepare(t, catalogue(repo)).prices
        items = [dict(i, unit_cost=prices[0]) for i in op.items] if len(set(prices)) == 1 else list(op.items)
        ref = re.search(r"(?:bill|invoice|ref)\s*(?:no\.?|#)?\s*([A-Za-z0-9][A-Za-z0-9-]*)", t, re.I)
        return {"supplier_id": _supp(t, repo).id, "items": items, "warehouse_id": (warehouses_in(t, repo) or [default_wh])[0],
                "invoice_ref": ref.group(1) if ref else "", "paid_amount": 0}

    def _purchase_ready(t: str) -> bool:
        op = _order(t, repo)
        issues = [p for p in op.problems if not p.startswith("two customers")]
        return _supp(t, repo).ok and bool(op.items) and not issues

    rules = [
        Rule(lambda t: _REVERSE(t) and bool(ids_in(t, "PUR")), "reverse_purchase", lambda t: {"purchase_id": ids_in(t, "PUR")[0], "reason": t[:120]}),
        Rule(lambda t: _REVERSE(t) and bool(ids_in(t, "SPY") + ids_in(t, "BIL")), "reverse_supplier_entry",
             lambda t: {"entry_id": (ids_in(t, "SPY") + ids_in(t, "BIL"))[0], "reason": t[:120]}),
        Rule(lambda t: pay(t) and _supp(t, repo).ok and _amount(t).amount is not None, "pay_supplier",
             lambda t: {"supplier_id": _supp(t, repo).id, "amount": _amount(t).amount, "method": method_in(t), "ref": t[:60]}),
        Rule(lambda t: arrived(t) and _purchase_ready(t), "record_purchase", _purchase_args),
        Rule(lambda t: _supp(t, repo).ok and contains("hisaab", "hisab", "khata", "balance", "account", "dena", "kitna", "owe", "حساب")(t), "supplier_khata",
             lambda t: {"supplier_id": _supp(t, repo).id}),
        Rule(contains("payable", "payables", "owe", "dena", "suppliers", "kitna dena"), "payables_report", lambda t: {}),
        Rule(lambda t: _supp(t, repo).ok and not arrived(t) and not pay(t), "supplier_khata", lambda t: {"supplier_id": _supp(t, repo).id}),
    ]

    def fallback(t: str, system: str) -> str | None:
        urdu = is_urdu(t)
        s = _supp(t, repo)
        if (arrived(t) or pay(t)) and not s.ok:
            return RP.ask_supplier(s, urdu)
        if pay(t) and _amount(t).amount is None:
            return RP.t("how_much", urdu)
        if arrived(t):
            op = _order(t, repo)
            issues = [p for p in op.problems if not p.startswith("two customers")]
            if op.unknown:
                return RP.t("unknown_item", urdu, what=op.unknown[0])
            if issues:
                return RP.t("fix_qty", urdu, why=issues[0])
            return RP.t("no_items", urdu)
        return _urdu_didnt(t)

    m = _model(model, rules, "Tell me what arrived and from whom, e.g. 'received 100 urea from Fauji at 3600', or ask what we owe.",
               [(lambda p: "need the owner" in p, "Supplier payments and reversals need the owner's approval."),
                (lambda p: "don't handle purchases" in p, "Purchases are handled by the office.")],
               fallback)
    return build_specialist("khareed", "Khareed Munshi", m, {"owner": owner, "clerk": clerk, "salesman": [], "driver": []}, prompts, checkpointer, repo=repo, guarded=guarded)


# ------------------------------------------------------------------ Wasooli
def build_wasooli_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None, guarded: bool | None = None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    office = [T["aging_report"], T["get_customer_khata"], T["broken_promises"], T["draft_reminder"], T["draft_due_reminders"], T["send_reminder"], T["log_promise"]]         + _lookups(model, T["find_customer"])
    field = [T["aging_report"], T["get_customer_khata"], T["log_promise"]] + _lookups(model, T["find_customer"])
    prompts = {
        "owner": f"You are the Wasooli Munshi for {B}. Age the receivables, draft templated reminders in the right tone for how overdue each account is, log promises to pay, and escalate the ones that slip. Never write free text to a customer; only approved templates go out.",
        "clerk": f"You are the Wasooli Munshi for {B}. Draft and send templated reminders after approval; log promises; flag broken ones.",
        "salesman": f"You are the Wasooli Munshi for {B}, with a salesman on the route. Show who owes what and log promises to pay that customers make to him. Reminders are sent by the office.",
        "driver": "You are the Wasooli Munshi. Drivers don't run collections.",
    }
    promise_words = contains("promise", "wada", "waada", "will pay", "de denge", "denge", "de dega", "dega", "وعدہ")
    broken = contains("broken", "missed", "slipped", "toda", "tod diya", "tora", "توڑا")

    def _tier(t: str) -> str:
        if contains("final", "aakhri", "akhri", "last")(t):
            return "final"
        if contains("firm", "sakht", "strict", "hard")(t):
            return "firm"
        if contains("gentle", "narm", "soft", "polite")(t):
            return "gentle"
        return ""

    def _promise_ready(t: str) -> bool:
        return _cust(t, repo).ok and _amount(t).amount is not None and bool(date_in(t))

    rules = [
        Rule(broken, "broken_promises", lambda t: {}),
        Rule(lambda t: promise_words(t) and _promise_ready(t), "log_promise",
             lambda t: {"customer_id": _cust(t, repo).id, "amount": _amount(t).amount, "promised_date": date_in(t)}),
        Rule(lambda t: contains("send")(t) and bool(ids_in(t, "REM")), "send_reminder", lambda t: {"reminder_id": ids_in(t, "REM")[0]}),
        Rule(lambda t: contains("remind", "reminder", "yaad", "yaad dehani", "یاد دہانی")(t) and _cust(t, repo).ok, "draft_reminder",
             lambda t: {"customer_id": _cust(t, repo).id, "tier": _tier(t)}),
        Rule(lambda t: contains("remind", "reminders", "collections", "chase", "wasooli")(t) and _cust(t, repo).status == "none"
             and not contains("list", "لسٹ", "kis kis", "kaun kaun", "kon kon", "who", "report")(t),
             "draft_due_reminders", lambda t: {"min_days_overdue": max(1, int_in(t) or 1)}),
        Rule(lambda t: _KHATA(t) and _cust(t, repo).ok, "get_customer_khata", lambda t: {"customer_id": _cust(t, repo).id}),
        Rule(contains("aging", "overdue", "receivable", "receivables", "who owes", "baqi", "owes", "kis kis", "kaun kaun", "kon kon", "udhaar", "udhar",
                      "dene", "dena", "lene", "lena", "paise", "kitne paise", "list", "wasooli", "collections", "report", "وصولی", "لسٹ", "ادھار", "پیسے"),
             "aging_report", lambda t: {}),
    ]

    def fallback(t: str, system: str) -> str | None:
        urdu = is_urdu(t)
        r = _cust(t, repo)
        if promise_words(t):
            if not r.ok:
                return RP.ask_customer(r, urdu)
            if _amount(t).amount is None:
                return "How much did they promise, and by when?"
            if not date_in(t):
                return RP.t("by_when", urdu, _roman(t), name=r.name)
        if contains("remind", "reminder", "yaad")(t) and r.status == "ambiguous":
            return RP.ask_customer(r, urdu)
        return _urdu_didnt(t)

    m = _model(model, rules, "I can show aging, draft reminders for overdue accounts, send an approved reminder, log a promise to pay, or list broken promises.",
               [(lambda p: "don't run collections" in p, "Collections are run by the office.")], fallback)
    return build_specialist("wasooli", "Wasooli Munshi", m, {"owner": office, "clerk": office, "salesman": field, "driver": []}, prompts, checkpointer, repo=repo, guarded=guarded)


# ------------------------------------------------------------------ Report (read-only)
def build_report_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None, guarded: bool | None = None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    tools = [T["get_digest"], T["sales_report"], T["profit_summary"], T["collection_report"], T["stock_ledger"], T["stock_valuation"], T["slow_stock"], T["top_customers"], T["payables_report"], T["aging_report"]]
    prompts = {
        "owner": f"You are the Report Munshi for {B}. Answer questions about sales, margin, collections, stock movement and valuation from the records. You only read; you never change anything. Give numbers with the period they cover.",
        "clerk": f"You are the Report Munshi for {B}. Answer questions about sales, collections and stock from the records. Read-only.",
        "salesman": "You are the Report Munshi. Reports are for the office.",
        "driver": "You are the Report Munshi. Reports are for the office.",
    }

    def _range(t: str) -> dict:
        s, e = _period(t)
        return {"start": s, "end": e}

    rules = [
        Rule(contains("profit", "munafa", "net", "margin", "منافع"), "profit_summary", _range),
        Rule(contains("collection", "collections", "collected", "recovery", "payment", "payments", "paid", "wasooli", "ادائیگی", "جمع", "وصولی"),
             "collection_report", _range),
        Rule(contains("valuation", "worth", "stock value", "value"), "stock_valuation", lambda t: {}),
        Rule(contains("slow", "dead stock", "not selling", "nahi bik", "bik nahi"), "slow_stock", lambda t: {"days": int_in(t) or 30}),
        Rule(lambda t: contains("top customers", "best customers", "top")(t) or bool(re.search(r"sab se (zyada|ziada|zaida) kaun|sab se (zyada|ziada|zaida)\b.{0,20}\b(kis ne|kisne|kaun|kon)\b", fold(t))), "top_customers",
             lambda t: {"days": int_in(t) or 30}),
        Rule(lambda t: contains("ledger", "movement", "history")(t) and bool(_sku(t, repo)), "stock_ledger",
             lambda t: {"sku": _sku(t, repo), "warehouse_id": (ids_in(t, "WH") or [""])[0]}),
        Rule(contains("sales", "sale", "bikri", "revenue", "بکری", "سیل"), "sales_report", _range),
        Rule(contains("payable", "owe"), "payables_report", lambda t: {}),
        Rule(contains("digest", "summary", "today"), "get_digest", lambda t: {}),
    ]
    m = _model(model, rules, "Ask for sales, profit, collections, stock valuation, slow stock, top customers or a product's stock ledger.",
               [(lambda p: "for the office" in p, "Reports are for the office.")], lambda t, s: _urdu_didnt(t))
    return build_specialist("report", "Report Munshi", m, {"owner": tools, "clerk": tools, "salesman": [], "driver": []}, prompts, checkpointer, repo=repo, guarded=guarded)


# ------------------------------------------------------------------ Help desk (no tools, ever)
def build_help_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None, guarded: bool | None = None) -> AgentBundle:
    """Greetings, thanks, 'what can you do', and plain refusals of what no munshi may do. It has no tool for any
    role, and it always answers from the fixed, reviewed strings in llm/replies.py -- even when a real model is
    configured -- so a refusal can't be talked into something else and never depends on a provider being up."""
    from munshi.agents import manager as M
    B = _biz(repo)
    prompts = {r: f"You are the help desk for {B} [role:{r}]. Answer greetings and say plainly what you can't do. You have no tools." for r in ROLES}

    def reply(t: str, system: str) -> str:
        urdu = is_urdu(t)
        role = (re.search(r"\[role:(\w+)\]", system) or [None, "clerk"])[1]
        if M._DESTROY(t):
            return RP.t("refuse_delete", urdu)
        if M._SECRET(t):
            return RP.t("refuse_secret", urdu)
        if M._EDIT(t):
            return RP.t("refuse_edit", urdu)
        if M._PERSONAL(t):
            return RP.t("offtopic", urdu) if urdu else "I can't see staff salaries or personal records. " + RP.HELP_BY_ROLE.get(role, "")
        if M._BYPASS(t):
            return RP.t("refuse_bypass", urdu)
        if M._GREET(t):
            return RP.t("greet", urdu)
        if M._THANKS(t):
            return RP.t("thanks", urdu)
        if M._BYE(t):
            return RP.t("bye", urdu)
        if M._HELP(t):
            if contains("sku")(t):
                return RP.t("sku", urdu, _roman(t))
            more = RP.t("help_more", urdu) if role in ("owner", "clerk") else ""
            return RP.HELP_BY_ROLE.get(role, RP.HELP_BY_ROLE["clerk"]) + more
        if M._ACK(t):
            return RP.t("ack", urdu)
        if M._OFFTOPIC(t):
            return RP.t("offtopic", urdu)
        return NotUnderstood(RP.t("didnt", urdu, closest="an order or a khata question"))

    m = StubToolCallingModel(rules=[], fallback_text="", fallback_fn=reply)
    return build_specialist("help", "Munshi", m, {r: [] for r in ROLES}, prompts, checkpointer, repo=repo, guarded=guarded)


BUILDERS = {
    "order": build_order_munshi, "godown": build_godown_munshi, "delivery": build_delivery_munshi, "hisaab": build_hisaab_munshi,
    "khareed": build_khareed_munshi, "wasooli": build_wasooli_munshi, "report": build_report_munshi, "help": build_help_munshi,
}
