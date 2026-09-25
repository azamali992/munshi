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
from munshi.llm.followup import PRONOUN, _strong_pronoun
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
from munshi.llm.text import fold, is_urdu, words
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


OPEN = ("draft", "confirmed", "allocated")


def orders_of(repo, cid: str, statuses=OPEN) -> list:
    """A customer's orders in these statuses, newest first."""
    try:
        return [o for o in repo.list_orders(customer_id=cid, limit=50) if o.status in statuses]
    except Exception:
        return []


def order_label(repo, o) -> str:
    """'3 Zinc Sulphate 10kg, Rs 7,644 (draft, 25 Sep)' -- what a person recognises an order by, no codes."""
    from munshi.llm.answers import _day, _n, rs
    names = {}
    for i in o.items:
        try:
            names[i.sku] = repo.get_product(i.sku).name
        except Exception:
            names[i.sku] = i.sku
    lines = ", ".join(f"{_n(i.qty)} {names[i.sku]}" for i in o.items)
    return f"{lines}, {rs(o.total)} ({o.status}, {_day(o.created_at)})"


def _order_ref(text: str, repo, statuses=OPEN) -> str:
    """The order a confirm/cancel/edit is about: an ID in the text; else -- for a pronoun ('isko', 'ye wala') -- the
    last order on this thread, and only if it belongs to the customer the message names (when it names one); else the
    ONE order in `statuses` of the customer the message names (with two or more, none: the fallback lists them)."""
    ids = ids_in(text, "ORD")
    if ids:
        return ids[0]
    named = _cust(text, repo)
    # 'isko' / 'ye wala' with nobody named: the thread's last order. With a customer named ('Rana Brothers WALA order'),
    # 'wala' is part of the name, not a pointer: only a strong pronoun ('iska', 'unka') leans on the thread then.
    if _PRONOUN(text) and (named.status == "none" or _strong_pronoun(text)):
        oid = _recent(r"ORD-[A-Z0-9]{8}", "order_id")
        if oid and named.status != "none":
            try:
                if not named.ok or repo.get_order(oid).customer_id != named.id:
                    oid = ""
            except Exception:
                oid = ""
        if oid:
            return oid
    if named.ok and named.other is None:
        cands = orders_of(repo, named.id or "", statuses)
        if len(cands) == 1:
            return cands[0].order_id
    return ""


def pick_order(verb: str, cid: str, name: str, repo, statuses=OPEN) -> str:
    """No order named: this customer's candidates as a numbered question ('pehla' / 'doosra' answers it), or why none fits."""
    cands = orders_of(repo, cid, statuses)
    if not cands:
        others = orders_of(repo, cid)
        if others:
            o = others[0]
            return (f"{name}'s order ({order_label(repo, o)}) is {o.status}, so I can't {verb} it -- only a "
                    f"{' or '.join(statuses)} order can be. Nothing was done.")
        return f"{name} has no open order to {verb}. Nothing was done."
    rows = " ".join(f"{k}) {order_label(repo, o)}" for k, o in enumerate(cands[:5], 1))
    return RP.Ask(f"{name} has {len(cands)} orders I could {verb} -- which one? {rows}. Reply 'pehla', 'doosra'...", "order",
                  [{"id": o.order_id, "name": order_label(repo, o)} for o in cands[:5]])


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
                  "kitne paise", "credit limit", "limit", "ledger", "statement", "کھاتہ", "کھاتا", "حساب", "بیلنس", "باقی", "ادھار")
_STOCKQ = contains("stock", "available", "kitna", "kitni", "kitne", "bachi", "bacha", "bache", "pari", "padi", "how much", "how many", "hai", "he", "kam",
                   "khatam", "low", "اسٹاک", "سٹاک", "کتنا", "کتنی")


# ------------------------------------------------------------------ Order
def build_order_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None, guarded: bool | None = None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    read = [T["find_customer"], T["get_customer_khata"], T["search_products"], T["get_stock"], T["get_order"], T["list_orders"]]
    desk = read + [T["create_order"], T["update_order"], T["confirm_order"], T["cancel_order"]]
    booker = read + [T["create_order"], T["update_order"]]      # a salesman may change his own draft (the app's form allows it too)
    prompts = {
        "owner": f"You are the Order Munshi for {B}. Take orders from chat in Urdu, English or mixed. Identify the customer, match items to real SKUs, check stock and the customer's khata, then create a draft order. Never confirm an order without the customer's yes. Cancel only with a reason.",
        "clerk": f"You are the Order Munshi for {B}. Take orders from chat, match to real SKUs, create drafts. Confirm only when the customer has said yes.",
        "salesman": f"You are the Order Munshi for {B}, with a salesman on the route. Book the order as a draft (the office confirms it), and show the customer's khata and stock when asked. You cannot confirm or cancel orders.",
        "driver": "You are the Order Munshi. Drivers cannot place orders; tell them to pass the request to the office.",
    }
    list_words = contains("orders", "order list", "list", "pending", "draft", "drafts", "dikhao", "dikha", "kaun se", "konse", "latest", "recent",
                          "naye", "new orders", "aaj ke", "kitne", "bane", "aaye", "aye", "koi order", "koi orders", "order he", "order hai", "آرڈرز")
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
    # a change to an order already placed ('Rana Brothers ke order mei npk 15 kar do', 'ORD-... update: zinc 5'): update_order, never a new one
    edit_rx = re.compile(r"\border\s+(mei|mein|me|main|mai|may|ma|mn|ko)\b|\b(update|edit|change|badal|badlo|tabdeel)\b.{0,20}\border\b"
                         r"|\border\b.{0,24}\b(update|edit|change|badal|badlo|badal do|tabdeel)\b|آرڈر میں")
    edit_w = lambda t: bool(edit_rx.search(fold(t))) and not order_verb(t) and not cancel_w(t) and not confirm_w(t)  # noqa: E731

    def _edit(t: str) -> dict | None:
        """{"status": ok | items | customer | pick, ...} for a change to an existing order; None if the message isn't one."""
        def calc():
            if not edit_w(t):
                return None
            op = _order(t, repo)
            ids = ids_in(t, "ORD")
            probs = [x for x in op.problems if not (ids and x.startswith("two customers"))]
            if not op.items or probs or op.unknown:
                return {"status": "items"}
            if ids:
                try:
                    o = repo.get_order(ids[0])
                except Exception:
                    return {"status": "missing", "order_id": ids[0]}
                return {"status": "ok" if o.status == "draft" else "not_draft", "order_id": o.order_id, "items": op.items, "order": o}
            c = op.customer
            if not c.ok or c.other is not None:
                return {"status": "customer"}
            drafts = orders_of(repo, c.id or "", ("draft",))
            if len(drafts) == 1:
                return {"status": "ok", "order_id": drafts[0].order_id, "items": op.items}
            return {"status": "pick", "customer_id": c.id, "name": c.name}
        return memo(("edit", t), calc)

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

    edit_ok = lambda t: (_edit(t) or {}).get("status") == "ok"  # noqa: E731

    rules = [
        Rule(lambda t: cancel_w(t) and bool(_order_ref(t, repo)), "cancel_order", lambda t: {"order_id": _order_ref(t, repo), "reason": t[:80]}),
        Rule(lambda t: confirm_w(t) and bool(_order_ref(t, repo, ("draft",))), "confirm_order", lambda t: {"order_id": _order_ref(t, repo, ("draft",))}),
        Rule(edit_ok, "update_order", lambda t: {"order_id": _edit(t)["order_id"], "items": _edit(t)["items"]}),
        Rule(lambda t: bool(ids_in(t, "ORD")) and _edit(t) is None, "get_order", lambda t: {"order_id": ids_in(t, "ORD")[0]}),
        Rule(_is_list, "list_orders", _list_args),
        Rule(lambda t: _KHATA(t) and bool(_customer_or_recent(t, repo)), "get_customer_khata", lambda t: {"customer_id": _customer_or_recent(t, repo)}),
        Rule(lambda t: contains("rate", "price", "qeemat", "bhao", "قیمت", "ریٹ")(t) and bool(_sku(t, repo)), "search_products", lambda t: {"text": _sku(t, repo)}),
        Rule(lambda t: _STOCKQ(t) and bool(_sku(t, repo)) and _cust(t, repo).status == "none" and not _order(t, repo).items, "get_stock",
             lambda t: {"sku": _sku(t, repo)}),
        Rule(lambda t: _order(t, repo).ready and _edit(t) is None, "create_order",
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
            if op.customer.ok and op.customer.other is None:
                return pick_order(verb, op.customer.id or "", op.customer.name, repo, OPEN if verb == "cancel" else ("draft",))
            if op.customer.status == "ambiguous":
                return RP.ask_customer(op.customer, urdu)
            return RP.t("which_order", False, verb=verb, where=" Name the customer, e.g. 'Rana Brothers ka order " + verb + " karo'.")
        ed = _edit(t)
        if ed is not None:
            if ed["status"] == "not_draft":
                o = ed["order"]
                return (f"That order ({order_label(repo, o)}) is {o.status}: only a draft order can be changed. Nothing was done -- "
                        "cancel it and book a new one if the customer wants something different.")
            if ed["status"] == "missing":
                return f"I can't find order {ed['order_id']}. Nothing was done."
            if ed["status"] == "pick":
                return pick_order("change", ed["customer_id"], ed["name"], repo, ("draft",))
            if ed["status"] == "customer":
                return RP.ask_customer(op.customer, urdu) if op.customer.status == "ambiguous" else RP.t("no_customer", urdu)
            return RP.ask_order(op, urdu) if (op.problems or op.unknown) else "Which product, and what should its quantity be? e.g. 'Rana Brothers ke order mei npk 15 kar do'."
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
    write_off = contains("write off", "write-off", "writeoff", "damaged", "damage", "reduce", "phat", "phati", "phata", "phat gayi", "kharab", "chori",
                         "lost", "toot", "tooti", "toot gayi", "expired", "nikal do", "nikaal do", "gal gayi", "bheeg gayi", "خراب", "پھٹ")
    move_w = contains("transfer", "move", "shift", "bhej do", "bhejo", "bhej", "bhijwa do", "le jao", "pohncha do", "pohnchao")

    def _wh_pos(t: str) -> list[tuple[str, int, int]]:
        """(godown id, start, end) of every mention of a godown's distinctive name word, in order."""
        f, out = fold(t), []
        for w in repo.list_warehouses():
            for tok in [fold(w.warehouse_id)] + [x for x in words(fold(w.name)) if x not in ("godown", "warehouse", "store", "main") and len(x) >= 4]:
                for m in re.finditer(rf"(?<![\w-]){re.escape(tok)}(?![\w-])", f):
                    out.append((w.warehouse_id, m.start(), m.end()))
        return sorted(out, key=lambda x: x[1])

    def _transfer(t: str) -> dict | None:
        """One product, one quantity, two godowns: 'X se' / 'from X' is the source, the other is where it goes."""
        whs = warehouses_in(t, repo)
        sku, n = _sku(t, repo), _qty(t)
        if len(whs) != 2 or not sku or n <= 0 or _cust(t, repo).ok:
            return None
        f = fold(t)
        src = next((w for w, _, e in _wh_pos(t) if re.match(r"\s*(se|sy|say|سے)\b", f[e:])), None) or \
            next((w for w, s, _ in _wh_pos(t) if re.search(r"\bfrom\s*$", f[:s])), None)
        if src is None:
            src = whs[0]
        dst = next(w for w in whs if w != src)
        return {"from_warehouse": src, "to_warehouse": dst, "sku": sku, "qty": n}

    def _route_wh(cid: str) -> str:
        try:
            return repo.get_route(repo.get_customer(cid).route_id).warehouse_id
        except Exception:
            return ""

    def _can_hold(o, wid: str) -> bool:
        need: dict[str, int] = {}
        for it in o.items:
            need[it.sku] = need.get(it.sku, 0) + it.qty
        try:
            return all(repo.get_stock(wid, sku).available >= q for sku, q in need.items())
        except Exception:
            return False

    def _alloc(t: str) -> dict | None:
        """{"ok": {order_id, warehouse_id}} or {"say": why/which} for an allocation; None if the message isn't one.
        The godown is the one named; else the customer's route godown (the gaari loads there) when it holds the stock;
        never a card for a godown that can't hold it -- that is asked about instead."""
        def calc():
            if not contains("allocate", "reserve", "allocation", "ریزرو")(t):
                return None
            oid = _order_ref(t, repo, ("confirmed",))
            if not oid:
                c = _cust(t, repo)
                if c.ok and c.other is None:
                    return {"say": pick_order("allocate", c.id or "", c.name, repo, ("confirmed",))}
                return {"say": RP.ask_customer(c) if c.status == "ambiguous" else "Which order should I reserve stock for? Name the customer, e.g. 'Malik Agro ka order allocate karo'."}
            try:
                o = repo.get_order(oid)
            except Exception:
                return {"ok": {"order_id": oid, "warehouse_id": (warehouses_in(t, repo) or [default_wh])[0]}}     # (the tool reports the unknown id)
            cname = repo.get_customer(o.customer_id).name
            if o.status != "confirmed":
                return {"say": f"{cname}'s order ({order_label(repo, o)}) is {o.status}: only a confirmed order can have stock reserved"
                               + (" -- confirm it first." if o.status == "draft" else ".") + " Nothing was done."}
            named = warehouses_in(t, repo)
            home = _route_wh(o.customer_id)
            gname = lambda w: repo.get_warehouse(w).name  # noqa: E731
            if named:
                if _can_hold(o, named[0]):
                    return {"ok": {"order_id": oid, "warehouse_id": named[0]}}
                other = next((w.warehouse_id for w in repo.list_warehouses() if _can_hold(o, w.warehouse_id)), None)
                return {"say": f"{gname(named[0])} doesn't have enough for {cname}'s order ({order_label(repo, o)})"
                               + (f"; {gname(other)} does -- say 'allocate at {gname(other).split()[0]}'." if other else ".") + " Nothing was done."}
            order = [home] + [w.warehouse_id for w in repo.list_warehouses() if w.warehouse_id != home] if home else [default_wh] + [
                w.warehouse_id for w in repo.list_warehouses() if w.warehouse_id != default_wh]
            fits = [w for w in order if w and _can_hold(o, w)]
            if fits and (fits[0] == home or not home):
                return {"ok": {"order_id": oid, "warehouse_id": fits[0]}}
            have = "; ".join(f"{gname(w.warehouse_id)}: " + ", ".join(f"{repo.get_stock(w.warehouse_id, i.sku).available} {repo.get_product(i.sku).name}" for i in o.items)
                             for w in repo.list_warehouses())
            if fits:
                return {"say": f"{cname}'s gaari loads at {gname(home)}, which doesn't have enough for this order ({have}). "
                               f"Move the stock to {gname(home)} first, or say 'allocate at {gname(fits[0]).split()[0]}' if it will go from there. Nothing was done."}
            return {"say": f"No single godown has all of {cname}'s order ({order_label(repo, o)}): {have}. "
                           "Move stock first (e.g. 'multan se 20 urea vehari bhejo'), then allocate. Nothing was done."}
        return memo(("alloc", t), calc)

    def _plan(t: str) -> dict | None:
        """{"ok": create_dispatch_plan args} or {"say": why}: the route from the orders' customers, a vehicle that fits
        (free today if one is), and every order allocated at that route's godown -- else it says why, no card."""
        def calc():
            ids = ids_in(t, "ORD")
            if not ids or not contains("dispatch", "plan", "bhejo", "gaari", "gari", "truck", "load")(t):
                return None
            orders = []
            for oid in ids:
                try:
                    orders.append(repo.get_order(oid))
                except Exception:
                    return {"say": f"I can't find order {oid}. Nothing was done."}
            names = {o.order_id: repo.get_customer(o.customer_id).name for o in orders}
            bad = [o for o in orders if o.status != "allocated"]
            if bad:
                o = bad[0]
                return {"say": f"{names[o.order_id]}'s order is {o.status}: it has to be allocated (stock reserved at the godown) before it can go on a plan"
                               + (" -- say 'allocate' for it first." if o.status == "confirmed" else ".") + " Nothing was done."}
            rid = (ids_in(t, "R") or [""])[0]
            routes = {repo.get_customer(o.customer_id).route_id for o in orders}
            if not rid:
                if len(routes) != 1 or not next(iter(routes)):
                    return {"say": "These orders are on different routes -- plan each route separately. Nothing was done."}
                rid = next(iter(routes))
            try:
                route = repo.get_route(rid)
            except Exception:
                return {"say": f"There is no route {rid}. Nothing was done."}
            wrong = [o for o in orders if o.warehouse_id != route.warehouse_id]
            if wrong:
                o = wrong[0]
                return {"say": f"{names[o.order_id]}'s order is reserved at {repo.get_warehouse(o.warehouse_id).name}, but the {route.name} gaari loads at "
                               f"{repo.get_warehouse(route.warehouse_id).name}. Move it (cancel and re-allocate there) first. Nothing was done."}
            load = sum(o.load_units for o in orders)
            vid = (ids_in(t, "V") or [""])[0]
            if not vid:
                from munshi.domain.models import business_today
                busy = {p.vehicle_id for p in repo.list_plans(plan_date=business_today().isoformat()) if p.status in ("planned", "approved", "loaded")}
                fit = sorted((v for v in repo.list_vehicles() if v.capacity_units >= load), key=lambda v: (v.vehicle_id in busy, v.capacity_units))
                if not fit:
                    return {"say": f"No vehicle holds {load} units -- split the orders over two plans. Nothing was done."}
                vid = fit[0].vehicle_id
            return {"ok": {"route_id": rid, "vehicle_id": vid, "order_ids": ids, "plan_date": date_in(t) if re.search(r"\d{4}-\d{2}-\d{2}", t) else ""}}
        return memo(("plan", t), calc)

    def _load(t: str) -> dict | None:
        """{"ok": plan id} / {"say": which} for 'load / approve the plan': a DSP id, else the ONE plan waiting to load."""
        def calc():
            if not contains("approve", "load", "loading", "manzoor", "lod")(t):
                return None
            ids = ids_in(t, "DSP")
            if ids:
                return {"ok": ids[0]}
            from munshi.domain.models import business_today
            planned = repo.list_plans(status="planned")
            today = [p for p in planned if p.plan_date == business_today().isoformat()]
            pick = today if today else planned
            if len(pick) == 1:
                return {"ok": pick[0].plan_id}
            if not pick:
                return {"say": "No dispatch plan is waiting to be loaded. Nothing was done."}
            rows = " ".join(f"{k}) {repo.get_route(p.route_id).name} on {repo.get_vehicle(p.vehicle_id).plate}, {len(p.order_ids)} order(s), {p.plan_date}"
                            for k, p in enumerate(pick[:5], 1))
            return {"say": f"{len(pick)} plans are waiting to load -- which one? {rows}. Say e.g. 'load DSP-...'."}
        return memo(("load", t), calc)

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

    ok_of = lambda fn: (lambda t: "ok" in (fn(t) or {}))  # noqa: E731
    rules = [
        Rule(lambda t: "ok" in (_load(t) or {}) and not ids_in(t, "ORD"), "approve_dispatch_plan", lambda t: {"plan_id": _load(t)["ok"]}),
        Rule(lambda t: move_w(t) and _transfer(t) is not None, "transfer_stock", lambda t: _transfer(t)),
        Rule(lambda t: (contains("restock", "adjust", "received")(t) or write_off(t) or increase(t)) and _adjust(t) is not None and len(warehouses_in(t, repo)) < 2,
             "adjust_stock", lambda t: _adjust(t)),
        Rule(ok_of(_alloc), "allocate_order", lambda t: _alloc(t)["ok"]),
        Rule(ok_of(_plan), "create_dispatch_plan", lambda t: _plan(t)["ok"]),
        Rule(lambda t: contains("dispatch", "suggest", "today", "aaj")(t) and not write_shaped(t) and not _all_stock(t)
             and _plan(t) is None and _alloc(t) is None, "suggest_dispatch",
             lambda t: {"plan_date": (re.findall(r"\d{4}-\d{2}-\d{2}", t) or [""])[0]}),
        Rule(lambda t: bool(_sku(t, repo)) and (_STOCKQ(t) or contains("godown", "گودام")(t)) and not write_shaped(t), "get_stock", lambda t: {"sku": _sku(t, repo)}),
        # a stock question about no one product: every product's stock ('stocks kitne baqi hein', 'aj ka stock count', 'for all the products?')
        Rule(lambda t: _all_stock(t) and not write_shaped(t), "get_stock", lambda t: {"sku": ""}),
        Rule(contains("confirmed", "allocated", "orders"), "list_orders", lambda t: {"status": "confirmed" if "confirmed" in t.lower() else "allocated"}),
    ]

    def fallback(t: str, system: str) -> str | None:
        urdu = is_urdu(t)
        for fn in (_load, _alloc, _plan):
            said = (fn(t) or {}).get("say")
            if said and not (fn is _load and ids_in(t, "ORD")):
                return said
        if contains("transfer", "move", "shift")(t) or (move_w(t) and len(warehouses_in(t, repo)) == 2):
            return "Which product, how many, and from which godown to which? e.g. 'multan se 20 urea vehari bhejo'."
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
def todays_stop(repo, cid: str):
    """This customer's ONE open stop on today's loaded plans (None if none, or more than one)."""
    if not cid:
        return None
    from munshi.domain.models import business_today
    try:
        stops = [s for p in repo.list_plans(plan_date=business_today().isoformat()) if p.status in ("approved", "loaded")
                 for s in repo.list_stops(p.plan_id) if s.customer_id == cid and s.status == "pending"]
    except Exception:
        return None
    return stops[0] if len(stops) == 1 else None


def build_delivery_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None, guarded: bool | None = None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    tools = [T["get_plan"], T["list_stops"], T["get_order"], T["close_stop"]]
    prompts = {r: f"You are the Delivery Munshi for {B}, on the driver's phone. Show the stops in order and close each one with what was delivered, what came back, cash taken, and the customer's OTP. Never close a stop without the OTP." for r in ROLES}
    close_words = contains("close", "delivered", "deliver", "de diya", "diya", "band", "utar", "utar diya", "pohncha", "ڈیلیور", "دے دیا")

    def _named_stop(t: str):
        """The stop a message means by the customer's name ('chaudhry farms pe maal de diya'): their one open stop today."""
        def calc():
            if ids_in(t, "STP"):
                return None
            c = _cust(t, repo)
            return todays_stop(repo, c.id or "") if c.ok and c.other is None else None
        return memo(("nstop", t), calc)

    def _close(t: str):
        st = _named_stop(t)
        return memo(("close", t), lambda: analyse_close(f"{t} {st.stop_id}" if st else t, repo))

    about_stop = contains("kitne paise", "kitna paisa", "paise lene", "kitne lene", "kitna lena", "raqam", "amount", "collect", "address", "pata", "kahan",
                          "location", "kya dena", "kitna dena", "bill kitna", "kya maal", "items", "maal kya", "پتہ", "پیسے")

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
        Rule(lambda t: close_words(t) and bool(ids_in(t, "STP") or _named_stop(t)) and not _close(t).problems, "close_stop", _close_args),
        # the amount to collect / the address at a customer's stop today: that plan's stops (the reply picks out theirs)
        Rule(lambda t: about_stop(t) and _named_stop(t) is not None and not close_words(t), "list_stops", lambda t: {"plan_id": _named_stop(t).plan_id}),
        Rule(lambda t: bool(ids_in(t, "DSP")), "list_stops", lambda t: {"plan_id": ids_in(t, "DSP")[0]}),
        Rule(lambda t: next_words(t) and not ids_in(t, "STP") and not contains("close", "band", "otp")(t) and len(_todays_plans()) == 1, "list_stops",
             lambda t: {"plan_id": _todays_plans()[0].plan_id}),
    ]

    def fallback(t: str, system: str) -> str | None:
        stop = (ids_in(t, "STP") or [""])[0] or (_named_stop(t).stop_id if _named_stop(t) else "")
        if stop and close_words(t) and not ids_in(t, "STP"):
            probs = _close(t).problems
            name = _cust(t, repo).name
            if probs == ["the customer's OTP code"]:
                return RP.Ask(f"To close {name}'s stop I need the delivery code the customer has, e.g. 'code 1234'.", "otp")
            if probs:
                return f"To close {name}'s stop I need {probs[0]}. e.g. '{name} pe sab de diya, cash 50000, code 1234'."
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
                         "wusool", "raqam", "de gaya", "de gya", "de gaye", "de ke gaya", "de kar gaya", "جمع", "ادائیگی", "بینک", "وصول")
    expense_words = contains("expense", "kharcha", "kharch", "diesel", "petrol", "fuel", "salary", "tankhwah", "rent", "kiraya", "bijli", "repair",
                             "mazdoor", "mazdoori", "labour", "tyre", "puncture", "marammat", "chai", "chai pani", "خرچہ", "مرمت")
    credit_words = contains("credit note", "credit", "refund", "waive", "maaf", "chhoot", "chhot", "choot", "chut", "riayat", "riyayat", "رعایت", "چھوٹ")
    cash_q = contains("cashbook", "cash book", "cash today", "rokar", "کیش بک", "driver ka cash", "driver cash", "cash pura", "poora cash", "pura cash",
                      "cash short", "short tha", "kitna short", "cash kam", "kharche", "kharchay", "kharchey", "kharcha kitna", "kharche kitne", "expenses today",
                      "aaj ke kharche", "aaj ka kharcha", "kitna kharcha", "kitne kharche", "خرچے")

    def _bounced(t: str) -> dict | None:
        """The receipt a 'X ka cheque bounce ho gaya' means: the customer's ONE cheque receipt (of that amount, when one is
        said) that hasn't been reversed. {"ok": entry id} or {"say": why / which}."""
        def calc():
            c = _cust(t, repo)
            if not is_bounce(t) or not c.ok or c.other is not None or _khata_ids(t, repo):
                return None
            amt = amount_in(re.sub(r"(?i)\b(cheque|check|chq)\s*(no\.?|number|#)\s*\d+", " ", t)).amount
            pays = [e for e in repo.ledger_for(c.id or "") if e.kind == "payment" and e.amount < 0 and not e.reversal_of
                    and (e.method or "") == "cheque" and not repo.reversal_of_ledger(e.entry_id)]
            if amt is not None:
                pays = [e for e in pays if abs(-e.amount - amt) < 0.01]
            if len(pays) == 1:
                return {"ok": pays[0].entry_id}
            if not pays:
                return {"say": f"I can't find a cheque receipt from {c.name}" + (f" for Rs {amt:,.0f}" if amt else "") + " that could be reversed. Nothing was done."}
            rows = " ".join(f"{k}) {e.entry_id} Rs {-e.amount:,.0f} ({str(e.created_at)[:10]})" for k, e in enumerate(pays[:5], 1))
            return {"say": f"{c.name} has {len(pays)} cheque receipts -- which one bounced? {rows}. Say e.g. '{pays[0].entry_id} cheque bounce, reverse karo'."}
        return memo(("bounced", t), calc)

    def _pay_ref(t: str) -> str:
        """A payment's reference: the cheque / transaction number when one is given, else the message itself."""
        m = re.search(r"(?i)\b(cheque|check|chq|chek|tid|trx|txn|ref)\s*(?:no\.?|number|#|:)?\s*([A-Za-z0-9-]{3,})", t)
        bank = re.search(r"\b(HBL|UBL|MCB|ABL|NBP|Meezan|Allied|Faysal|Askari|Alfalah|BOP|JS)\b", t, re.I)
        if m:
            return f"{m.group(1).lower()} {m.group(2)}" + (f" ({bank.group(1).upper()})" if bank else "")
        return t[:60]

    def _one_customer(t):
        r = _cust(t, repo)
        return r.ok and r.other is None

    def _amt_ok(t):
        return _amount(t).amount is not None

    def _deposit_amount(t):
        return amount_in(t.replace(ids_in(t, "DSP")[0], " ") if ids_in(t, "DSP") else t)

    def _run_of(t: str) -> str:
        """The dispatch plan a driver's hand-in is for: a DSP id, else today's ONE loaded run that the message's route,
        godown or vehicle words fit ('vehari gaari ka') -- or today's only loaded run when it names none."""
        ids = ids_in(t, "DSP")
        if ids:
            return ids[0]
        from munshi.domain.models import business_today
        runs = [p for p in repo.list_plans(plan_date=business_today().isoformat()) if p.status in ("approved", "loaded")]
        f = fold(t)

        def fits(p) -> bool:
            names = []
            for fn, rid in ((repo.get_route, p.route_id), (repo.get_warehouse, p.warehouse_id)):
                try:
                    names += [w for w in words(fold(fn(rid).name)) if len(w) >= 4 and w not in ("road", "godown", "north", "south")]
                except Exception:
                    pass
            try:
                names.append(fold(repo.get_vehicle(p.vehicle_id).plate))
            except Exception:
                pass
            return any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", f) for n in names)
        hit = [p for p in runs if fits(p)]
        pick = hit if hit else (runs if not any(fits(p) for p in repo.list_plans(limit=20)) else [])
        return pick[0].plan_id if len(pick) == 1 else ""

    rules = [
        Rule(lambda t: _REVERSE(t) and bool(ids_in(t, "EXP")), "reverse_expense", lambda t: {"expense_id": ids_in(t, "EXP")[0], "reason": t[:120]}),
        Rule(lambda t: _REVERSE(t) and bool(_khata_ids(t, repo)), "reverse_ledger_entry", lambda t: {"entry_id": _khata_ids(t, repo)[0], "reason": t[:120]}),
        Rule(lambda t: "ok" in (_bounced(t) or {}), "reverse_ledger_entry", lambda t: {"entry_id": _bounced(t)["ok"], "reason": t[:120]}),
        Rule(lambda t: credit_words(t) and _one_customer(t) and _amt_ok(t), "credit_note",
             lambda t: {"customer_id": _cust(t, repo).id, "amount": _amount(t).amount, "reason": t[:80]}),
        Rule(lambda t: contains("deposit", "handed", "counted", "jama")(t) and bool(ids_in(t, "DSP")) and _deposit_amount(t).amount is not None, "record_deposit",
             lambda t: {"plan_id": ids_in(t, "DSP")[0], "amount_counted": _deposit_amount(t).amount, "counted_by": "cashier"}),
        # 'driver ne 18000 jama karwaye he vehari gaari ka': the hand-in for today's run the message points at
        Rule(lambda t: contains("driver", "gaari", "gari", "gaadi", "truck")(t) and contains("deposit", "handed", "counted", "jama", "jama karwaye", "de gaya", "diye")(t)
             and not _cust(t, repo).ok and bool(_run_of(t)) and _amount(t).amount is not None, "record_deposit",
             lambda t: {"plan_id": _run_of(t), "amount_counted": _amount(t).amount, "counted_by": "cashier"}),
        Rule(lambda t: expense_words(t) and not _cust(t, repo).ok and _amt_ok(t), "record_expense",
             lambda t: {"category": _expense_category(t), "amount": _amount(t).amount, "note": t[:80], "method": method_in(t)}),
        Rule(lambda t: pay_words(t) and not is_bounce(t) and not credit_words(t) and _one_customer(t) and _amt_ok(t), "record_payment",
             lambda t: {"customer_id": _cust(t, repo).id, "amount": _amount(t).amount, "method": method_in(t), "ref": _pay_ref(t)}),
        Rule(lambda t: cash_q(t) and not _cust(t, repo).ok, "cashbook", lambda t: {"day": (re.findall(r"\d{4}-\d{2}-\d{2}", t) or [""])[0]}),
        Rule(lambda t: _KHATA(t) and not credit_words(t) and bool(_customer_or_recent(t, repo)), "get_customer_khata",
             lambda t: {"customer_id": _customer_or_recent(t, repo)}),
        Rule(lambda t: contains("digest", "today", "summary", "close the day", "aaj", "hisaab", "hisab")(t) and not credit_words(t), "get_digest", lambda t: {}),
    ]

    def fallback(t: str, system: str) -> str | None:
        urdu = is_urdu(t)
        if (_bounced(t) or {}).get("say"):
            return _bounced(t)["say"]
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

    send_w = contains("send", "bhej", "bhejo", "bhej do", "bhejdo", "bhijwa", "bhijwa do", "bhijwao", "forward", "chala do", "بھیج", "بھیجو")
    draft_w = contains("draft", "bana do", "banao", "bana den", "tayyar", "tayar", "likh do", "likho", "ready")
    promise_q = lambda t: promise_words(t) and _amount(t).amount is None and (  # noqa: E731
        contains("kab", "kya", "kia", "kitna", "kitne", "pura", "poora", "hua", "hui", "tha", "thi", "check", "when", "what", "کب", "کیا")(t) or "?" in t)

    def _drafted_rem(t: str) -> str:
        """The reminder 'send the reminder' means: the named (or remembered) customer's latest drafted, unsent one; with
        nobody named, the one this conversation drafted, if it is still unsent."""
        c = _cust(t, repo)
        rems = [r for r in repo.list_reminders("drafted")]
        if c.ok and c.other is None:
            mine = [r for r in rems if r.customer_id == c.id]
            return mine[0].reminder_id if mine else ""
        if c.status != "none":
            return ""
        rid = ids_in(t, "REM")[0] if ids_in(t, "REM") else _recent(r"REM-[A-Z0-9]{8}", "reminder_id")
        return rid if rid and any(r.reminder_id == rid for r in rems) else ""

    rules = [
        Rule(broken, "broken_promises", lambda t: {}),
        # a question about a promise already made ('X ka waada kab ka tha?', 'pura hua?'): their khata shows it -- never a new promise
        Rule(lambda t: promise_q(t) and _cust(t, repo).ok, "get_customer_khata", lambda t: {"customer_id": _cust(t, repo).id}),
        Rule(lambda t: promise_words(t) and _promise_ready(t), "log_promise",
             lambda t: {"customer_id": _cust(t, repo).id, "amount": _amount(t).amount, "promised_date": date_in(t)}),
        Rule(lambda t: contains("send")(t) and bool(ids_in(t, "REM")), "send_reminder", lambda t: {"reminder_id": ids_in(t, "REM")[0]}),
        # 'send the reminder' / 'ab bhej do reminder' when one is already drafted: send THAT one, never a second draft
        Rule(lambda t: send_w(t) and not draft_w(t) and bool(_drafted_rem(t)), "send_reminder", lambda t: {"reminder_id": _drafted_rem(t)}),
        Rule(lambda t: contains("remind", "reminder", "yaad", "yaad dehani", "یاد دہانی")(t) and _cust(t, repo).ok, "draft_reminder",
             lambda t: {"customer_id": _cust(t, repo).id, "tier": _tier(t)}),
        Rule(lambda t: contains("remind", "reminders", "collections", "chase", "wasooli")(t) and _cust(t, repo).status == "none"
             and not contains("list", "لسٹ", "kis kis", "kaun kaun", "kon kon", "who", "report")(t),
             "draft_due_reminders", lambda t: {"min_days_overdue": max(1, int_in(t) or 1)}),
        Rule(lambda t: _KHATA(t) and _cust(t, repo).ok, "get_customer_khata", lambda t: {"customer_id": _cust(t, repo).id}),
        Rule(lambda t: bool(re.search(r"sab se (zyada|ziada|zaida)|\b(most|largest|biggest)\b", fold(t))) and _cust(t, repo).status == "none",
             "aging_report", lambda t: {}),
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
                return RP.Ask(f"How much did {r.name} promise" + ("?" if date_in(t) else ", and by when?") + " e.g. '50000, 2 october tak'.", "amount")
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
        Rule(contains("profit", "munafa", "net", "margin", "kamaya", "kamai", "kamaaya", "منافع"), "profit_summary", _range),
        Rule(contains("best seller", "best selling", "bikne wali", "bikne wala", "zyada bika", "zyada biki", "bikta", "bikti"), "sales_report", _range),
        Rule(lambda t: bool(re.search(r"sab se (zyada|ziada|zaida)\b.{0,30}\b(kis|kaun|kon)\b.{0,25}\b(ne|khareed\w*|kharid\w*|liya|lia)\b", fold(t))),
             "top_customers", lambda t: {"days": 30}),
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
