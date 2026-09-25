"""Code checks a real model's tool arguments against the user's message before anything runs.

The principle: the model decides WHAT to do; code decides WHO, HOW MUCH, and WHETHER it's allowed.
A real model can pick the right tool and still pass the wrong customer ('Malik Seeds ko 8 makai' ->
create_order for C-001: the first 'Malik' it found). So every tool call a real model makes is read here,
in code, against the message it answers, with the same deterministic layer the offline munshis use
(llm/resolve.py, llm/parse.py):

  WHO       the customer / supplier the MESSAGE names must resolve confidently (resolve.py's rule) to the
            id in the arguments. Ambiguous -> ask which, naming the candidates; a different id -> ask which
            of the two; two customers in one message -> ask for one at a time. A message that names nobody
            may lean on the thread's history only through a pronoun ('isko', 'uska'), exactly like the
            offline layer; otherwise ask.
  HOW MUCH  order lines must equal what parse.py reads from the message when it reads any (and a message
            parse.py finds problems in -- a correction, a range, an unknown product -- is asked about);
            an amount must equal amount_in(); every other quantity, amount, delta or price must be a number
            the message states; a payment method must be the one the message says (cash when it says none);
            a promise date must be the date the message gives.
  WHICH     a record reference (ORD-, DSP-, STP-, REM-, RCP-, EXP-, PUR-, route, vehicle) must appear in the
            message or earlier in this conversation -- never one the model found or recalled this turn;
            a godown must be one the message names (or the default when it names none); a stop is closed
            only with the OTP the message gives.

Every write-capable tool (anything not READ_ONLY in the risk registry, including the OTP-gated close_stop)
gets the full check. A read gets the WHO check only when the message names a customer or supplier.
A failed check drops the model's tool calls and replaces its message with ONE clarifying question
(llm/replies.py), marked response_metadata[GUARD_KEY]; the gated call never reaches the approval gate, so
no card is raised, and no extra model call is spent. Runs as agent middleware placed after the approval
middleware, so its after_model hook runs first (LangChain runs after_model hooks in reverse order)."""
from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from munshi.llm import replies as RP
from munshi.llm.parse import (
    amount_in,
    analyse_order,
    customer_resolution,
    date_in,
    method_in,
    numbers_said,
    otp_in,
    sku_in,
    supplier_resolution,
    warehouses_in,
)
from munshi.llm.resolve import Candidate, Resolution
from munshi.llm.text import is_urdu
from munshi.safety.risk import RiskTier, risk_of

log = logging.getLogger("munshi.guard")

GUARD_KEY = "munshi_guard"

# Earlier lines of this conversation, as the user saw them (oldest first), set by the platform around a
# model-engine turn. Grounds references and pronouns in what was actually said, across both engines.
_HISTORY: ContextVar[list[str] | None] = ContextVar("munshi_guard_history", default=None)


def set_history(lines: list[str]):
    return _HISTORY.set(list(lines))


def reset_history(token) -> None:
    _HISTORY.reset(token)


CUSTOMER_TOOLS = {"create_order", "record_payment", "credit_note", "draft_reminder", "log_promise", "get_customer_khata"}
SUPPLIER_TOOLS = {"record_purchase", "pay_supplier", "supplier_khata"}
_REF_KEYS = {"order_id": ("order", "ORD-..."), "plan_id": ("dispatch plan", "DSP-..."), "stop_id": ("stop", "STP-..."),
             "reminder_id": ("reminder", "REM-..."), "entry_id": ("entry", "RCP-... or SPY-..."), "expense_id": ("expense", "EXP-..."),
             "purchase_id": ("purchase", "PUR-..."), "route_id": ("route", "R-MULTAN-N"), "vehicle_id": ("vehicle", "V-01")}
_NUM_KEYS = ("qty", "delta", "unit_cost", "unit_price", "paid_amount", "cash_collected", "min_days_overdue")
_AMOUNT_KEYS = ("amount", "amount_counted")
_METHOD_TOOLS = {"record_payment", "record_expense", "pay_supplier"}
_METHODS = {"cash": "cash", "bank": "bank", "banktransfer": "bank", "transfer": "bank", "online": "bank", "jazzcash": "jazzcash",
            "easypaisa": "easypaisa", "cheque": "cheque", "check": "cheque"}


def _money(v: float) -> str:
    return f"Rs {v:,.0f}" if float(v).is_integer() else f"Rs {v:,.2f}"


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _items(raw) -> dict[str, float] | None:
    """{SKU: total qty} for a list of {"sku", "qty"}; None if malformed."""
    if not isinstance(raw, list):
        return None
    out: dict[str, float] = {}
    for it in raw:
        if not isinstance(it, dict):
            return None
        q = _float(it.get("qty"))
        sku = str(it.get("sku") or "").strip().upper()
        if q is None or not sku:
            return None
        out[sku] = out.get(sku, 0) + q
    return out


def _fmt_items(items: dict[str, float]) -> str:
    return ", ".join(f"{int(q) if float(q).is_integer() else q} × {s}" for s, q in items.items())


def _in(value: str, texts: list[str]) -> bool:
    v = re.escape(value.strip().upper())
    return bool(v) and any(re.search(rf"(?<![A-Z0-9-]){v}(?![A-Z0-9])", t.upper()) for t in texts)


class _Ctx:
    def __init__(self, text: str, repo, prior: list[BaseMessage], history: list[str]) -> None:
        self.text, self.repo, self.urdu = text, repo, is_urdu(text)
        self.history = history
        prior_txt = []
        for m in prior[-12:]:
            if isinstance(m, AIMessage):
                prior_txt += [json.dumps(tc.get("args") or {}, default=str, ensure_ascii=False) for tc in m.tool_calls]
                prior_txt.append(str(m.content))
            elif isinstance(m, (ToolMessage, HumanMessage)):
                prior_txt.append(str(m.content))
        self.earlier = history[-8:] + prior_txt              # what was said before this message, either engine
        self._memo: dict = {}

    def memo(self, key, fn):
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    def customer(self) -> Resolution:
        return self.memo("c", lambda: customer_resolution(self.text, self.repo))

    def supplier(self) -> Resolution:
        return self.memo("s", lambda: supplier_resolution(self.text, self.repo))

    def numbers(self) -> set[float]:
        return self.memo("n", lambda: numbers_said(self.text, self.repo))


def _recent_entity(ctx: _Ctx, kind: str) -> str:
    """The thread-history resolution the offline layer uses: only for a pronoun ('isko', 'uska', 'ye wala'), the
    most recent customer / supplier named in the last few lines of this conversation."""
    from munshi.agents.specialists import _PRONOUN
    if not _PRONOUN(ctx.text):
        return ""
    pat = r"\bC-\d{3,}\b" if kind == "customer" else r"\bS-\d{3,}\b"
    res = customer_resolution if kind == "customer" else supplier_resolution
    for line in reversed(ctx.history[-6:]):
        found = re.findall(pat, line)
        if found:
            return found[0]
        r = res(line, ctx.repo)
        if r.ok and r.other is None:
            return r.id or ""
    return ""


def _check_entity(kind: str, arg: Any, ctx: _Ctx, write: bool) -> str | None:
    res = ctx.customer() if kind == "customer" else ctx.supplier()
    ask = RP.ask_customer if kind == "customer" else RP.ask_supplier
    got = str(arg or "").strip().upper()
    if res.status == "ambiguous":
        return ask(res, ctx.urdu)
    if res.ok:
        if res.other is not None and kind == "customer":
            return RP.t("two_customers", ctx.urdu, a=res.name, b=res.other.name)
        if got == (res.id or "").upper():
            return None
        cands = [c for c in res.candidates if c.id == res.id][:1] or [Candidate(res.id or "", res.name, 1.0)]
        rec = _name_of(kind, got, ctx.repo)
        if rec:
            cands.append(Candidate(got, rec, 0.0))
        return ask(Resolution("ambiguous", None, "", cands), ctx.urdu)
    # the message names nobody: a read may go ahead; a write only for a pronoun that points at the same record
    if not write:
        return None
    if got and got == _recent_entity(ctx, kind):
        return None
    return ask(res, ctx.urdu)


def _name_of(kind: str, rid: str, repo) -> str:
    try:
        return (repo.get_customer(rid) if kind == "customer" else repo.get_supplier(rid)).name
    except Exception:
        return ""


def _check_items(name: str, args: dict, ctx: _Ctx) -> str | None:
    got = _items(args.get("items"))
    if not got:
        return RP.t("no_items", ctx.urdu)
    if any(q <= 0 or not float(q).is_integer() for q in got.values()):
        return RP.t("no_items", ctx.urdu)
    op = ctx.memo("order", lambda: analyse_order(ctx.text, ctx.repo))
    problems = [p for p in op.problems if not p.startswith("two customers")] if name == "record_purchase" else list(op.problems)
    if op.items or problems or op.unknown:
        if name == "create_order" and (problems or op.unknown):
            return RP.ask_order(op, ctx.urdu)
        if op.unknown:
            return RP.t("unknown_item", ctx.urdu, what=op.unknown[0])
        if problems:
            return RP.t("fix_qty", ctx.urdu, why=problems[0])
        want = _items(op.items) or {}
        if want != got:
            return RP.t("confirm_items", ctx.urdu, items=_fmt_items(want))
        return None
    # parse.py read no lines at all: every quantity must still be a number the message states, every SKU a real product
    nums = ctx.numbers()
    for sku, q in got.items():
        if q not in nums:
            return RP.t("no_items", ctx.urdu)
        try:
            ctx.repo.get_product(sku)
        except Exception:
            return RP.t("unknown_item", ctx.urdu, what=sku)
    return None


def _check_amount(key: str, v: Any, ctx: _Ctx) -> str | None:
    got = _float(v)
    ap = ctx.memo("amt", lambda: amount_in(ctx.text))
    if ap.amount is None:
        return RP.t("how_much", ctx.urdu) if ap.problem == "how much?" else f"{ap.problem[:1].upper()}{ap.problem[1:]}."
    if got is None or abs(got - ap.amount) > 0.01:
        return RP.t("confirm_amount", ctx.urdu, amount=_money(ap.amount))
    return None


def _check_refs(args: dict, ctx: _Ctx) -> str | None:
    texts = [ctx.text] + ctx.earlier
    for key, (what, example) in _REF_KEYS.items():
        if key not in args:
            continue
        v = str(args.get(key) or "").strip()
        if key in ("route_id", "vehicle_id") and not v:
            continue
        if not v or not _in(v, texts):
            return RP.t("which_ref", ctx.urdu, what=what, example=example)
    for oid in args.get("order_ids") or []:
        if not _in(str(oid), texts):
            return RP.t("which_ref", ctx.urdu, what="order", example="ORD-...")
    return None


def _check_godowns(name: str, args: dict, ctx: _Ctx) -> str | None:
    named = warehouses_in(ctx.text, ctx.repo)
    if name == "transfer_stock":
        pair = {str(args.get("from_warehouse") or ""), str(args.get("to_warehouse") or "")}
        return None if len(named) == 2 and pair == set(named) else RP.t("which_godown", ctx.urdu)
    wid = str(args.get("warehouse_id") or "").strip()
    if not wid or wid in named:
        return None
    if not named:
        try:
            if wid == ctx.repo.default_warehouse_id():
                return None
        except Exception:
            pass
    return RP.t("which_godown", ctx.urdu)


def _check_numbers(args: dict, ctx: _Ctx) -> str | None:
    nums = ctx.numbers()
    for key in _NUM_KEYS:
        v = _float(args.get(key)) if key in args else None
        if v is None or v == 0 or (key == "min_days_overdue" and v == 1):
            continue
        if abs(v) not in nums:
            return RP.t("not_backed", ctx.urdu, why=f"{key.replace('_', ' ')} {v:g}")
    for lk in ("items", "delivered_items", "returned_items"):
        for it in args.get(lk) or []:
            if isinstance(it, dict):
                # order / purchase quantities were checked line by line against parse.py ('2 dozen' -> 24) in _check_items
                for key in (("unit_cost", "unit_price") if lk == "items" else ("qty", "unit_cost", "unit_price")):
                    v = _float(it.get(key))
                    if v and abs(v) not in nums:
                        return RP.t("not_backed", ctx.urdu, why=f"{key.replace('_', ' ')} {v:g}")
    return None


def check_call(name: str, args: dict, text: str, repo, prior: list[BaseMessage] | None = None, history: list[str] | None = None) -> str | None:
    """None if the message backs this call; otherwise the ONE question to ask instead of acting."""
    ctx = _Ctx(text, repo, prior or [], history if history is not None else (_HISTORY.get() or []))
    args = args if isinstance(args, dict) else {}
    try:
        write = risk_of(name) != RiskTier.READ_ONLY
    except ValueError:
        return RP.t("not_backed", ctx.urdu, why=f"unknown action {name}")
    if name in CUSTOMER_TOOLS or "customer_id" in args:
        if write or ctx.customer().status != "none":
            q = _check_entity("customer", args.get("customer_id"), ctx, write)
            if q:
                return q
    if name in SUPPLIER_TOOLS or "supplier_id" in args:
        if write or ctx.supplier().status != "none":
            q = _check_entity("supplier", args.get("supplier_id"), ctx, write)
            if q:
                return q
    if not write:
        return None
    if name in ("create_order", "record_purchase"):
        q = _check_items(name, args, ctx)
        if q:
            return q
    for key in _AMOUNT_KEYS:
        if key in args:
            q = _check_amount(key, args.get(key), ctx)
            if q:
                return q
    if name in _METHOD_TOOLS:
        got = _METHODS.get(re.sub(r"[\s_-]", "", str(args.get("method") or "cash").lower()), "?")
        if got != method_in(text):
            return RP.t("which_method", ctx.urdu)
    if name == "log_promise":
        want = date_in(text)
        if not want or str(args.get("promised_date") or "") != want:
            return RP.t("which_date", ctx.urdu)
    if name in ("adjust_stock", "transfer_stock"):
        want = sku_in(text, repo)
        if not want or str(args.get("sku") or "").upper() != want.upper():
            return RP.t("which_product", ctx.urdu)
    if name == "close_stop":
        otp = otp_in(text)
        if not otp or str(args.get("otp") or "").strip() != otp:
            return RP.t("which_otp", ctx.urdu)
    q = _check_refs(args, ctx)
    if q:
        return q
    if any(k in args for k in ("warehouse_id", "from_warehouse", "to_warehouse")):
        q = _check_godowns(name, args, ctx)
        if q:
            return q
    return _check_numbers(args, ctx)


def hints(text: str, repo) -> str:
    """What code already read in the message, handed to the model up front: the customer / supplier it names (or the
    candidates when it is ambiguous) and any order lines parse.py read cleanly. Saves the model a lookup, and the
    guard checks the same readings, so following them is always the path that gets a card."""
    out = []
    c, s = customer_resolution(text, repo), supplier_resolution(text, repo)
    for kind, r in (("customer", c), ("supplier", s)):
        if r.ok and (kind == "customer" or not c.ok or set(r.evidence) - set(c.evidence)):
            out.append(f"{kind} {r.name} = {r.id}")
        elif r.status == "ambiguous":
            out.append(f"{kind} is ambiguous: " + " or ".join(f"{x.name} ({x.id})" for x in r.candidates) + " -- ask which")
    op = analyse_order(text, repo)
    if op.items and not op.problems and not op.unknown:
        out.append("items " + ", ".join(f"{i['qty']} x {i['sku']}" for i in op.items))
    elif not op.items:
        amt = amount_in(text).amount
        if amt is not None and amt >= 100:
            out.append(f"amount {amt:g} rupees")
    return ("Code read this message as: " + "; ".join(out) + ".") if out else ""


def _last_human(messages: list[BaseMessage]) -> int:
    return max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)


def guard_refusal(msg: Any) -> dict | None:
    """What the guard refused on this message, if it replaced it."""
    return (getattr(msg, "response_metadata", None) or {}).get(GUARD_KEY) if isinstance(msg, AIMessage) else None


class EntityGuard(AgentMiddleware):
    """Checks each tool call a real model proposes (check_call) before the approval gate sees it."""

    def __init__(self, repo) -> None:
        super().__init__()
        self.repo = repo

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state["messages"]
        ai = messages[-1] if messages and isinstance(messages[-1], AIMessage) else None
        if ai is None or not ai.tool_calls:
            return None
        h = _last_human(messages)
        text = str(messages[h].content) if h >= 0 else ""
        prior = messages[:h] if h >= 0 else []
        for tc in ai.tool_calls:
            try:
                q = check_call(tc["name"], tc.get("args") or {}, text, self.repo, prior)
            except Exception:                                   # a checking bug must never let a call through
                log.exception("guard failed on %s", tc.get("name"))
                q = RP.t("not_backed", is_urdu(text), why="it couldn't be checked")
            if q:
                log.warning("guard refused %s(%s) for %r: %s", tc["name"], json.dumps(tc.get("args"), default=str)[:200], text[:80], q)
                kw = {k: v for k, v in (ai.additional_kwargs or {}).items() if k not in ("tool_calls", "function_call")}
                meta = dict(ai.response_metadata or {}) | {GUARD_KEY: {"tool": tc["name"], "args": tc.get("args") or {}, "question": q}}
                blocked = ai.model_copy(update={"content": q, "tool_calls": [], "invalid_tool_calls": [], "additional_kwargs": kw, "response_metadata": meta})
                return {"messages": [blocked]}
        return None
