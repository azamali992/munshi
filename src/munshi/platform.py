"""MunshiPlatform: one business's agents, one entry point for chat, approvals
and the daily close.

handle_message()  routes a message to a specialist and runs one turn; if the
                  specialist pauses on a gated tool, a PendingApproval is
                  persisted and returned instead of the action running.
resolve()         a human with the right role -- and, unless they are the owner,
                  not the person who asked for it -- approves or rejects it;
                  the specialist resumes from its checkpoint (SQLite when the
                  platform has a checkpoint_path, so a restart loses nothing).
                  If the resumed turn asks for another gated action, that
                  becomes a new card rather than a silent "Done.".

One gated call per step: see safety.middleware.OneGatedCallPerStep. A card is
never raised for a call whose record reference is blank.

card()            what the human reads before approving (CardBuilder): names,
                  lines, total, and the before/after effect, read-only.
A message sent while a card waits never reaches the paused graph: a plain
read-only question goes to the Report munshi; anything else gets the waiting
card back (Reply.waiting) so it can be decided right there.
"""
from __future__ import annotations

import inspect
import logging
import re
import sqlite3
import threading
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from munshi.agents.manager import CLARIFY, build_manager, classify
from munshi.agents.specialists import BUILDERS
from munshi.channels import build_channel, deliver_outbox
from munshi.domain.models import discounted_paisa, to_paisa, to_rupees, today_iso
from munshi.domain.repository import MunshiRepository
from munshi.domain.seed import seeded_repository
from munshi.observability.tracing import TurnTrace, configure_tracking, trace_turn
from munshi.safety.middleware import DEFERRED_KEY
from munshi.safety.risk import RiskTier, approval_refusal, approver_for, risk_of, same_person, stricter_role
from munshi.tools.core import MunshiTools

log = logging.getLogger("munshi.platform")


@dataclass
class PendingApproval:
    approval_id: str
    thread_id: str
    specialist: str
    tool: str
    args: dict
    tier: str
    needs_role: str
    requested_by_role: str
    requested_by: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def describe(self) -> str:
        a = self.args
        t = self.tool
        money = lambda v: f"Rs {float(v or 0):,.0f}"   # noqa: E731
        if t == "create_order":
            lines = ", ".join(f"{i['qty']} × {i['sku']}" for i in a.get("items", []))
            return f"Create order for {a.get('customer_id')}: {lines}"
        if t == "confirm_order": return f"Confirm order {a.get('order_id')}" + (" — over credit limit" if self.needs_role == "owner" else "")
        if t == "cancel_order": return f"Cancel order {a.get('order_id')} — {a.get('reason', '')[:40]}"
        if t == "allocate_order": return f"Allocate {a.get('order_id')} at {a.get('warehouse_id') or 'default godown'}"
        if t == "create_dispatch_plan": return f"Plan {a.get('route_id')} on {a.get('vehicle_id')} with {len(a.get('order_ids', []))} order(s)"
        if t == "approve_dispatch_plan": return f"Load and dispatch plan {a.get('plan_id')} — stock leaves the godown"
        if t == "adjust_stock": return f"Adjust {a.get('sku')} at {a.get('warehouse_id')} by {int(a.get('delta') or 0):+} ({a.get('reason', '')[:40]})"
        if t == "transfer_stock": return f"Move {a.get('qty')} × {a.get('sku')} from {a.get('from_warehouse')} to {a.get('to_warehouse')}"
        if t == "record_deposit": return f"Record deposit of {money(a.get('amount_counted'))} for {a.get('plan_id')}"
        if t == "record_payment": return f"Record {money(a.get('amount'))} received from {a.get('customer_id')} by {a.get('method', 'cash')}"
        if t == "record_expense": return f"Record expense {money(a.get('amount'))} — {a.get('category')} ({a.get('note', '')[:30]})"
        if t == "credit_note": return f"Credit note {money(a.get('amount'))} to {a.get('customer_id')} — {a.get('reason', '')[:40]}"
        if t == "record_purchase":
            lines = ", ".join(f"{i['qty']} × {i['sku']}" for i in a.get("items", []))
            return f"Receive from {a.get('supplier_id')}: {lines} into {a.get('warehouse_id') or 'default godown'}"
        if t == "pay_supplier": return f"Pay supplier {a.get('supplier_id')} {money(a.get('amount'))} by {a.get('method', 'cash')}"
        if t == "draft_reminder": return f"Draft {a.get('tier') or 'auto'} reminder for {a.get('customer_id')}"
        if t == "draft_due_reminders": return f"Draft reminders for everyone ≥{a.get('min_days_overdue')} days overdue"
        if t == "send_reminder": return f"Send reminder {a.get('reminder_id')} to the customer"
        if t == "log_promise": return f"Log promise: {a.get('customer_id')} pays {money(a.get('amount'))} by {a.get('promised_date')}"
        return f"{t}({a})"


@dataclass
class Reply:
    text: str
    specialist: str | None = None
    pending: PendingApproval | None = None      # a card THIS turn raised
    thread_id: str | None = None
    waiting: PendingApproval | None = None      # an earlier card this message is held behind (nothing new was asked for)


# ====================================================================== approval cards
# What a human sees before tapping Approve: names instead of ids, the lines, the total, and one
# sentence saying what will change, with before/after figures read from the books right now.
#
# Every piece of text is returned twice: as English (`title`, `effect`, warning `text`) for API
# clients and notifications, and as a template key plus structured vars (`title_key`/`title_vars`,
# ...) so the app can render the same sentence in Urdu. A var is a plain string (a name or id), a
# number (a quantity), {"rs": rupees} (money), {"k": word} (a translatable word: a status, a
# category, a document kind) or a list of {"name", "before", "after"} (stock or balance changes).
#
# Built from READ-ONLY repository calls. Never raises: a record that has gone, a malformed id or
# a malformed argument gives the fallback card (the raw description), flagged `fallback: true`.
CARD_EN = {
    # titles
    "t_create_order": "Create order for {customer}",
    "t_confirm_order": "Confirm order for {customer}",
    "t_confirm_order_id": "Confirm order {order}",
    "t_cancel_order": "Cancel order for {customer}",
    "t_cancel_order_id": "Cancel order {order}",
    "t_allocate_order": "Reserve stock for {customer}'s order",
    "t_allocate_order_id": "Reserve stock for order {order}",
    "t_create_dispatch_plan": "Plan delivery: {route} on {vehicle}",
    "t_approve_dispatch_plan": "Load {vehicle} for {route}",
    "t_approve_dispatch_plan_id": "Load dispatch plan {plan}",
    "t_write_off": "Write off {qty} {product} at {godown}",
    "t_add_stock": "Add {qty} {product} at {godown}",
    "t_transfer_stock": "Move {qty} {product} from {from} to {to}",
    "t_record_deposit": "Record cash hand-in for {route} ({vehicle})",
    "t_record_deposit_id": "Record cash hand-in for plan {plan}",
    "t_record_payment": "Record {amount} received from {customer}",
    "t_record_expense": "Record {amount} {category} expense",
    "t_credit_note": "Credit note of {amount} to {customer}",
    "t_record_purchase": "Receive stock from {supplier}",
    "t_pay_supplier": "Pay {supplier} {amount}",
    "t_draft_reminder": "Draft a {tier} payment reminder to {customer}",
    "t_draft_due_reminders": "Draft reminders for everyone {days}+ days overdue",
    "t_send_reminder": "Send payment reminder to {customer}",
    "t_send_reminder_id": "Send reminder {reminder}",
    "t_log_promise": "Log promise: {customer} pays {amount} by {date}",
    "t_reverse_khata": "Reverse {doc} {entry} for {customer}",
    "t_reverse_khata_id": "Reverse khata entry {entry}",
    "t_reverse_expense": "Reverse {category} expense {entry}",
    "t_reverse_expense_id": "Reverse expense {entry}",
    "t_reverse_purchase": "Reverse purchase {entry} from {supplier}",
    "t_reverse_purchase_id": "Reverse purchase {entry}",
    "t_reverse_supplier": "Reverse {doc} {entry} for {supplier}",
    "t_reverse_supplier_id": "Reverse supplier entry {entry}",
    "fallback": "{text}",
    # effects
    "order_new": "Saves a draft order of {total}; nothing is owed until it is delivered. {customer} owes {now} now, {after} once delivered.",
    "order_new_plain": "Saves a draft order of {total}; nothing is owed until it is delivered.",
    "order_confirm": "Order {order} ({total}) goes to the godown for packing. {customer} owes {now} now, {after} once delivered.",
    "order_cancel": "Order {order} ({total}) is cancelled. Nothing is owed for it.",
    "order_cancel_release": "Order {order} ({total}) is cancelled and its reserved stock at {godown} is released.",
    "stock_reserve": "Reserves stock at {godown} for order {order}. Available: {changes}.",
    "plan_create": "{count} order(s), {units} units on a vehicle that holds {capacity}, for {date}. Nothing leaves the godown until loading is approved.",
    "plan_load": "Stock leaves {godown}: {changes}. {count} customer(s) get a delivery code.",
    "stock_adjust": "{product} at {godown} goes {before} → {after}, worth about {value} at cost.",
    "stock_transfer": "{product}: {changes}.",
    "deposit_short": "Counted {counted} against {expected} expected: short by {gap}. The shortfall is booked as a cash-shortage expense.",
    "deposit_ok": "Counted {counted} against {expected} expected: it matches.",
    "deposit_over": "Counted {counted} against {expected} expected: {gap} more than expected.",
    "khata_change": "{customer} will owe {after} (now {now}).",
    "expense_out": "{amount} goes out as a {category} expense, paid by {method}.",
    "credit_note": "{customer} will owe {after} (now {now}). Reported revenue drops by {amount}.",
    "purchase_in": "Stock at {godown}: {changes}. We will owe {supplier} {after} (now {now}).",
    "supplier_pay": "We will owe {supplier} {after} (now {now}). Paid by {method}.",
    "reminder_draft": "Drafts a reminder about {amount} ({days} days overdue). Nothing is sent until someone approves sending it.",
    "reminder_none": "{customer} has nothing overdue, so no reminder will be drafted.",
    "reminders_due": "Drafts {count} reminder(s) covering {amount}. Nothing is sent until each one is approved.",
    "reminder_send": "A WhatsApp message goes to {customer} ({phone}) about {amount} due.",
    "promise": "No money moves. Munshi follows up if {customer} hasn't paid by {date}. Owes {now} now.",
    "reverse_khata": "Cancels {doc} {entry} ({amount}): {customer} will owe {after} (now {now}).",
    "reverse_expense": "Cancels expense {entry} ({amount} {category}, {date}): {amount} comes back into today's books.",
    "reverse_purchase": "Goods go back out of {godown}: {changes}. We will owe {supplier} {after} (now {now}).",
    "reverse_supplier": "Cancels {doc} {entry} ({amount}): we will owe {supplier} {after} (now {now}).",
    # warnings
    "w_not_found": "{what} {id} was not found: approving will do nothing.",
    "w_unknown_product": "Unknown product {sku}: approving will fail.",
    "w_over_credit": "Over credit limit: {customer} would owe {exposure} against a limit of {limit}. The owner must confirm.",
    "w_big_order": "Above the big-order limit of {limit}: the owner must approve.",
    "w_short_stock": "Not enough stock: {product} needs {need}, only {available} available.",
    "w_below_zero": "Only {on_hand} {product} on hand at {godown}: stock can't go below zero, so approving will fail.",
    "w_bad_status": "{what} {id} is {status}: approving will fail.",
    "w_over_capacity": "{units} units won't fit: the vehicle holds {capacity}.",
    "w_overpay": "More than is owed: the balance goes to {after}.",
    "w_cash_short": "Cash short by {gap}.",
    "w_paid_over_total": "Paid now ({paid}) is more than the bill ({total}): approving will fail.",
    "w_is_reversal": "{entry} is itself a reversal: approving will fail.",
    "w_already_reversed": "{entry} was already reversed by {by}: approving will fail.",
    "w_bill_of_purchase": "Bill {entry} came with purchase {purchase}: reverse the purchase instead, so the goods go back too.",
    "w_already_sent": "Reminder {reminder} was already sent.",
    # who approves
    "approver_owner": "The owner must approve",
    "approver_clerk": "Another clerk or the owner must approve",
}

# English for {"k": word} vars. The app translates the same keys (k_<word>) in i18n.js.
CARD_WORDS_EN = {
    "customer": "Customer", "supplier": "Supplier", "order": "Order", "plan": "Dispatch plan", "product": "Product", "godown": "Godown",
    "entry": "Entry", "expense": "Expense", "purchase": "Purchase", "reminder": "Reminder", "route": "Route", "vehicle": "Vehicle",
    "receipt": "Receipt", "invoice": "Invoice", "credit_note": "Credit note", "opening": "Opening balance", "payment": "Payment", "bill": "Bill",
    "cash": "cash", "bank": "bank", "jazzcash": "JazzCash", "easypaisa": "Easypaisa", "cheque": "cheque", "adjustment": "adjustment",
    "fuel": "fuel", "salary": "salary", "rent": "rent", "repair": "repair", "utilities": "utilities", "loading": "loading", "food": "food", "misc": "misc",
    "cash_shortage": "cash shortage", "gentle": "gentle", "firm": "firm", "final": "final", "auto": "",
    "draft": "draft", "confirmed": "confirmed", "allocated": "allocated", "dispatched": "dispatched", "delivered": "delivered", "short": "short",
    "cancelled": "cancelled", "planned": "planned", "approved": "approved", "loaded": "loaded", "completed": "completed", "sent": "sent", "drafted": "drafted",
    "method": "Method", "reason": "Reason", "note": "Note", "reference": "Reference", "paid_now": "Paid now", "bill_ref": "Bill no.",
}


def _rs(paisa: int) -> dict:
    return {"rs": to_rupees(int(paisa))}


def _word(w: str) -> dict:
    return {"k": str(w)}


def _en(v) -> str:
    if isinstance(v, dict) and "rs" in v:
        r = v["rs"]
        return f"Rs {r:,.0f}" if float(r).is_integer() else f"Rs {r:,.2f}"
    if isinstance(v, dict) and "k" in v:
        return CARD_WORDS_EN.get(v["k"], str(v["k"]).replace("_", " "))
    if isinstance(v, list):
        return ", ".join(f"{c['name']} {_en(c['before'])} → {_en(c['after'])}" for c in v)
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _t(key: str, **vars) -> dict:
    """One sentence: its template key, its vars, and the English text."""
    text = CARD_EN[key].format_map({k: _en(v) for k, v in vars.items()})
    return {"key": key, "vars": vars, "text": text}


class CardBuilder:
    """Reads the books to describe a pending gated call. Read-only: it calls only getters."""

    def __init__(self, repo: MunshiRepository, ops: MunshiTools) -> None:
        self.repo, self.ops = repo, ops

    # ---------------------------------------------------------- safe lookups (None when missing or malformed)
    def _get(self, fn, *a):
        try:
            return fn(*a)
        except Exception:
            return None

    def _name(self, fn, rid) -> str:
        rec = self._get(fn, str(rid or ""))
        return rec.name if rec is not None else str(rid or "?")

    def _cust(self, cid): return self._get(self.repo.get_customer, str(cid or ""))
    def _sup(self, sid): return self._get(self.repo.get_supplier, str(sid or ""))
    def _prod(self, sku): return self._get(self.repo.get_product, str(sku or ""))
    def _order(self, oid): return self._get(self.repo.get_order, str(oid or ""))
    def _wh_name(self, wid): return self._name(self.repo.get_warehouse, wid)

    def _default_wh(self) -> str:
        return self._get(self.repo.default_warehouse_id) or ""

    def _available(self, sku: str, wid: str | None = None) -> int:
        levels = [self.repo.get_stock(wid, sku)] if wid else self.repo.stock_by_sku(sku)
        return sum(s.available for s in levels)

    @staticmethod
    def _need(items) -> dict[str, int]:
        need: dict[str, int] = {}
        for it in items:
            need[it.sku] = need.get(it.sku, 0) + int(it.qty)
        return need

    def _line(self, sku: str, qty: int, unit_price_p: int | None, **extra) -> dict:
        p = self._prod(sku)
        return {"sku": sku, "name": p.name if p else sku, "qty": int(qty), "unit": p.unit if p else "",
                "unit_price": to_rupees(unit_price_p) if unit_price_p is not None else None,
                "line_total": to_rupees(int(qty) * unit_price_p) if unit_price_p is not None else None} | extra

    def _order_lines(self, o) -> list[dict]:
        return [self._line(i.sku, i.qty, i.unit_price_paisa) for i in o.items]

    def _stock_warnings(self, need: dict[str, int], wid: str | None = None) -> list[dict]:
        out = []
        for sku, qty in need.items():
            avail = self._available(sku, wid)
            if avail < qty:
                out.append(_t("w_short_stock", product=self._name(self.repo.get_product, sku), need=qty, available=avail))
        return out

    def _customer_balance(self, cid: str) -> int:
        return self.repo.outstanding_paisa(str(cid))

    def _credit_warning(self, cid: str, cname: str, exposure_p: int) -> list[dict]:
        limit_p = self.repo.customer_credit_limit_paisa(str(cid))
        return [_t("w_over_credit", customer=cname, exposure=_rs(exposure_p), limit=_rs(limit_p))] if limit_p and exposure_p > limit_p else []

    def _missing(self, title_key: str, what: str, rid, **title_vars) -> dict:
        return {"title": _t(title_key, **title_vars), "warnings": [_t("w_not_found", what=_word(what), id=str(rid or "?"))]}

    # ---------------------------------------------------------- entry point
    def build(self, pa: PendingApproval) -> dict:
        base = {"version": 1, "tool": pa.tool, "tier": pa.tier, "needs_role": pa.needs_role,
                "approver": _t("approver_owner" if pa.needs_role == "owner" else "approver_clerk"),
                "requested_by": pa.requested_by, "requested_by_role": pa.requested_by_role, "created_at": pa.created_at}
        body = None
        try:
            fn = getattr(self, "_c_" + pa.tool, None)
            body = fn(dict(pa.args or {}), pa) if fn else None
        except Exception:
            log.warning("approval card for %s fell back to the raw description", pa.tool, exc_info=True)
            body = None
        fallback = body is None
        if fallback:
            try:
                raw = pa.describe()
            except Exception:
                raw = pa.tool.replace("_", " ")
            body = {"title": _t("fallback", text=raw)}
        title, effect = body["title"], body.get("effect")
        return base | {
            "title": title["text"], "title_key": title["key"], "title_vars": title["vars"],
            "effect": effect["text"] if effect else "", "effect_key": effect["key"] if effect else None, "effect_vars": effect["vars"] if effect else {},
            "lines": body.get("lines", []), "total": body.get("total"),
            "facts": body.get("facts", []), "quote": body.get("quote"),
            "warnings": [{"code": w["key"][2:], "text": w["text"], "key": w["key"], "vars": w["vars"]} for w in body.get("warnings", [])],
            "fallback": fallback,
        }

    # ---------------------------------------------------------- order desk
    def _c_create_order(self, a: dict, pa) -> dict:
        cid = str(a.get("customer_id") or ""); cust = self._cust(cid); cname = cust.name if cust else cid
        lines, warns, need = [], [], {}
        total_p = list_total_p = 0
        for it in a.get("items") or []:
            sku, qty = str(it.get("sku") or ""), int(it.get("qty") or 0)
            p = self._prod(sku)
            if p is None:
                warns.append(_t("w_unknown_product", sku=sku)); lines.append(self._line(sku, qty, None)); continue
            list_p = to_paisa(p.unit_price)
            price_p = to_paisa(it["unit_price"]) if it.get("unit_price") not in (None, "", 0) else discounted_paisa(list_p, cust.discount_pct if cust else 0)
            lines.append(self._line(sku, qty, price_p))
            total_p += qty * price_p; list_total_p += qty * list_p
            need[sku] = need.get(sku, 0) + qty
        if cust:
            now_p = self._customer_balance(cid)
            effect = _t("order_new", total=_rs(total_p), customer=cname, now=_rs(now_p), after=_rs(now_p + total_p))
            warns += self._credit_warning(cid, cname, now_p + total_p)
        else:
            effect = _t("order_new_plain", total=_rs(total_p))
            warns.insert(0, _t("w_not_found", what=_word("customer"), id=cid or "?"))
        limit = float(self.repo.setting("big_order_limit") or 0)
        if limit and to_rupees(list_total_p) > limit:      # the same test _needs_role escalates on
            warns.append(_t("w_big_order", limit={"rs": limit}))
        warns += self._stock_warnings(need)
        return {"title": _t("t_create_order", customer=cname), "effect": effect, "lines": lines, "total": to_rupees(total_p), "warnings": warns}

    def _c_confirm_order(self, a: dict, pa) -> dict:
        oid = str(a.get("order_id") or ""); o = self._order(oid)
        if o is None:
            return self._missing("t_confirm_order_id", "order", oid, order=oid)
        cname = self._name(self.repo.get_customer, o.customer_id)
        now_p = self._customer_balance(o.customer_id)
        warns = [] if o.status == "draft" else [_t("w_bad_status", what=_word("order"), id=oid, status=_word(o.status))]
        warns += self._credit_warning(o.customer_id, cname, now_p + o.total_paisa) + self._stock_warnings(self._need(o.items))
        return {"title": _t("t_confirm_order", customer=cname), "lines": self._order_lines(o), "total": o.total, "warnings": warns,
                "effect": _t("order_confirm", order=oid, total=_rs(o.total_paisa), customer=cname, now=_rs(now_p), after=_rs(now_p + o.total_paisa))}

    def _c_cancel_order(self, a: dict, pa) -> dict:
        oid = str(a.get("order_id") or ""); o = self._order(oid)
        if o is None:
            return self._missing("t_cancel_order_id", "order", oid, order=oid)
        cname = self._name(self.repo.get_customer, o.customer_id)
        if o.status == "allocated" and o.warehouse_id:
            effect = _t("order_cancel_release", order=oid, total=_rs(o.total_paisa), godown=self._wh_name(o.warehouse_id))
        else:
            effect = _t("order_cancel", order=oid, total=_rs(o.total_paisa))
        warns = [] if o.status in ("draft", "confirmed", "allocated") else [_t("w_bad_status", what=_word("order"), id=oid, status=_word(o.status))]
        facts = [{"key": "reason", "value": str(a["reason"])}] if a.get("reason") else []
        return {"title": _t("t_cancel_order", customer=cname), "effect": effect, "lines": self._order_lines(o), "total": o.total, "warnings": warns, "facts": facts}

    # ---------------------------------------------------------- godown
    def _c_allocate_order(self, a: dict, pa) -> dict:
        oid = str(a.get("order_id") or ""); o = self._order(oid)
        if o is None:
            return self._missing("t_allocate_order_id", "order", oid, order=oid)
        wid = str(a.get("warehouse_id") or "") or self._default_wh(); gname = self._wh_name(wid)
        need = self._need(o.items)
        avail = {sku: self._available(sku, wid) for sku in need}
        lines = [self._line(i.sku, i.qty, i.unit_price_paisa) for i in o.items]
        changes = [{"name": self._name(self.repo.get_product, sku), "before": avail[sku], "after": avail[sku] - q} for sku, q in need.items()]
        warns = [] if o.status == "confirmed" else [_t("w_bad_status", what=_word("order"), id=oid, status=_word(o.status))]
        warns += self._stock_warnings(need, wid)
        return {"title": _t("t_allocate_order", customer=self._name(self.repo.get_customer, o.customer_id)), "lines": lines, "total": o.total,
                "effect": _t("stock_reserve", godown=gname, order=oid, changes=changes), "warnings": warns}

    def _plan_lines(self, order_ids) -> tuple[list[dict], list, list[dict]]:
        lines, orders, warns = [], [], []
        for oid in dict.fromkeys(str(x) for x in order_ids or []):
            o = self._order(oid)
            if o is None:
                warns.append(_t("w_not_found", what=_word("order"), id=oid)); continue
            orders.append(o)
            lines.append({"sku": oid, "name": self._name(self.repo.get_customer, o.customer_id), "qty": o.load_units, "unit": "units",
                          "unit_price": None, "line_total": o.total, "ref": oid})
        return lines, orders, warns

    def _c_create_dispatch_plan(self, a: dict, pa) -> dict:
        route, veh = self._get(self.repo.get_route, str(a.get("route_id") or "")), self._get(self.repo.get_vehicle, str(a.get("vehicle_id") or ""))
        lines, orders, warns = self._plan_lines(a.get("order_ids"))
        if route is None: warns.insert(0, _t("w_not_found", what=_word("route"), id=str(a.get("route_id") or "?")))
        if veh is None: warns.insert(0, _t("w_not_found", what=_word("vehicle"), id=str(a.get("vehicle_id") or "?")))
        units = sum(o.load_units for o in orders)
        warns += [_t("w_bad_status", what=_word("order"), id=o.order_id, status=_word(o.status)) for o in orders if o.status != "allocated"]
        if veh is not None and units > veh.capacity_units:
            warns.append(_t("w_over_capacity", units=units, capacity=veh.capacity_units))
        total_p = sum(o.total_paisa for o in orders)
        return {"title": _t("t_create_dispatch_plan", route=route.name if route else str(a.get("route_id") or "?"), vehicle=veh.plate if veh else str(a.get("vehicle_id") or "?")),
                "effect": _t("plan_create", count=len(orders), units=units, capacity=veh.capacity_units if veh else 0, date=str(a.get("plan_date") or today_iso())),
                "lines": lines, "total": to_rupees(total_p), "warnings": warns}

    def _c_approve_dispatch_plan(self, a: dict, pa) -> dict:
        pid = str(a.get("plan_id") or ""); plan = self._get(self.repo.get_plan, pid)
        if plan is None:
            return self._missing("t_approve_dispatch_plan_id", "plan", pid, plan=pid)
        lines, orders, warns = self._plan_lines(plan.order_ids)
        need: dict[str, int] = {}
        for o in orders:
            for sku, q in self._need(o.items).items(): need[sku] = need.get(sku, 0) + q
        on_hand = {sku: self.repo.get_stock(plan.warehouse_id, sku).on_hand for sku in need}
        changes = [{"name": self._name(self.repo.get_product, sku), "before": on_hand[sku], "after": on_hand[sku] - q} for sku, q in need.items()]
        gname = self._wh_name(plan.warehouse_id)
        if plan.status != "planned":
            warns.insert(0, _t("w_bad_status", what=_word("plan"), id=pid, status=_word(plan.status)))
        warns += [_t("w_below_zero", on_hand=on_hand[sku], product=self._name(self.repo.get_product, sku), godown=gname) for sku, q in need.items() if on_hand[sku] < q]
        stops = self._get(self.repo.list_stops, pid) or []
        veh = self._get(self.repo.get_vehicle, plan.vehicle_id)
        return {"title": _t("t_approve_dispatch_plan", vehicle=veh.plate if veh else plan.vehicle_id, route=self._name(self.repo.get_route, plan.route_id)),
                "effect": _t("plan_load", godown=gname, changes=changes, count=len(stops) or len(orders)),
                "lines": lines, "total": to_rupees(sum(o.total_paisa for o in orders)), "warnings": warns}

    def _c_adjust_stock(self, a: dict, pa) -> dict:
        wid = str(a.get("warehouse_id") or "") or self._default_wh(); sku = str(a.get("sku") or ""); delta = int(a.get("delta") or 0)
        p = self._prod(sku); pname, gname = (p.name if p else sku), self._wh_name(wid)
        before = self.repo.get_stock(wid, sku).on_hand; after = before + delta
        cost_p = self.repo.avg_cost_paisa(sku) if p else 0
        warns = [] if p else [_t("w_unknown_product", sku=sku)]
        if after < 0:
            warns.append(_t("w_below_zero", on_hand=before, product=pname, godown=gname))
        facts = [{"key": "reason", "value": str(a["reason"])}] if a.get("reason") else []
        return {"title": _t("t_write_off" if delta < 0 else "t_add_stock", qty=abs(delta), product=pname, godown=gname),
                "effect": _t("stock_adjust", product=pname, godown=gname, before=before, after=after, value=_rs(abs(delta) * cost_p)),
                "lines": [self._line(sku, delta, cost_p)], "total": None, "warnings": warns, "facts": facts}

    def _c_transfer_stock(self, a: dict, pa) -> dict:
        src, dst, sku, qty = str(a.get("from_warehouse") or ""), str(a.get("to_warehouse") or ""), str(a.get("sku") or ""), int(a.get("qty") or 0)
        p = self._prod(sku); pname = p.name if p else sku
        s_from, s_to = self.repo.get_stock(src, sku), self.repo.get_stock(dst, sku)
        sname, dname = self._wh_name(src), self._wh_name(dst)
        changes = [{"name": sname, "before": s_from.on_hand, "after": s_from.on_hand - qty}, {"name": dname, "before": s_to.on_hand, "after": s_to.on_hand + qty}]
        warns = [] if p else [_t("w_unknown_product", sku=sku)]
        if s_from.available < qty:
            warns.append(_t("w_short_stock", product=pname, need=qty, available=s_from.available))
        return {"title": _t("t_transfer_stock", qty=qty, product=pname, **{"from": sname, "to": dname}),
                "effect": _t("stock_transfer", product=pname, changes=changes), "lines": [self._line(sku, qty, None)], "warnings": warns}

    # ---------------------------------------------------------- hisaab
    def _c_record_deposit(self, a: dict, pa) -> dict:
        pid = str(a.get("plan_id") or ""); plan = self._get(self.repo.get_plan, pid)
        if plan is None:
            return self._missing("t_record_deposit_id", "plan", pid, plan=pid)
        counted_p = to_paisa(a.get("amount_counted") or 0)
        expected_p = sum(cash for _, _, cash in self.repo._stop_cash_paisa(pid)) - self.repo._deposited_paisa(pid)
        gap_p = counted_p - expected_p
        key = "deposit_ok" if gap_p == 0 else "deposit_short" if gap_p < 0 else "deposit_over"
        veh = self._get(self.repo.get_vehicle, plan.vehicle_id)
        warns = [_t("w_cash_short", gap=_rs(-gap_p))] if gap_p < 0 else []
        if plan.status == "planned":
            warns.insert(0, _t("w_bad_status", what=_word("plan"), id=pid, status=_word(plan.status)))
        return {"title": _t("t_record_deposit", route=self._name(self.repo.get_route, plan.route_id), vehicle=veh.plate if veh else plan.vehicle_id),
                "effect": _t(key, counted=_rs(counted_p), expected=_rs(expected_p), gap=_rs(abs(gap_p))), "total": to_rupees(counted_p), "warnings": warns}

    def _c_record_payment(self, a: dict, pa) -> dict:
        cid = str(a.get("customer_id") or ""); cust = self._cust(cid)
        amt_p = to_paisa(a.get("amount") or 0)
        facts = [{"key": "method", "value": _word(a.get("method") or "cash")}] + ([{"key": "reference", "value": str(a["ref"])}] if a.get("ref") else [])
        if cust is None:
            return self._missing("t_record_payment", "customer", cid, amount=_rs(amt_p), customer=cid) | {"facts": facts, "total": to_rupees(amt_p)}
        now_p = self._customer_balance(cid); after_p = now_p - amt_p
        warns = [_t("w_overpay", after=_rs(after_p))] if after_p < 0 else []
        return {"title": _t("t_record_payment", amount=_rs(amt_p), customer=cust.name), "effect": _t("khata_change", customer=cust.name, after=_rs(after_p), now=_rs(now_p)),
                "total": to_rupees(amt_p), "warnings": warns, "facts": facts}

    def _c_record_expense(self, a: dict, pa) -> dict:
        from munshi.domain.repository.cash import EXPENSE_CATEGORIES
        amt_p = to_paisa(a.get("amount") or 0)
        cat = a.get("category") if a.get("category") in EXPENSE_CATEGORIES else "misc"        # as the repository files it
        method = a.get("method") or "cash"
        facts = [{"key": "note", "value": str(a["note"])}] if a.get("note") else []
        return {"title": _t("t_record_expense", amount=_rs(amt_p), category=_word(cat)), "effect": _t("expense_out", amount=_rs(amt_p), category=_word(cat), method=_word(method)),
                "total": to_rupees(amt_p), "facts": facts}

    def _c_credit_note(self, a: dict, pa) -> dict:
        cid = str(a.get("customer_id") or ""); cust = self._cust(cid)
        amt_p = abs(to_paisa(a.get("amount") or 0))
        facts = [{"key": "reason", "value": str(a["reason"])}] if a.get("reason") else []
        if cust is None:
            return self._missing("t_credit_note", "customer", cid, amount=_rs(amt_p), customer=cid) | {"facts": facts, "total": to_rupees(amt_p)}
        now_p = self._customer_balance(cid); after_p = now_p - amt_p
        warns = [_t("w_overpay", after=_rs(after_p))] if after_p < 0 else []
        return {"title": _t("t_credit_note", amount=_rs(amt_p), customer=cust.name), "effect": _t("credit_note", customer=cust.name, after=_rs(after_p), now=_rs(now_p), amount=_rs(amt_p)),
                "total": to_rupees(amt_p), "warnings": warns, "facts": facts}

    # ---------------------------------------------------------- khareed
    def _c_record_purchase(self, a: dict, pa) -> dict:
        sid = str(a.get("supplier_id") or ""); sup = self._sup(sid); sname = sup.name if sup else sid
        wid = str(a.get("warehouse_id") or "") or self._default_wh(); gname = self._wh_name(wid)
        lines, warns, qty_by = [], [] if sup else [_t("w_not_found", what=_word("supplier"), id=sid or "?")], {}
        total_p = 0
        for it in a.get("items") or []:
            sku, qty = str(it.get("sku") or ""), int(it.get("qty") or 0)
            p = self._prod(sku)
            if p is None:
                warns.append(_t("w_unknown_product", sku=sku)); lines.append(self._line(sku, qty, None)); continue
            cost_p = to_paisa(it.get("unit_cost") or 0) or to_paisa(p.cost_price)       # as the repository prices it
            lines.append(self._line(sku, qty, cost_p)); total_p += qty * cost_p
            qty_by[sku] = qty_by.get(sku, 0) + qty
        paid_p = to_paisa(a.get("paid_amount") or 0)
        if paid_p > total_p:
            warns.append(_t("w_paid_over_total", paid=_rs(paid_p), total=_rs(total_p)))
        on_hand = {sku: self.repo.get_stock(wid, sku).on_hand for sku in qty_by}
        changes = [{"name": self._name(self.repo.get_product, sku), "before": on_hand[sku], "after": on_hand[sku] + q} for sku, q in qty_by.items()]
        now_p = self.repo.supplier_balance_paisa(sid) if sup else 0
        facts = ([{"key": "bill_ref", "value": str(a["invoice_ref"])}] if a.get("invoice_ref") else []) + ([{"key": "paid_now", "value": _rs(paid_p)}] if paid_p else [])
        return {"title": _t("t_record_purchase", supplier=sname), "lines": lines, "total": to_rupees(total_p), "warnings": warns, "facts": facts,
                "effect": _t("purchase_in", godown=gname, changes=changes, supplier=sname, after=_rs(now_p + total_p - paid_p), now=_rs(now_p))}

    def _c_pay_supplier(self, a: dict, pa) -> dict:
        sid = str(a.get("supplier_id") or ""); sup = self._sup(sid)
        amt_p = to_paisa(a.get("amount") or 0); method = a.get("method") or "cash"
        facts = [{"key": "reference", "value": str(a["ref"])}] if a.get("ref") else []
        if sup is None:
            return self._missing("t_pay_supplier", "supplier", sid, supplier=sid, amount=_rs(amt_p)) | {"facts": facts, "total": to_rupees(amt_p)}
        now_p = self.repo.supplier_balance_paisa(sid); after_p = now_p - amt_p
        warns = [_t("w_overpay", after=_rs(after_p))] if after_p < 0 else []
        return {"title": _t("t_pay_supplier", supplier=sup.name, amount=_rs(amt_p)), "total": to_rupees(amt_p), "warnings": warns, "facts": facts,
                "effect": _t("supplier_pay", supplier=sup.name, after=_rs(after_p), now=_rs(now_p), method=_word(method))}

    # ---------------------------------------------------------- wasooli
    def _c_draft_reminder(self, a: dict, pa) -> dict:
        cid = str(a.get("customer_id") or ""); cust = self._cust(cid)
        if cust is None:
            return self._missing("t_draft_reminder", "customer", cid, tier=_word(a.get("tier") or "auto"), customer=cid)
        ag = next(iter(self.repo.aging(customer_id=cid)), None)
        if not ag:
            return {"title": _t("t_draft_reminder", tier=_word(a.get("tier") or "auto"), customer=cust.name), "effect": _t("reminder_none", customer=cust.name)}
        tier = a.get("tier") or ("final" if ag["days_overdue"] > 60 else "firm" if ag["days_overdue"] > 30 else "gentle")    # as the tool picks it
        return {"title": _t("t_draft_reminder", tier=_word(tier), customer=cust.name), "total": ag["balance"],
                "effect": _t("reminder_draft", amount={"rs": ag["balance"]}, days=int(ag["days_overdue"]))}

    def _c_draft_due_reminders(self, a: dict, pa) -> dict:
        days = int(a.get("min_days_overdue") or 1)
        waiting = {r.customer_id for r in self.repo.list_reminders("drafted")}
        rows = [x for x in self.repo.aging() if x["days_overdue"] >= days and x["customer_id"] not in waiting]
        total_p = sum(to_paisa(x["balance"]) for x in rows)
        lines = [{"sku": x["customer_id"], "name": x["name"], "qty": int(x["days_overdue"]), "unit": "days", "unit_price": None, "line_total": x["balance"]} for x in rows]
        return {"title": _t("t_draft_due_reminders", days=days), "lines": lines, "total": to_rupees(total_p),
                "effect": _t("reminders_due", count=len(rows), amount=_rs(total_p))}

    def _c_send_reminder(self, a: dict, pa) -> dict:
        rid = str(a.get("reminder_id") or ""); r = self._get(self.repo.get_reminder, rid)
        if r is None:
            return self._missing("t_send_reminder_id", "reminder", rid, reminder=rid)
        cust = self._cust(r.customer_id); cname = cust.name if cust else r.customer_id
        warns = [_t("w_already_sent", reminder=rid)] if r.status == "sent" else []
        return {"title": _t("t_send_reminder", customer=cname), "quote": r.message, "total": r.amount_due, "warnings": warns,
                "effect": _t("reminder_send", customer=cname, phone=cust.phone if cust else "", amount={"rs": r.amount_due})}

    def _c_log_promise(self, a: dict, pa) -> dict:
        cid = str(a.get("customer_id") or ""); cust = self._cust(cid)
        amt_p, day = to_paisa(a.get("amount") or 0), str(a.get("promised_date") or "")
        if cust is None:
            return self._missing("t_log_promise", "customer", cid, customer=cid, amount=_rs(amt_p), date=day)
        return {"title": _t("t_log_promise", customer=cust.name, amount=_rs(amt_p), date=day), "total": to_rupees(amt_p),
                "effect": _t("promise", customer=cust.name, date=day, now=_rs(self._customer_balance(cid)))}

    # ---------------------------------------------------------- reversals
    @staticmethod
    def _reason(a: dict) -> list[dict]:
        return [{"key": "reason", "value": str(a["reason"])}] if a.get("reason") else []

    def _reversal_warnings(self, rid: str, reversal_of, kind: str) -> list[dict]:
        if reversal_of:
            return [_t("w_is_reversal", entry=rid)]
        by = self.ops.reversed_by(kind, rid)
        return [_t("w_already_reversed", entry=rid, by=by)] if by else []

    def _c_reverse_ledger_entry(self, a: dict, pa) -> dict:
        eid = str(a.get("entry_id") or ""); e = self._get(self.repo.get_ledger_entry, eid)
        if e is None:
            return self._missing("t_reverse_khata_id", "entry", eid, entry=eid) | {"facts": self._reason(a)}
        doc = {"payment": "receipt", "credit_note": "credit_note"}.get(e.kind, "opening" if e.kind == "invoice" and e.method == "adjustment" else e.kind)
        cname = self._name(self.repo.get_customer, e.customer_id)
        amt_p = to_paisa(e.amount); now_p = self._customer_balance(e.customer_id)
        return {"title": _t("t_reverse_khata", doc=_word(doc), entry=eid, customer=cname), "total": to_rupees(abs(amt_p)), "facts": self._reason(a),
                "effect": _t("reverse_khata", doc=_word(doc), entry=eid, amount=_rs(abs(amt_p)), customer=cname, after=_rs(now_p - amt_p), now=_rs(now_p)),
                "warnings": self._reversal_warnings(eid, e.reversal_of, "ledger")}

    def _c_reverse_expense(self, a: dict, pa) -> dict:
        xid = str(a.get("expense_id") or ""); x = self._get(self.repo.get_expense, xid)
        if x is None:
            return self._missing("t_reverse_expense_id", "expense", xid, entry=xid) | {"facts": self._reason(a)}
        amt = _rs(abs(to_paisa(x.amount)))
        return {"title": _t("t_reverse_expense", category=_word(x.category), entry=xid), "total": amt["rs"], "facts": self._reason(a),
                "effect": _t("reverse_expense", entry=xid, amount=amt, category=_word(x.category), date=x.expense_date),
                "warnings": self._reversal_warnings(xid, x.reversal_of, "expense")}

    def _c_reverse_purchase(self, a: dict, pa) -> dict:
        pid = str(a.get("purchase_id") or ""); pur = self._get(self.repo.get_purchase, pid)
        if pur is None:
            return self._missing("t_reverse_purchase_id", "purchase", pid, entry=pid) | {"facts": self._reason(a)}
        sname, gname = self._name(self.repo.get_supplier, pur.supplier_id), self._wh_name(pur.warehouse_id)
        qty_by: dict[str, int] = {}
        for i in pur.items: qty_by[i["sku"]] = qty_by.get(i["sku"], 0) + int(i["qty"])
        on_hand = {sku: self.repo.get_stock(pur.warehouse_id, sku).on_hand for sku in qty_by}
        changes = [{"name": self._name(self.repo.get_product, sku), "before": on_hand[sku], "after": on_hand[sku] - q} for sku, q in qty_by.items()]
        warns = self._reversal_warnings(pid, pur.reversal_of, "purchase")
        warns += [_t("w_below_zero", on_hand=on_hand[sku], product=self._name(self.repo.get_product, sku), godown=gname) for sku, q in qty_by.items() if on_hand[sku] < q]
        now_p = self.repo.supplier_balance_paisa(pur.supplier_id)
        owed_p = to_paisa(pur.total) - to_paisa(pur.paid_amount)
        return {"title": _t("t_reverse_purchase", entry=pid, supplier=sname), "lines": [self._line(i["sku"], i["qty"], to_paisa(i["unit_cost"])) for i in pur.items],
                "total": pur.total, "warnings": warns, "facts": self._reason(a),
                "effect": _t("reverse_purchase", godown=gname, changes=changes, supplier=sname, after=_rs(now_p - owed_p), now=_rs(now_p))}

    def _c_reverse_supplier_entry(self, a: dict, pa) -> dict:
        eid = str(a.get("entry_id") or ""); e = self._get(self.ops.supplier_entry, eid)
        if e is None:
            return self._missing("t_reverse_supplier_id", "entry", eid, entry=eid) | {"facts": self._reason(a)}
        sname = self._name(self.repo.get_supplier, e.supplier_id)
        amt_p = to_paisa(e.amount); now_p = self.repo.supplier_balance_paisa(e.supplier_id)
        warns = self._reversal_warnings(eid, e.reversal_of, "supplier_entry")
        if e.kind == "bill" and self.ops.purchase_exists(e.ref):
            warns.insert(0, _t("w_bill_of_purchase", entry=eid, purchase=e.ref))
        return {"title": _t("t_reverse_supplier", doc=_word(e.kind), entry=eid, supplier=sname), "total": to_rupees(abs(amt_p)), "warnings": warns, "facts": self._reason(a),
                "effect": _t("reverse_supplier", doc=_word(e.kind), entry=eid, amount=_rs(abs(amt_p)), supplier=sname, after=_rs(now_p - amt_p), now=_rs(now_p))}


class MunshiPlatform:
    def __init__(self, repo: MunshiRepository | None = None, model: BaseChatModel | None = None, enable_tracing: bool = False,
                 checkpoint_path: str | None = None) -> None:
        self.repo = repo or seeded_repository()
        self.ops = MunshiTools(self.repo)
        self.enable_tracing = enable_tracing
        self.channel = build_channel()
        self._lock = threading.RLock()
        if enable_tracing:
            configure_tracking()
        self._ckpt_conn = None
        checkpointer = None
        if checkpoint_path:
            from langgraph.checkpoint.sqlite import SqliteSaver
            self._ckpt_conn = sqlite3.connect(checkpoint_path, check_same_thread=False)
            checkpointer = SqliteSaver(self._ckpt_conn)
            checkpointer.setup()
        self.specialists = {name: build(self.ops, self.repo, model, checkpointer) for name, build in BUILDERS.items()}
        self.manager = build_manager(model)
        self.cards = CardBuilder(self.repo, self.ops)

    def close(self) -> None:
        if self._ckpt_conn is not None:
            self._ckpt_conn.close()
        self.repo.close()

    # ------------------------------------------------------------------
    @property
    def pending(self) -> dict[str, PendingApproval]:
        return {a["approval_id"]: self._pa(a) for a in self.repo.pending_approvals()}

    @staticmethod
    def _pa(a: dict) -> PendingApproval:
        return PendingApproval(a["approval_id"], a["thread_id"], a["specialist"], a["tool"], a["args"], a["tier"], a["needs_role"],
                               a["requested_by_role"], a.get("requested_by") or "", a["created_at"])

    def _needs_role(self, tool: str, args: dict) -> str:
        """The registry's approver, escalated to the owner for an order above the business's big-order limit."""
        need = approver_for(tool)
        if tool == "confirm_order" and need == "clerk":
            try:
                if self.repo.over_credit(self.repo.get_order(args.get("order_id", ""))):
                    return "owner"
            except Exception:
                pass
        if tool == "create_order" and need == "clerk":
            try:
                limit = float(self.repo.setting("big_order_limit") or 0)
                total = sum(int(i["qty"]) * self.repo.get_product(i["sku"]).unit_price for i in args.get("items", []))
                if limit and total > limit:
                    return "owner"
            except Exception:
                pass
        return need

    def _cfg(self, thread_id: str, role: str, specialist: str) -> dict:
        # Role is part of the checkpoint key: a driver and a clerk on the same
        # conversation never share a specialist's memory or its pending action.
        return {"configurable": {"thread_id": f"{thread_id}:{role}:{specialist}"}}

    def _trace(self, agent: str, role: str, text: str):
        return trace_turn(agent, role, text) if self.enable_tracing else nullcontext(TurnTrace(agent_name=agent, role=role, user_text=text))

    def _final_text(self, result: dict) -> str:
        msg = result["messages"][-1]
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        return content or "Done."

    def handle_message(self, thread_id: str, role: str, text: str, user: str = "") -> Reply:
        # Every write this turn makes -- including any tool the agent runs on a worker thread -- is `user`'s.
        with self._lock, self.repo.acting_as(user):
            self.repo.add_chat(thread_id, role, text, {"user": user})
            with self._trace("manager", role, text) as tr:
                specialist = classify(self.manager, text, role)
                tr.specialist = specialist
                if specialist is None:
                    self.repo.add_chat(thread_id, "munshi", CLARIFY, {"specialist": None})
                    return Reply(CLARIFY, None, None, thread_id)

                # A thread can only hold one pending action per specialist per role, and a paused graph is
                # NEVER invoked with a new message. A plain read-only question goes to the Report munshi
                # instead (its own thread namespace, no write tools for any role); anything else is
                # answered with the card it is waiting behind, so it can be decided right there.
                held = sorted((p for p in self.repo.pending_approvals(thread_id) if p["specialist"] == specialist and p["requested_by_role"] == role),
                              key=lambda p: p["created_at"])
                if held:
                    if self._read_only_question(text) and self._report_answers(role, thread_id):
                        specialist = tr.specialist = "report"
                    else:
                        pa = self._pa(held[0])
                        txt = self._still_waiting(pa)
                        self.repo.add_chat(thread_id, "munshi", txt, {"specialist": specialist, "waiting_on": pa.approval_id})
                        tr.response_text = txt
                        return Reply(txt, specialist, None, thread_id, waiting=pa)

                bundle = self.specialists[specialist]
                cfg = self._cfg(thread_id, role, specialist)
                self._clear_orphaned_interrupt(bundle, cfg)
                result = bundle.agent.invoke({"messages": [HumanMessage(text)], "role": role}, config=cfg)
                reply = self._settle(bundle, specialist, thread_id, role, user, result, tr)
                self.repo.add_chat(thread_id, "munshi", reply.text, {"specialist": specialist} | ({"approval_id": reply.pending.approval_id} if reply.pending else {}))
                return reply

    # ------------------------------------------------------------------ pausing on a gated call
    _MAX_DECLINES = 3

    def _settle(self, bundle, specialist: str, thread_id: str, role: str, user: str, result: dict, tr, lead: str = "") -> Reply:
        """Turn a specialist run into a Reply. If the run paused on a gated call,
        persist exactly one approval card for it -- unless the call references
        something that doesn't exist (or the pause holds more than one action,
        which OneGatedCallPerStep prevents), in which case the call is declined
        back to the agent so the thread is never left hanging on an unanswered
        tool call, and the human is told plainly that it was not done.
        `lead` is set when settling a resumed turn ("Approved. "): whatever
        follows is a follow-up to an action that has already been decided."""
        problems: list[str] = []
        for _ in range(self._MAX_DECLINES + 1):
            interrupts = result.get("__interrupt__")
            if not interrupts:
                break
            value = interrupts[0].value
            reqs = value.get("action_requests", [])
            problem = "only one action needing approval can be asked for at a time" if len(reqs) != 1 else self._unresolvable(reqs[0]["name"], reqs[0]["args"])
            if problem is None:
                return self._open_card(bundle, specialist, thread_id, role, user, reqs[0], value.get(DEFERRED_KEY, []), problems, tr, lead)
            log.warning("declined gated call on %s/%s: %s", thread_id, specialist, problem)
            problems.append(problem)
            if len(problems) > self._MAX_DECLINES:
                break
            result = bundle.agent.invoke(Command(resume={"decisions": [{"type": "reject", "message": f"Not asked for: {problem}."}] * max(len(reqs), 1)}),
                                         config=self._cfg(thread_id, role, specialist))
        if problems and lead:
            txt = f"{lead}A follow-up action couldn't be asked for — {problems[0]}. That follow-up was not done."
        elif problems:
            txt = f"Couldn't ask for approval — {problems[0]}. Nothing was done."
        else:
            txt = self._final_text(result)
        tr.response_text = txt
        return Reply(txt, specialist, None, thread_id)

    def _open_card(self, bundle, specialist: str, thread_id: str, role: str, user: str, req: dict, deferred: list[dict], problems: list[str], tr,
                   lead: str = "") -> Reply:
        tool = req["name"]
        pa = PendingApproval(uuid.uuid4().hex[:10].upper(), thread_id, specialist, tool, req["args"],
                             risk_of(tool).value, self._needs_role(tool, req["args"]), role, user)
        self.repo.save_approval(asdict(pa))
        later = ""
        if deferred:
            later = (" Only one action needing approval can be asked for at a time — not asked for yet: "
                     + "; ".join(self._summary(d.get("name", "?"), d.get("args") or {}) for d in deferred)
                     + ". Ask again for it once this one is decided.")
        card = self.card(pa)
        self.repo.notify(pa.needs_role, "approval", f"{bundle.title} wants to: {self._headline(card)}" + (f" (asked by {user})" if user else ""), pa.approval_id)
        tr.required_approval = True; tr.tool_called = tool
        skipped = f"(Skipped: {problems[0]}.) " if problems else ""
        effect = f" {card['effect']}" if card["effect"] else ""
        txt = f"{lead}{skipped}{bundle.title} {'next ' if lead else ''}wants to: {self._headline(card)}.{effect} Needs {pa.needs_role} approval.{later}"
        return Reply(txt, specialist, pa, thread_id)

    # ------------------------------------------------------------------ cards
    def card(self, pa: PendingApproval) -> dict:
        """The human-readable approval card (see CardBuilder). Read-only; never raises."""
        return self.cards.build(pa)

    @staticmethod
    def _headline(card: dict) -> str:
        total = card.get("total")
        amount = _en({"rs": total}) if total is not None else ""
        return card["title"] + (f", {amount}" if amount and amount not in card["title"] else "")

    def pending_out(self, pa: PendingApproval) -> dict:
        """A pending approval as the API returns it: every stored field, the one-line summary, and the card."""
        return asdict(pa) | {"summary": pa.describe(), "card": self.card(pa)}

    def decision_refusal(self, pa: PendingApproval, role: str, user: str = "") -> str | None:
        """Why this person may not approve this card right now, or None if they may. The same check
        resolve() makes at decision time: the requirement re-read from the books (it can only get
        stricter), then safety.risk.approval_refusal, which owns the policy."""
        need = stricter_role(pa.needs_role, self._needs_role(pa.tool, pa.args))
        return approval_refusal(role, pa.tool, need, approver=user, requester=pa.requested_by)

    def viewer_decision(self, pa: PendingApproval, role: str, user: str = "") -> dict:
        """What this viewer can do with this card: can_approve, and if not, a plain reason (with a stable
        code the app translates). Only the wording is chosen here; the decision is decision_refusal()'s."""
        why = self.decision_refusal(pa, role, user)
        need = stricter_role(pa.needs_role, self._needs_role(pa.tool, pa.args))
        mine = bool(user.strip() and pa.requested_by.strip() and same_person(user, pa.requested_by))
        if why is None:
            return {"can_approve": True, "blocked_reason": None, "blocked_code": None, "is_requester": mine, "needs_role_now": need}
        if role not in ("owner", "clerk"):
            code, txt = "not_approver", "Waiting for the office to approve."
        elif mine:
            code, txt = ("own_request_owner", "You asked for this: waiting for the owner to approve.") if need == "owner" else \
                        ("own_request", "You asked for this: waiting for the owner (or another clerk) to approve.")
        elif need == "owner" and role != "owner":
            code, txt = "needs_owner", "Only the owner can approve this: waiting for the owner."
        elif not user.strip():
            code, txt = "sign_in", "Sign in as yourself to approve this."
        else:
            code, txt = "refused", why
        return {"can_approve": False, "blocked_reason": txt, "blocked_code": code, "is_requester": mine, "needs_role_now": need}

    def _still_waiting(self, pa: PendingApproval) -> str:
        card = self.card(pa)
        who = "the owner needs to approve" if pa.needs_role == "owner" else "another clerk or the owner needs to approve"
        return f"Still waiting for approval: {self._headline(card)} — {who}. Approve or reject it here, then ask again."

    # A read-only question may be answered by the Report munshi while a write waits. Deliberately
    # conservative: it needs a question cue AND no action word at all. Getting this wrong is safe in
    # both directions -- the Report munshi has no write tools (checked below against the registry),
    # and a message that isn't recognised keeps the old block, now showing the waiting card.
    _READ_CUE = re.compile(r"[?؟]|\b(how much|how many|what|which|who|whose|show|list|tell me|kitna|kitni|kitne|kya|kaun|kis|kab|balance|khata|outstanding|baqi|"
                           r"stock|report|sales|bikri|profit|munafa|margin|cashbook|digest|summary|aging|owe|owes|valuation|position)\b", re.I)
    _WRITE_CUE = re.compile(r"\b(create|confirm|cancel|approve|allocate|dispatch|load|transfer|move|adjust|restock|write.?off|damaged|record|pay|paid|receive|received|"
                            r"deposit|handed|credit note|refund|waive|reverse|reversal|undo|bounced?|send|remind|reminder|draft|promise|book|bhej|bhejo|bhej do|chahiye|"
                            r"de do|kar do|karo|likh|mansookh|haan|yes|ok|okay|add|delete|remove|set|change|update|edit|place|new order|expense|kharcha)\b", re.I)

    def _read_only_question(self, text: str) -> bool:
        return bool(self._READ_CUE.search(text)) and not self._WRITE_CUE.search(text)

    def _report_answers(self, role: str, thread_id: str) -> bool:
        """The Report munshi can take this role's question: it has tools for the role, every one of
        them READ_ONLY in the risk registry, and nothing of its own is waiting on this thread."""
        tools = self.specialists["report"].role_tools.get(role) or []
        if not tools or any(risk_of(t) != RiskTier.READ_ONLY for t in tools):
            return False
        return not any(p["specialist"] == "report" and p["requested_by_role"] == role for p in self.repo.pending_approvals(thread_id))

    @staticmethod
    def _summary(tool: str, args: dict) -> str:
        try:
            return PendingApproval("", "", "", tool, args, "", "clerk", "").describe()
        except Exception:
            return f"{tool}({args})"

    # Required arguments that name a record. Every gated tool that takes one of these requires it.
    # entry_id / expense_id / purchase_id: the four reversal tools (reverse_ledger_entry, reverse_supplier_entry,
    # reverse_expense, reverse_purchase) each take exactly one of these and nothing else that names a record.
    _REFS = {"order_id": "order", "customer_id": "customer", "plan_id": "dispatch plan", "supplier_id": "supplier", "reminder_id": "reminder",
             "entry_id": "entry to reverse", "expense_id": "expense to reverse", "purchase_id": "purchase to reverse"}

    def _unresolvable(self, tool: str, args: dict) -> str | None:
        """Why no card should be raised for this call, or None. A card must never
        read "Confirm order ." -- a required reference that is blank means the
        model didn't find one, so there is nothing a human could approve.
        (A well-formed id that doesn't exist still gets a card and fails safely
        on approve with "no such ..."; eval step cancel_unknown_order pins that.)"""
        refs = [(k, v) for k, v in args.items() if k in self._REFS] + [("order_id", v) for v in (args.get("order_ids") or [])]
        # a required reference the call left out altogether is as blank as an empty one
        fn = getattr(self.ops, tool, None)
        if fn is not None:
            refs += [(n, args.get(n)) for n, prm in inspect.signature(fn).parameters.items()
                     if n in self._REFS and prm.default is inspect.Parameter.empty and n not in args]
        for key, raw in refs:
            if not str(raw or "").strip():
                return f"no {self._REFS[key]} was named"
        return None

    def _clear_orphaned_interrupt(self, bundle, cfg: dict) -> None:
        """A paused graph with no card behind it (e.g. a thread wedged before this
        fix, or a resume that crashed) would carry an unanswered tool call into
        the next model request, which provider APIs refuse. Decline it first."""
        try:
            state = bundle.agent.get_state(cfg)
        except Exception:
            return
        for _ in range(self._MAX_DECLINES):
            if not state.interrupts:
                return
            n = max(len(state.interrupts[0].value.get("action_requests", [])), 1)
            log.warning("declining orphaned interrupt on %s", cfg["configurable"]["thread_id"])
            try:
                bundle.agent.invoke(Command(resume={"decisions": [{"type": "reject", "message": "Superseded: the user sent a new message instead."}] * n}), config=cfg)
                state = bundle.agent.get_state(cfg)
            except Exception:
                log.exception("couldn't clear orphaned interrupt")
                return

    def resolve(self, approval_id: str, approve: bool, role: str, note: str = "", user: str = "") -> Reply:
        # The decision is the approver's act; the action it releases is the requester's. So the approval
        # record is written as `user`, and the resumed turn runs as `pa.requested_by` with the approver
        # alongside (audit approved_by) -- never as the approver, or the requester would vanish from
        # created_by / the audit trail and could later clear their own request on the direct routes.
        with self._lock, self.repo.acting_as(user):
            pa = self.pending.get(approval_id)
            if not pa:
                raise KeyError(f"no pending approval {approval_id}")
            if approve:
                # re-check at decision time: the requirement can only get stricter (e.g. the order went over limit meanwhile)
                why = self.decision_refusal(pa, role, user)
                if why:
                    raise PermissionError(why)
            bundle = self.specialists[pa.specialist]
            decision = {"type": "approve"} if approve else {"type": "reject", "message": note or f"rejected by {role}"}
            with self._trace(f"{pa.specialist}.resume", role, f"{'approve' if approve else 'reject'} {pa.tool}") as tr:
                tr.approval_decision = "approve" if approve else "reject"
                # the approval is on record BEFORE the tool runs: the audit trail never shows a write ahead of its approval
                self.repo.resolve_approval(approval_id, approve, user or role, note)
                self.repo.audit(role, "approval_" + ("granted" if approve else "rejected"), "approval", approval_id,
                                {"tool": pa.tool, "args": pa.args, "specialist": pa.specialist, "note": note}, approved_by=role)
                signature = (f"{role}:{user}" if user.strip() else role) if approve else ""
                try:
                    with self.repo.acting_as(pa.requested_by, approved_by=signature):
                        result = bundle.agent.invoke(Command(resume={"decisions": [decision]}), config=self._cfg(pa.thread_id, pa.requested_by_role, pa.specialist))
                except Exception as e:      # the graph state is gone (e.g. checkpoints wiped); the approval stays resolved but unexecuted
                    log.exception("resume failed for %s", approval_id)
                    txt = f"Couldn't resume that action ({type(e).__name__}); please ask the munshi again."
                    self.repo.add_chat(pa.thread_id, "munshi", txt, {"specialist": pa.specialist, "resolved": approval_id, "approved": approve, "error": True})
                    return Reply(txt, pa.specialist, None, pa.thread_id)
                # The resumed turn may go on to ask for another gated action ("confirm A, then B"): that gets its
                # own card, on the requester's behalf, instead of a bare "Done." over a paused graph. Anything the
                # agent does while settling is the requester's too, but no longer covered by this approval.
                with self.repo.acting_as(pa.requested_by):
                    reply = self._settle(bundle, pa.specialist, pa.thread_id, pa.requested_by_role, pa.requested_by, result, tr,
                                         lead="Approved. " if approve else "Rejected. ")
                meta = {"specialist": pa.specialist, "resolved": approval_id, "approved": approve}
                self.repo.add_chat(pa.thread_id, "munshi", reply.text, meta | ({"approval_id": reply.pending.approval_id} if reply.pending else {}))
                if approve: self.deliver_messages()
                return reply

    def pending_items(self, thread_id: str | None = None) -> list[PendingApproval]:
        """Pending approvals, oldest first."""
        return sorted((self._pa(a) for a in self.repo.pending_approvals(thread_id)), key=lambda p: p.created_at)

    def list_pending(self, thread_id: str | None = None) -> list[dict]:
        return [self.pending_out(p) for p in self.pending_items(thread_id)]

    def get_pending(self, approval_id: str) -> PendingApproval | None:
        return self.pending.get(approval_id)

    def deliver_messages(self) -> dict:
        """Push the outbox through the configured channel (no-op without one)."""
        try:
            return deliver_outbox(self.repo, self.channel)
        except Exception as e:
            log.warning("outbox delivery failed: %s", e)
            return {"sent": 0, "failed": 0, "error": str(e)}
