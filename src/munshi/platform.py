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
read-only question goes to the Report munshi; anything else runs on a fresh
graph LANE of its own ('{thread}:{role}:{specialist}:q{approval id}', recorded
in the chat log, so resolve() resumes exactly that graph) -- an independent
request gets its own card, a read its answer; what can't stand alone (a
duplicate of the waiting card, a bare 'yes') gets the waiting card back
(Reply.waiting) so it can be decided right there. A correction of the card the
person just asked for ('galti, 40 kar do') withdraws it and raises the corrected
one. A batch ('sab confirm kar do') is one card per record, chained: the next is
raised when the previous one is decided (resolve(), 'Next, 2 of 6').

Never a card that can't succeed: _cannot() reads the books first (a draft to
edit, stock at the godown to reserve, a route/vehicle/godown for a plan, stock
to load) and says why instead.

Two engines, deterministic first. The RULES engine (manager + specialists on the
offline language layer, llm/*) always runs first. With a real model configured
(LLM_PROVIDER=groq) there is also a MODEL engine (the same specialists and tools on
the real model, with agents.guard.EntityGuard checking every tool call against the
message). A message goes to the model engine only when the rules engine explicitly
did not understand it: its manager routed nowhere, or its specialist ended the turn
with the stub's NOT_UNDERSTOOD outcome (llm.stub_model.not_understood). A card, a
read, a refusal or a clarifying question from the rules engine is the reply, and no
model is called. If the model call fails (timeout, rate limit, provider error) the
rules engine's own reply is used. With no real model, only the rules engine exists
and nothing here changes.

The engines never share a paused graph. Model-engine checkpoints live under
"{thread}:{role}:{specialist}:llm" (rules: "{thread}:{role}:{specialist}"; a rules key
always ends in a specialist name, so the two can't collide), and a card raised by the
model engine has an approval id starting with MODEL_APPROVAL_PREFIX ("L"; rules ids are
upper-case hex, 0-9A-F). resolve() reads the engine off the id and resumes that
engine's graph from the shared (SQLite) checkpointer, so it survives a restart -- even
one with the model switched off (the model engine's graph is then rebuilt on the
offline model just to finish the approved action).
"""
from __future__ import annotations

import inspect
import json
import logging
import re
import sqlite3
import threading
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from munshi.agents import guard
from munshi.agents.factory import is_real_model
from munshi.agents.manager import CLARIFY, build_manager, classify
from munshi.agents.specialists import BUILDERS, order_label, orders_of
from munshi.channels import build_channel, deliver_outbox
from munshi.domain.models import discounted_paisa, to_paisa, to_rupees, today_iso
from munshi.domain.repository import MunshiRepository
from munshi.domain.seed import seeded_repository
from munshi.llm import answers
from munshi.llm import followup as FU
from munshi.llm import memory as MEM
from munshi.llm import replies as RP
from munshi.llm import turns as TURNS
from munshi.llm.answers import lang_of
from munshi.llm.parse import analyse_order, catalogue, customer_resolution, otp_in, prepare, supplier_resolution
from munshi.llm.stub_model import ask_of, not_understood
from munshi.llm.text import fold, is_urdu
from munshi.observability.tracing import TurnTrace, configure_tracking, trace_turn
from munshi.safety.middleware import DEFERRED_KEY
from munshi.safety.risk import RiskTier, approval_refusal, approver_for, risk_of, same_person, stricter_role
from munshi.tools.core import MunshiTools

log = logging.getLogger("munshi.platform")

RULES, MODEL = "rules", "model"
MODEL_APPROVAL_PREFIX = "L"          # approval ids raised by the model engine; the rules engine's are hex (0-9A-F)
# said instead of a model reply that claims an action no tool took
NOTHING_DONE = "Nothing was recorded -- I couldn't turn that into an action."
NOTHING_DONE_UR = "کچھ درج نہیں ہوا۔"
# A reply to a tool result is a readable sentence (llm/answers.py) followed by this marker and the raw result: the
# app folds the raw part away under "details"; the eval and anyone debugging a turn still have every field.
DETAILS = "\n\nDone -- "


def visible(text: str) -> str:
    """The part of a reply a person reads (without the folded details block)."""
    return str(text or "").split(DETAILS, 1)[0]


def engine_of(approval_id: str) -> str:
    """Which engine raised this approval (and so which graph resumes it)."""
    return MODEL if str(approval_id).startswith(MODEL_APPROVAL_PREFIX) else RULES


class _ModelCalls(BaseCallbackHandler):
    """Counts chat-model requests in one turn (cost/latency visibility)."""

    def __init__(self) -> None:
        self.n = 0
        self.tokens = 0

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        self.n += 1

    def on_llm_end(self, response, **kwargs) -> None:
        try:
            usage = (response.llm_output or {}).get("token_usage") or {}
            self.tokens += int(usage.get("total_tokens") or 0)
        except Exception:
            pass


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
        if t == "update_order":
            lines = ", ".join(f"{i.get('sku')} → {i.get('qty')}" for i in a.get("items", []) if isinstance(i, dict))
            return f"Change draft order {a.get('order_id')}: {lines}"
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
    engine: str = RULES                         # which engine produced the reply: rules | model
    model_calls: int = 0                        # real-model requests this turn made (0 when the rules answered)
    model_tokens: int = 0                       # tokens those requests used, as the provider reported them
    model_error: str | None = None              # the model failed and the rules' reply was used instead
    ask: dict | None = None                     # the one missing piece this reply asks for ({"slot", "candidates"}), if any
    tool: str | None = None                     # the (last) tool the specialist called this turn, if any
    listed: list | None = None                  # the order ids a list reply showed (what 'sab confirm kar do' then means)
    extra: dict = field(default_factory=dict)   # chat-log meta the turn adds: the graph lane of each card, a chain of cards to come


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
    "t_update_order": "Change order for {customer}",
    "t_update_order_id": "Change order {order}",
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
    "order_change": "{changes}. Total {before} → {after}. It stays a draft; nothing is owed until it is delivered.",
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
    "w_empty_order": "No lines would be left: cancel the order instead. Approving will fail.",
    "w_over_capacity": "{units} units won't fit: the vehicle holds {capacity}.",
    "w_overpay": "More than is owed: the balance goes to {after}.",
    "w_cash_short": "Cash short by {gap}.",
    "w_paid_over_total": "Paid now ({paid}) is more than the bill ({total}): approving will fail.",
    "w_is_reversal": "{entry} is itself a reversal: approving will fail.",
    "w_already_reversed": "{entry} was already reversed by {by}: approving will fail.",
    "w_bill_of_purchase": "Bill {entry} came with purchase {purchase}: reverse the purchase instead, so the goods go back too.",
    "w_already_sent": "Reminder {reminder} was already sent.",
    "w_not_overdue": "{customer} isn't overdue yet: the message would say '0 din'. Send it only if you mean to.",
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
        facts = list(body.get("facts", [])) + self._memory_facts(pa.approval_id)
        return base | {
            "title": title["text"], "title_key": title["key"], "title_vars": title["vars"],
            "effect": effect["text"] if effect else "", "effect_key": effect["key"] if effect else None, "effect_vars": effect["vars"] if effect else {},
            "lines": body.get("lines", []), "total": body.get("total"),
            "facts": facts, "quote": body.get("quote"),
            "warnings": [{"code": w["key"][2:], "text": w["text"], "key": w["key"], "vars": w["vars"]} for w in body.get("warnings", [])],
            "fallback": fallback,
        }

    def _memory_facts(self, approval_id: str) -> list[dict]:
        """A card whose customer / supplier / product came from a learned name says so, in plain words, so the approver
        can catch a wrong memory: 'Bhatti sahab = Bhatti Kisan Store (remembered)', or right after a re-point
        'Bhatti sahab = Bhatti Traders (last time you meant Bhatti Kisan Store)'."""
        out = []
        for ln in (self._get(self.repo.card_links, str(approval_id or "")) or []):
            if ln["link"] != "used":
                continue
            getter = {"customer": self.repo.get_customer, "supplier": self.repo.get_supplier, "product": self.repo.get_product}.get(ln["entity_kind"])
            if getter is None:
                continue
            prev = self._name(getter, ln["previous_entity_id"]) if ln["previous_entity_id"] else None
            out.append({"key": "note", "value": MEM.note_text(ln["phrase"], self._name(getter, ln["entity_id"]), prev)})
        return out

    # ---------------------------------------------------------- order desk
    def _c_create_order(self, a: dict, pa) -> dict:
        cid = str(a.get("customer_id") or ""); cust = self._cust(cid); cname = cust.name if cust else cid
        lines, warns, need, negotiated = [], [], {}, []
        total_p = list_total_p = 0
        for it in a.get("items") or []:
            sku, qty = str(it.get("sku") or ""), int(it.get("qty") or 0)
            p = self._prod(sku)
            if p is None:
                warns.append(_t("w_unknown_product", sku=sku)); lines.append(self._line(sku, qty, None)); continue
            list_p = to_paisa(p.unit_price)
            price_p = to_paisa(it["unit_price"]) if it.get("unit_price") not in (None, "", 0) else discounted_paisa(list_p, cust.discount_pct if cust else 0)
            lines.append(self._line(sku, qty, price_p))
            if it.get("unit_price") not in (None, "", 0) and price_p != discounted_paisa(list_p, cust.discount_pct if cust else 0):
                negotiated.append(f"{p.name} at {_en(_rs(price_p))} (negotiated; list {_en(_rs(discounted_paisa(list_p, cust.discount_pct if cust else 0)))})")
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
        facts = [{"key": "note", "value": "; ".join(negotiated)}] if negotiated else []
        return {"title": _t("t_create_order", customer=cname), "effect": effect, "lines": lines, "total": to_rupees(total_p), "warnings": warns,
                "facts": facts}

    def _c_update_order(self, a: dict, pa) -> dict:
        """A change to a draft: the lines as they will be, each product's quantity before -> after, and the total before -> after
        (priced by the repository's own rule: kept lines keep their drafted price, a new line gets the customer's price)."""
        oid = str(a.get("order_id") or ""); o = self._order(oid)
        if o is None:
            return self._missing("t_update_order_id", "order", oid, order=oid)
        cust = self._cust(o.customer_id); cname = cust.name if cust else o.customer_id
        new = self.ops.merged_lines(o, [i for i in a.get("items") or [] if isinstance(i, dict)])
        before_q = self._need(o.items)
        lines, warns, total_p = [], [], 0
        for it in new:
            p = self._prod(it["sku"])
            if p is None:
                warns.append(_t("w_unknown_product", sku=it["sku"])); lines.append(self._line(it["sku"], it["qty"], None)); continue
            price_p = to_paisa(it["unit_price"]) if it.get("unit_price") not in (None, "", 0) else discounted_paisa(to_paisa(p.unit_price), cust.discount_pct if cust else 0)
            lines.append(self._line(it["sku"], it["qty"], price_p, qty_before=before_q.get(it["sku"], 0)))
            total_p += int(it["qty"]) * price_p
        after_q = {it["sku"]: int(it["qty"]) for it in new}
        changes = [{"name": self._name(self.repo.get_product, sku), "before": before_q.get(sku, 0), "after": after_q.get(sku, 0)}
                   for sku in dict.fromkeys([*before_q, *after_q]) if before_q.get(sku, 0) != after_q.get(sku, 0)]
        if o.status != "draft":
            warns.insert(0, _t("w_bad_status", what=_word("order"), id=oid, status=_word(o.status)))
        if not new:
            warns.insert(0, _t("w_empty_order"))
        if cust:
            warns += self._credit_warning(o.customer_id, cname, self._customer_balance(o.customer_id) + total_p)
        limit = float(self.repo.setting("big_order_limit") or 0)
        list_total = sum(int(it["qty"]) * (self._prod(it["sku"]).unit_price if self._prod(it["sku"]) else 0) for it in new)
        if limit and list_total > limit:
            warns.append(_t("w_big_order", limit={"rs": limit}))
        grow = {sku: q - before_q.get(sku, 0) for sku, q in after_q.items() if q > before_q.get(sku, 0)}
        warns += self._stock_warnings(grow)
        return {"title": _t("t_update_order", customer=cname), "lines": lines, "total": to_rupees(total_p), "warnings": warns,
                "effect": _t("order_change", changes=changes or [{"name": "-", "before": 0, "after": 0}], before=_rs(o.total_paisa), after=_rs(total_p)),
                "facts": [{"key": "note", "value": f"Draft {oid}, total before {_en(_rs(o.total_paisa))}"}]}

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
        warns = [_t("w_not_overdue", customer=cust.name)] if int(ag["days_overdue"] or 0) <= 0 else []
        return {"title": _t("t_draft_reminder", tier=_word(tier), customer=cust.name), "total": ag["balance"], "warnings": warns,
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
        self._checkpointer = checkpointer
        # the rules engine: always there, always first
        self.specialists = {name: build(self.ops, self.repo, None, checkpointer) for name, build in BUILDERS.items()}
        self._built = dict(self.specialists)     # the offline rules' own munshis (a test may swap one for a scripted model)
        self.manager = build_manager(None, self.repo)
        # the model engine: only with a real model, only for what the rules didn't understand
        self.model = model if is_real_model(model) else None
        self.llm_specialists: dict | None = None
        self.llm_manager = None
        if self.model is not None:
            self.llm_specialists = {name: build(self.ops, self.repo, self.model, checkpointer) for name, build in BUILDERS.items() if name != "help"}
            self.llm_manager = build_manager(self.model)
        self._resume_only: dict = {}
        self._model_down_until: datetime | None = None     # a rate-limited provider isn't asked again until then
        self.cards = CardBuilder(self.repo, self.ops)
        self._from_chat: dict | None = None     # the remembered customer/supplier this turn's message leaned on (named on its card)
        self._topic: dict | None = None         # this turn's remembered topic (handed to the model guard)
        self._answered: dict | None = None      # the open question this turn's message answered, if it did (llm/followup.py)
        self._said: str = ""                    # the text the engine now answering is reading (a card's wording, for memory)

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
        if tool in ("create_order", "update_order") and need == "clerk":
            try:
                limit = float(self.repo.setting("big_order_limit") or 0)
                items = args.get("items", []) if tool == "create_order" else self.ops.merged_lines(self.repo.get_order(args.get("order_id", "")), args.get("items", []))
                total = sum(int(i["qty"]) * self.repo.get_product(i["sku"]).unit_price for i in items)
                if limit and total > limit:
                    return "owner"
            except Exception:
                pass
        return need

    def _cfg(self, thread_id: str, role: str, specialist: str, engine: str = RULES) -> dict:
        # Role is part of the checkpoint key: a driver and a clerk on the same
        # conversation never share a specialist's memory or its pending action.
        # So is the engine: the model engine's graphs live under their own ':llm' keys.
        return {"configurable": {"thread_id": f"{thread_id}:{role}:{specialist}" + (":llm" if engine == MODEL else "")}}

    @property
    def hybrid(self) -> bool:
        return self.llm_specialists is not None

    def _bundle(self, engine: str, specialist: str):
        if engine == RULES:
            return self.specialists[specialist]
        if self.llm_specialists is not None and specialist in self.llm_specialists:
            return self.llm_specialists[specialist]
        # A model-engine card, but no model configured now (e.g. restarted offline): the same graph -- same
        # tools, same approval gate and guard -- on the offline model, only to finish the approved action.
        if specialist not in self._resume_only:
            self._resume_only[specialist] = BUILDERS[specialist](self.ops, self.repo, None, self._checkpointer, guarded=True)
        return self._resume_only[specialist]

    def _trace(self, agent: str, role: str, text: str):
        return trace_turn(agent, role, text) if self.enable_tracing else nullcontext(TurnTrace(agent_name=agent, role=role, user_text=text))

    def _readable(self, msgs: list, i: int) -> str | None:
        """The readable sentence for the tool result at msgs[i] (a ToolMessage), in the language of the message it
        answers; None if there is no formatter for it."""
        tm = msgs[i]
        args = next((tc.get("args") or {} for m in reversed(msgs[:i]) if isinstance(m, AIMessage) for tc in (m.tool_calls or [])
                     if tc.get("id") == tm.tool_call_id), {})
        human = next((str(m.content) for m in reversed(msgs[:i]) if isinstance(m, HumanMessage)), "")
        return answers.render(str(tm.name or ""), str(tm.content), self.repo, human, args)

    def _final_text(self, result: dict) -> str:
        msgs = result["messages"]
        msg = msgs[-1]
        # Gemini (and other providers) return a list of content parts; .text joins the text parts in order
        content = msg.content if isinstance(msg.content, str) else str(msg.text)
        # the offline model's one-line summary of a tool result ("Done -- {json}"): a sentence, with the raw result folded after it
        if isinstance(msg, AIMessage) and content.startswith("Done -- ") and len(msgs) >= 2 and isinstance(msgs[-2], ToolMessage):
            nice = self._readable(msgs, len(msgs) - 2)
            if nice:
                raw = content[len("Done -- "):]
                return nice + (DETAILS + raw if raw.lstrip()[:1] in ("{", "[") else "")
        return content or "Done."

    # ------------------------------------------------------------------ conversation memory (llm/followup.py)
    _MEMO_ROWS = 16

    def _memory(self, thread_id: str, role: str) -> tuple[dict | None, dict | None]:
        """This role's open question and topic on this thread, if still fresh. Stored in the chat log's meta (the
        munshi's reply rows), so a restart loses nothing; another role's turns never touch it."""
        for r in reversed(self.repo.chat_history(thread_id, self._MEMO_ROWS)[:-1]):
            memo = (r.get("meta") or {}).get("memo") if r.get("role") == "munshi" else None
            if not memo or memo.get("role") != role:
                continue
            ask, topic = memo.get("ask"), memo.get("topic")
            ask = ask if ask and FU.fresh(ask.get("at")) and int(ask.get("left", 0)) >= 0 else None
            topic = topic if topic and FU.fresh(topic.get("at")) else None
            return ask, topic
        return None, None

    def handle_message(self, thread_id: str, role: str, text: str, user: str = "") -> Reply:
        # Every write this turn makes -- including any tool the agent runs on a worker thread -- is `user`'s.
        with self._lock, self.repo.acting_as(user):
            self.repo.add_chat(thread_id, role, text, {"user": user})
            try:
                said = self._memory_command(role, text, user)
            except Exception:
                log.exception("memory command failed on %r", text[:80])
                said = None
            if said is not None:                # 'Bhatti sahab matlab Bhatti Traders hai', 'forget X', 'kya kya yaad hai'
                reply = Reply(said[0], None, None, thread_id)
                self.repo.add_chat(thread_id, "munshi", reply.text, {"specialist": None, "memory": said[1]})
                return reply
            try:
                ask, topic = self._memory(thread_id, role)
            except Exception:
                log.exception("couldn't read the conversation memory of %s", thread_id)
                ask, topic = None, None
            run, force, used, answered = text, None, None, False
            try:
                done = FU.answer(ask, text, self.repo) if ask else None
                if done:                        # the message answers the open question: the original request, completed
                    run, force = done[0], done[1] or ask.get("specialist")
                    used, answered = ask.get("used"), True
                else:
                    run, used = FU.augment(text, topic, self.repo)
            except Exception:
                log.exception("follow-up reading failed on %r", text[:80])
                run, force, used, answered = text, None, None, False
            self._from_chat = used if used and used.get("kind") in ("customer", "supplier") else None
            self._topic = topic
            self._answered = ask if answered else None
            try:
                with self._trace("manager", role, text) as tr:
                    reply, meta, understood = self._rules_turn(thread_id, role, text, run, user, tr, force, answered)
                    if not understood and self.hybrid:
                        # the model reads the message as the user wrote it (a completed open question: the whole request),
                        # with the remembered customer in its context note; the guard accepts that customer by the same rule
                        reply, meta = self._model_turn(thread_id, role, run if answered else text, user, tr, reply, meta)
                    meta = meta | ({"completed": run} if answered else {"read_as": run} if run != text else {}) | (reply.extra or {})
                    try:
                        meta["memo"] = self._remember(role, text, run, reply, ask, topic, used, answered)
                    except Exception:
                        log.exception("couldn't update the conversation memory of %s", thread_id)
                    if answered and reply.pending is None:
                        try:
                            learned = self._learn_from_answer(role, user, ask, run, reply)
                            if learned:
                                meta["learned"] = learned
                        except Exception:
                            log.exception("couldn't learn from the answer on %s", thread_id)
                    self.repo.add_chat(thread_id, "munshi", reply.text, meta)
                    return reply
            finally:
                self._from_chat = self._topic = self._answered = None
                self._said = ""

    def _remember(self, role: str, text: str, run: str, reply: Reply, ask: dict | None, topic: dict | None, used: dict | None,
                  answered: bool) -> dict:
        """What this role's conversation carries into its next message: the open question (if this reply asked
        one, or an unanswered one survives a bit of small talk) and the topic."""
        at = FU.now().isoformat()
        new_ask = None
        if reply.ask and not reply.pending:
            new_ask = dict(reply.ask) | {"text": reply.ask.get("text") or run, "specialist": reply.ask.get("specialist") or reply.specialist, "at": at,
                                         "left": FU.MAX_CHATTER, "used": used}
        elif ask and not answered and not reply.pending and reply.specialist in (None, "help") and int(ask.get("left", 0)) > 0:
            new_ask = dict(ask) | {"left": int(ask.get("left", 0)) - 1}
        intent = {"get_stock": "stock", "list_orders": "orders"}.get(reply.tool or "")
        if intent == "stock" and not FU.entities(run, self.repo).get("product"):
            intent = None
        whole = FU.WHOLE_BUSINESS(text) and not FU.PRONOUN(text)
        new_topic = FU.next_topic(topic, run, self.repo, role, reply.pending.args if reply.pending else None, whole, intent)
        out = {"role": role, "ask": new_ask, "topic": new_topic}
        if reply.listed:
            out["listed"] = {"ids": list(reply.listed)[:40], "at": at}
        return out

    # ------------------------------------------------------------------ memory across conversations (llm/memory.py)
    # Learned names are taught ONLY by a human confirming who they meant: approving a card made from their wording
    # (settled in resolve()), or -- owner / clerk -- picking from a "which one?" question, or saying so in chat.
    # They are applied by the resolver (llm.resolve.with_memory), the same function the rules, the topic memory and
    # the model guard all read with, so every engine agrees on them; and a card that relied on one names it.
    def _resolution(self, kind: str, text: str, memory: bool = True):
        return (customer_resolution if kind == "customer" else supplier_resolution)(text, self.repo, memory=memory)

    def _entity_name(self, kind: str, rid: str) -> str:
        try:
            return {"customer": self.repo.get_customer, "supplier": self.repo.get_supplier, "product": self.repo.get_product}[kind](rid).name
        except Exception:
            return ""

    def _answer_link(self, kind: str, rid: str, base: str, user: str) -> dict | None:
        """The phrase a "which one?" question was about (in the request it asked about), when the answer picked one of
        the options it offered: 'Malik ka khata' + 'doosra' -> ('Malik', C-012)."""
        res = self._resolution(kind, base)
        if res.status != "ambiguous" or rid not in {c.id for c in res.candidates}:
            return None
        phrase = MEM.phrase_of(base, prepare(base, catalogue(self.repo)).tokens, res.evidence, self._entity_name(kind, rid))
        return MEM.learn_link(kind, rid, phrase, "answer", user) if phrase else None

    def _memory_links(self, args: dict, text: str, role: str, user: str) -> list[dict]:
        """What a card about to be raised owes to memory ('used': a learned name decided who it is for) or may teach it
        ('learn': the wording that named them, learned only if the card is approved -- see resolve())."""
        links: list[dict] = []
        if not text:
            return links
        fc = self._from_chat
        for kind, key in (("customer", "customer_id"), ("supplier", "supplier_id")):
            rid = str(args.get(key) or "")
            if not rid or (fc and fc.get("id") == rid):          # the conversation named them, not this wording
                continue
            res = self._resolution(kind, text)
            if res.ok and res.id == rid and res.alias:
                links.append(MEM.used_link(kind, res.alias))
                continue
            if res.ok and res.id == rid and res.candidates and res.candidates[0].via in ("name", "head"):
                phrase = MEM.phrase_of(text, prepare(text, catalogue(self.repo)).tokens, res.evidence, res.name)
                if phrase:
                    links.append(MEM.learn_link(kind, rid, phrase, "card", user))
                continue
            ask = self._answered                                 # the card completes a "which one?" question
            if ask and ask.get("slot") == kind and rid in {str(c.get("id")) for c in ask.get("candidates") or []}:
                link = self._answer_link(kind, rid, str(ask.get("text") or ""), user)
                if link:
                    links.append(link)
        skus = {str(i.get("sku")) for i in (args.get("items") or []) if isinstance(i, dict)}
        if skus:
            toks = prepare(text, catalogue(self.repo)).tokens
            for a in self.repo.learned_aliases("product"):
                ws = [w for w in MEM.words(MEM.fold(a["phrase"]))]
                if a["entity_id"] in skus and ws and any(toks[i:i + len(ws)] == ws for i in range(len(toks) - len(ws) + 1)):
                    links.append(MEM.used_link("product", {"alias_id": a["alias_id"], "phrase": a["phrase"], "phrase_norm": a["phrase_norm"],
                                                           "id": a["entity_id"], "previous_id": a["previous_entity_id"] if not a["uses"] else None}))
        return links

    def _learn_from_answer(self, role: str, user: str, ask: dict, run: str, reply: Reply) -> dict | None:
        """The owner or a clerk answered "which one?" and the munshi READ for the one they picked: that pick is a
        confirmation, learned now. (When the answer leads to a card instead, the card's approval teaches it.)"""
        kind = ask.get("slot")
        if role not in MEM.TEACHERS or kind not in ("customer", "supplier") or not reply.tool or self._is_write(reply.tool):
            return None
        res = self._resolution(kind, run)
        if not res.ok or not res.id:
            return None
        link = self._answer_link(kind, res.id, str(ask.get("text") or ""), user)
        if not link:
            return None
        r = self.repo.learn_alias(link["phrase_norm"], link["phrase"], kind, res.id, "answer", taught_by=user or role, confirmed_by=user or role)
        return {"phrase": link["phrase"], "kind": kind, "id": res.id, "status": r["status"]}

    def _memory_command(self, role: str, text: str, user: str) -> tuple[str, dict] | None:
        """(reply, meta) for a memory command -- teach / re-point, forget, list -- or None when the message isn't one."""
        cmd = MEM.command_of(text)
        if cmd is None:
            return None
        lang, who = lang_of(text), (user or role)
        no = (MEM.say("not_allowed", lang), {"cmd": cmd.kind, "refused": "role"})
        if cmd.kind == "list":
            return (self._memory_list(lang), {"cmd": "list"}) if role in MEM.TEACHERS else no
        if cmd.kind == "forget":
            key = MEM.phrase_key(cmd.phrase)
            if not any(a["phrase_norm"] == key for a in self.repo.learned_aliases()):
                return (MEM.say("not_known", lang, phrase=cmd.phrase), {"cmd": "forget", "known": False}) if MEM.valid_phrase(cmd.phrase) else None
            if role not in MEM.TEACHERS:
                return no
            closed = self.repo.forget_alias(key, by=who)
            names = ", ".join(dict.fromkeys(self._entity_name(a["entity_kind"], a["entity_id"]) or a["entity_id"] for a in closed))
            return MEM.say("forgot", lang, phrase=closed[0]["phrase"], name=names), {"cmd": "forget", "closed": [a["alias_id"] for a in closed]}
        # teach / re-point
        tgt = MEM.target_of(cmd.target, self.repo)
        if tgt.problem == "unknown":
            return (MEM.say("no_target", lang, target=cmd.target), {"cmd": "teach", "problem": "unknown"}) if MEM.valid_phrase(cmd.phrase) else None
        if role not in MEM.TEACHERS:
            return no
        if not MEM.valid_phrase(cmd.phrase):
            return MEM.say("bad_phrase", lang), {"cmd": "teach", "problem": "phrase"}
        if tgt.problem:
            return MEM.say("which_target", lang, target=cmd.target, options=" / ".join(tgt.options)), {"cmd": "teach", "problem": tgt.problem}
        owner = MEM.phrase_owner(cmd.phrase, tgt.kind, self.repo)
        if owner is not None and owner.id != tgt.id:
            return MEM.say("taken", lang, phrase=cmd.phrase, owner=owner.name, name=tgt.name), {"cmd": "teach", "problem": "taken"}
        r = self.repo.learn_alias(MEM.phrase_key(cmd.phrase), cmd.phrase, tgt.kind, tgt.id, "chat", taught_by=who, confirmed_by=who)
        prev = r["previous"]
        key = {"learned": "learned", "repointed": "repointed", "same": "same"}[r["status"]]
        txt = MEM.say(key, lang, phrase=cmd.phrase, name=tgt.name,
                      previous=(self._entity_name(prev["entity_kind"], prev["entity_id"]) or prev["entity_id"]) if prev else "")
        return txt, {"cmd": "teach", "status": r["status"], "kind": tgt.kind, "id": tgt.id}

    def _memory_list(self, lang: str) -> str:
        rows = [a for a in self.repo.alias_history(500) if a["active"]]
        if not rows:
            return MEM.say("list_empty", lang)
        out = [MEM.say("list_head", lang, n=len(rows))]
        for a in sorted(rows, key=lambda a: (a["entity_kind"], a["phrase"].lower())):
            prev = MEM.say("list_prev", lang, previous=self._entity_name(a["entity_kind"], a["previous_entity_id"]) or a["previous_entity_id"]) \
                if a["previous_entity_id"] else ""
            out.append(MEM.say("list_row", lang, phrase=a["phrase"], name=self._entity_name(a["entity_kind"], a["entity_id"]) or a["entity_id"],
                               kind=a["entity_kind"], who=a["taught_by"] or "?", day=str(a["taught_at"])[:10], uses=a["uses"], prev=prev))
        return "\n".join(out)

    def _ran(self, bundle, cfg: dict, tool: str) -> bool:
        """Did the approved call actually do its work (its tool result is not an error)?"""
        try:
            msgs = bundle.agent.get_state(cfg).values.get("messages", [])
        except Exception:
            return False
        tm = next((m for m in reversed(msgs) if isinstance(m, ToolMessage) and m.name == tool), None)
        return tm is not None and tm.status != "error" and not str(tm.content).lstrip().startswith('{"error"')

    def _settle_memory(self, pa: PendingApproval, approve: bool, learnable: bool, why_not: str, by: str) -> None:
        try:
            self.repo.settle_card_links(pa.approval_id, approve, learnable, by=by, why_not=why_not)
        except Exception:
            log.exception("couldn't settle memory for %s", pa.approval_id)

    def _bare_entity(self, text: str) -> str | None:
        """A message that is only a customer's or a supplier's name ('haji sons', 'Fauji?'): their khata."""
        for res, spec in ((customer_resolution(text, self.repo), "order"), (supplier_resolution(text, self.repo), "khareed")):
            if res.ok and res.other is None and not FU._leftover(text, lambda w, r=res: FU.is_res_word(w, r) or w in ("aur", "and", "?")):
                return spec
        return None

    # ------------------------------------------------------------------ the rules engine: messages about more than one request
    def _rules_turn(self, thread_id: str, role: str, text: str, run: str, user: str, tr, force: str | None, answered: bool) -> tuple[Reply, dict, bool]:
        """The rules engine's go at a message. Before the one-request path (_turn), in order: a question about the
        approvals queue; a correction of the card this person just asked for; a driver's 'customer wasn't there';
        a batch ('sab confirm kar do'); one message with orders for several customers; two questions asked at once."""
        ans = self._answered or {}
        if answered and ans.get("slot") == "split":
            return self._split_cards(thread_id, role, user, [str(c["id"]) for c in ans.get("candidates") or []], tr)
        if not answered:
            for step in (self._pending_question, self._correct, self._driver_step, self._batch, self._split_orders, self._split_reads):
                try:
                    out = step(thread_id, role, text, run, user, tr)
                except Exception:
                    log.exception("%s failed on %r", step.__name__, text[:80])
                    out = None
                if out is not None:
                    return out
        reply, meta, ok = self._turn(RULES, thread_id, role, run, user, tr, force=force)
        pa = reply.pending
        # 'X ko reminder bhej do' with nothing drafted yet: the draft card now, and -- once it is approved -- the send card
        # right after it, so sending takes no second request (two decisions still: what it says, and that it goes out)
        if pa is not None and pa.tool == "draft_reminder" and re.search(r"\b(bhej|bhejo|bhejdo|send|bhijwa\w*)\b", fold(run)) and \
                not re.search(r"\b(draft|bana|banao|tayyar|tayar|likh\w*)\b", fold(run)):
            reply.extra = dict(reply.extra or {}) | {"chain": {"after": pa.approval_id, "rest": [f"send reminder {pa.args.get('customer_id')}"], "total": 0,
                                                              "done": 0, "specialist": "wasooli", "role": role, "user": user, "on": "approve"}}
            reply.text += " Once it is approved, the card to send it comes next."
        return reply, meta, ok

    # -- the approvals queue ('koi approval pending he?')
    def _pending_question(self, thread_id, role, text, run, user, tr):
        if not TURNS.asks_pending(text) or customer_resolution(text, self.repo).status != "none":
            return None
        items = self.pending_items()
        mine = lambda p: bool(user.strip() and p.requested_by.strip() and same_person(user, p.requested_by)) or (not user.strip() and p.requested_by_role == role)  # noqa: E731
        can = [p for p in items if role in ("owner", "clerk") and self.decision_refusal(p, role, user) is None]
        own = [p for p in items if p not in can and mine(p)]
        rows = lambda ps: " ".join(f"{k}) {self._headline(self.card(p))} -- asked by {p.requested_by or p.requested_by_role}." for k, p in enumerate(ps[:10], 1))  # noqa: E731
        parts = []
        if can:
            parts.append(f"{len(can)} card(s) waiting for you to approve: {rows(can)}" + (f" ...and {len(can) - 10} more." if len(can) > 10 else "")
                         + " Open Approvals to decide them.")
        if own:
            parts.append(f"Your own request(s) waiting for someone else: {rows(own)}")
        txt = " ".join(parts) if parts else ("Nothing is waiting for your approval." if role in ("owner", "clerk") else "None of your requests is waiting for approval.")
        tr.specialist, tr.response_text = "report", txt
        return Reply(txt, "report", None, thread_id), {"specialist": "report", "approvals_listed": [p.approval_id for p in can + own]}, True

    # -- corrections ('galti ho gayi 40 kar do', 'nahi 25', '15000 nahi 12000 the')
    def _correct(self, thread_id, role, text, run, user, tr):
        me = lambda p: p.requested_by_role == role and (not p.requested_by.strip() or not user.strip() or same_person(p.requested_by, user))  # noqa: E731
        mine = [p for p in self.pending_items(thread_id) if me(p)]
        if mine:
            pa = mine[-1]
            new = TURNS.correction(text, pa.tool, pa.args, self.repo)
            if new is None:
                return None
            req = self._corrected_request(pa.tool, new)
            if req is None:
                return None
            old = self._headline(self.card(pa))
            # the requester withdraws their own request (never a decision on someone else's), then asks again, corrected
            self._withdraw(pa, role, user, f"withdrawn by the requester: corrected ('{text[:60]}')")
            reply, meta, ok = self._turn(RULES, thread_id, role, req, user, tr, force=pa.specialist)
            now = (", ".join(f"{int(i['qty'])} {self._entity_name('product', i['sku']) or i['sku']}"
                             + (f" at {_en({'rs': float(i['unit_cost'])})}" if i.get("unit_cost") else "") for i in new.get("items") or [])
                   if pa.tool in TURNS.ORDER_TOOLS + ("record_purchase",) else _en({"rs": float(new.get("amount") or 0)}))
            lead = f"Corrected to {now}: the earlier card ({old}) is withdrawn. "
            reply.text = lead + reply.text
            return reply, meta | {"corrects": pa.approval_id}, ok
        last = self._last_approved(thread_id, role, user)
        if last is None:
            return None
        new = TURNS.correction(text, last["tool"], last["args"], self.repo)
        if new is None:
            return None
        if last["tool"] in TURNS.ORDER_TOOLS:
            oid = last["args"].get("order_id") or self._order_made_by(last["approval_id"], thread_id)
            try:
                o = self.repo.get_order(oid or "")
            except Exception:
                return None
            before = {i["sku"]: int(i["qty"]) for i in last["args"].get("items") or []}
            changed = [i for i in new["items"] if before.get(i["sku"]) != int(i["qty"])]
            if o.status != "draft":
                txt = (f"That order ({order_label(self.repo, o)}) is already {o.status}: only a draft can be changed, so nothing was done. "
                       "Cancel it and book the right quantity as a new order.")
                return Reply(txt, "order", None, thread_id), {"specialist": "order"}, True
            reply, meta, ok = self._turn(RULES, thread_id, role, TURNS.update_text(o.order_id, changed), user, tr, force="order")
            return reply, meta | {"corrects": last["approval_id"]}, ok
        if last["tool"] == "record_payment":
            a = last["args"]
            cname = self._entity_name("customer", a.get("customer_id", "")) or a.get("customer_id", "")
            entry = next((e.entry_id for e in reversed(self.repo.ledger_for(a.get("customer_id", ""))) if e.kind == "payment" and abs(-e.amount - float(a.get("amount") or 0)) < .01), "")
            amt = lambda v: _en({"rs": float(v)})  # noqa: E731
            txt = (f"{amt(a.get('amount'))} from {cname} is already recorded" + (f" (receipt {entry})" if entry else "") + ". A recorded payment is never edited: "
                   f"the owner reverses it -- '{entry or 'RCP-...'} reverse karo, galat raqam' -- and then send the right one, e.g. "
                   f"'{cname} ne {new['amount']:,.0f} {a.get('method') or 'cash'} diye'. Nothing was changed yet.")
            return Reply(txt, "hisaab", None, thread_id), {"specialist": "hisaab"}, True
        return None

    @staticmethod
    def _corrected_request(tool: str, args: dict) -> str | None:
        if tool == "create_order":
            return TURNS.order_text(args["customer_id"], args["items"])
        if tool == "update_order":
            return TURNS.update_text(args["order_id"], args["items"])
        if tool == "record_payment":
            return TURNS.payment_text(args["customer_id"], float(args["amount"]), str(args.get("method") or "cash"))
        if tool == "record_purchase" and len(args.get("items") or []) == 1:
            return TURNS.purchase_text(args)
        return None

    def _withdraw(self, pa: PendingApproval, role: str, user: str, note: str) -> None:
        """The requester takes their own request back: recorded as a rejection with the reason, and the paused graph is
        told so (never left hanging). No decision is made on anyone else's behalf."""
        engine = engine_of(pa.approval_id)
        bundle, cfg = self._bundle(engine, pa.specialist), self._resume_cfg(pa, engine)
        self.repo.resolve_approval(pa.approval_id, False, user or role, note)
        self.repo.audit(role, "approval_rejected", "approval", pa.approval_id, {"tool": pa.tool, "args": pa.args, "specialist": pa.specialist, "note": note},
                        approved_by=role)
        try:
            bundle.agent.invoke(Command(resume={"decisions": [{"type": "reject", "message": note}]}), config=cfg)
        except Exception:
            log.exception("couldn't release the graph of withdrawn %s", pa.approval_id)
        self._settle_memory(pa, False, False, "withdrawn", user or role)

    def _last_approved(self, thread_id: str, role: str, user: str) -> dict | None:
        """This person's most recent approved card on this thread, if it was decided within the conversation window."""
        for a in self.repo.approval_history(40):
            if a["thread_id"] != thread_id or a["requested_by_role"] != role:
                continue
            if user.strip() and (a.get("requested_by") or "").strip() and not same_person(user, a["requested_by"]):
                continue
            return a if a["status"] == "approved" and FU.fresh(a.get("resolved_at")) else None
        return None

    def _order_made_by(self, approval_id: str, thread_id: str) -> str:
        """The order an approved create_order card made (its resolve reply names it)."""
        for r in reversed(self.repo.chat_history(thread_id, 200)):
            if (r.get("meta") or {}).get("resolved") == approval_id:
                m = re.search(r"ORD-[A-Z0-9]{8}", str(r.get("text") or ""))
                return m.group(0) if m else ""
        return ""

    # -- a driver at the door: 'customer ghar pe nahi tha'
    _NOT_THERE = re.compile(r"\b(ghar (pe|par|pr)? ?nahi|nahi (mila|mile|the|tha|thi)|dukan band|shop (was )?closed|band thi|band tha|not (at )?home|koi nahi tha|"
                            r"mana kar (diya|dia)|lene se mana|maal nahi liya|wapis le aya|wapas le aya|refused)\b")

    def _driver_step(self, thread_id, role, text, run, user, tr):
        if role != "driver" or not self._NOT_THERE.search(fold(text)) or otp_in(text):
            return None
        from munshi.agents.specialists import todays_stop
        c = customer_resolution(text, self.repo)
        st = todays_stop(self.repo, c.id or "") if c.ok else None
        if st is None:
            txt = "Which customer wasn't there? Say their name, e.g. 'Chaudhry Farms ghar pe nahi the'. The stop stays open until it is delivered."
            return Reply(txt, "delivery", None, thread_id), {"specialist": "delivery"}, True
        self.repo.notify("clerk", "delivery_failed", f"Driver{(' ' + user) if user else ''}: {c.name} -- delivery not made ('{text[:80]}'). The stop is still open.", st.stop_id)
        self.repo.notify("owner", "delivery_failed", f"Driver{(' ' + user) if user else ''}: {c.name} -- delivery not made ('{text[:80]}'). The stop is still open.", st.stop_id)
        txt = (f"I've told the office that {c.name} couldn't take the delivery. The stop stays open: nothing is delivered or collected, and no code is needed. "
               "Keep their goods on the gaari and go on to the next stop -- the office will tell you whether to try again today or bring it back.")
        tr.specialist, tr.response_text = "delivery", txt
        return Reply(txt, "delivery", None, thread_id), {"specialist": "delivery", "notified": st.stop_id}, True

    def _driver_route(self, text: str) -> str | None:
        """A driver's message about a customer on today's run (the amount to collect, the address, a delivery made) is the
        Delivery munshi's, whichever words it used -- the driver has no other munshi for it."""
        from munshi.agents.specialists import todays_stop
        f = fold(text)
        door = re.search(r"\b(address|pata|kahan|location|otp|code|agla|next|stop|paise lene|lene hein|lene hain|kitne lene|kitna lena|collect|raqam|bill kitna|"
                         r"de diya|de dia|diya|utar|utara|delivered|deliver|pohncha|maal de)\b", f)
        if not door:
            return None
        c = customer_resolution(text, self.repo)
        if c.ok and c.other is None and todays_stop(self.repo, c.id or "") is not None:
            return "delivery"
        if re.search(r"\b(address|otp|code|agla|next|stop)\b", f):
            return "delivery"
        return None

    # -- batches ('unko confirm kar do sab'): one card per record, chained through resolve()
    def _listed(self, thread_id: str, role: str) -> list[str]:
        for r in reversed(self.repo.chat_history(thread_id, self._MEMO_ROWS * 2)):
            memo = (r.get("meta") or {}).get("memo") if r.get("role") == "munshi" else None
            if memo and memo.get("role") == role and memo.get("listed") and FU.fresh(memo["listed"].get("at")):
                return list(memo["listed"]["ids"])
        return []

    def _batch(self, thread_id, role, text, run, user, tr):
        b = TURNS.batch_of(run)
        if b is None or role not in ("owner", "clerk"):
            return None
        verb, spec, statuses, ids = b
        if self.specialists.get(spec) is not self._built.get(spec):
            return None                                   # a swapped-in munshi (a scripted model) reads the whole message itself
        c = customer_resolution(text, self.repo)          # (as typed: a remembered customer the topic added doesn't narrow 'sab')
        if ids:
            cands = ids
        elif c.ok and c.other is None:
            cands = [o.order_id for o in orders_of(self.repo, c.id or "", statuses)]
        else:
            listed = self._listed(thread_id, role)
            pool = listed or [o.order_id for o in self.repo.list_orders(limit=200)]
            cands = []
            for oid in pool:
                try:
                    if self.repo.get_order(oid).status in statuses:
                        cands.append(oid)
                except Exception:
                    continue
        cands = list(dict.fromkeys(cands))[:20]
        if not cands:
            txt = f"There is no {' or '.join(statuses)} order to {verb}. Nothing was done."
            return Reply(txt, spec, None, thread_id), {"specialist": spec}, True
        steps = [TURNS.batch_step(verb, oid, text) for oid in cands]
        if len(steps) == 1:
            return self._turn(RULES, thread_id, role, steps[0], user, tr, force=spec)
        chain = {"rest": steps, "total": len(steps), "done": 0, "specialist": spec, "role": role, "user": user, "on": "any", "verb": verb}
        reply, meta, extra, notes = self._chain_step(thread_id, chain, tr)
        head = f"{len(steps)} orders to {verb} -- one card each, so each can be checked."
        reply.text = head + (" " + " ".join(notes) if notes else "") + (f" {reply.text}" if reply.pending else "")
        reply.extra = extra
        return reply, meta, True

    def _chain_step(self, thread_id: str, chain: dict, tr) -> tuple[Reply, dict, dict, list[str]]:
        """Raise the next card of a chain: (reply, meta, chat meta for the chain, notes on steps that raised no card)."""
        rest, k, n = list(chain["rest"]), int(chain["done"]), int(chain["total"])
        notes: list[str] = []
        reply, meta = Reply("", chain["specialist"], None, thread_id), {"specialist": chain["specialist"]}
        while rest:
            step = rest.pop(0)
            k += 1
            reply, meta, _ = self._turn(RULES, thread_id, chain["role"], step, chain["user"], tr, force=chain["specialist"])
            label = f"{k} of {n}: " if n else ""
            if reply.pending:
                reply.text = f"{label}{visible(reply.text)}" + (" After you decide it, the next one comes." if rest else "")
                nxt = dict(chain) | {"rest": rest, "done": k, "after": reply.pending.approval_id}
                extra = {"chain": nxt} | ({"lane": meta["lane"]} if meta.get("lane") else {})
                return reply, meta, extra, notes
            notes.append(f"{label}{visible(reply.text)}")
        reply = Reply("", chain["specialist"], None, thread_id)
        return reply, meta, {}, notes

    def _chain_of(self, approval_id: str, thread_id: str) -> dict | None:
        for r in reversed(self.repo.chat_history(thread_id, 5000)):
            ch = (r.get("meta") or {}).get("chain")
            if ch and ch.get("after") == approval_id:
                return ch
        return None

    # -- one message, several requests
    def _split_orders(self, thread_id, role, text, run, user, tr):
        """Orders for two or more customers in one message are never merged into one card. The munshi reads it back
        split, one order per customer, and asks once; 'haan' raises one card per customer (_split_cards)."""
        if role == "driver":
            return None
        segs = TURNS.split_customers(run, self.repo)
        if not segs:
            return None
        rows = []
        for k, seg in enumerate(segs, 1):
            op = analyse_order(seg, self.repo)
            items = ", ".join(f"{i['qty']} {self._entity_name('product', i['sku']) or i['sku']}" for i in op.items)
            rows.append(f"{k}) {op.customer.name}: {items}")
        txt = (f"That's {len(segs)} customers in one message, so I read it as {len(segs)} separate orders -- " + "; ".join(rows)
               + ". Shall I ask for approval of each, one card per customer? Say 'haan' (or send them one at a time).")
        reply = Reply(txt, "order", None, thread_id)
        reply.ask = {"slot": "split", "candidates": [{"id": s, "name": r} for s, r in zip(segs, rows, strict=True)], "specialist": "order"}
        tr.specialist, tr.response_text = "order", txt
        return reply, {"specialist": "order"}, True

    def _split_cards(self, thread_id: str, role: str, user: str, segs: list[str], tr) -> tuple[Reply, dict, bool]:
        """'haan' to a split order: one card per customer -- the first on the order desk's thread, the rest on lanes."""
        outs = [self._turn(RULES, thread_id, role, seg, user, tr, force="order") for seg in segs]
        cards = [{"approval_id": r.pending.approval_id, "lane": m.get("lane")} for r, m, _ in outs if r.pending]
        names = [(self._entity_name("customer", customer_resolution(s, self.repo).id or "")) for s in segs]
        head = f"{len(segs)} customers in one message ({', '.join(names)}), so each gets its own card:"
        body = " ".join(f"{k}) {visible(r.text)}" for k, (r, _, _) in enumerate(outs, 1))
        first = next((r for r, _, _ in outs if r.pending), outs[0][0])
        meta = next((m for r, m, _ in outs if r.pending), outs[0][1])
        first.text = f"{head} {body}"
        first.extra = {"cards": cards}
        return first, meta, True

    def _split_reads(self, thread_id, role, text, run, user, tr):
        parts = TURNS.split_reads(run)
        if not parts or not all(self._read_only_question(p) for p in parts):
            return None
        outs = [self._turn(RULES, thread_id, role, p, user, tr) for p in parts]
        if any(r.pending or r.waiting or not r.tool or self._is_write(r.tool) for r, _, _ in outs):
            return None
        first, meta, _ = outs[0]
        first.text = "\n".join(visible(r.text) for r, _, _ in outs)
        return first, meta, True

    # -- what a tool result means for the next message
    def _tool_followups(self, reply: Reply, new_msgs: list, text: str) -> None:
        """A list reply remembers what it listed; a wrong delivery code keeps the close open for the right one; a
        dispatch suggestion can be taken with 'theek he, bana do'; a cut-short list can be shown whole with 'sab dikhao'."""
        tms = [m for m in new_msgs if isinstance(m, ToolMessage)]
        tm = tms[-1] if tms else None
        if tm is None:
            return
        content = str(tm.content)
        if tm.name == "list_orders":
            reply.listed = list(dict.fromkeys(re.findall(r"ORD-[A-Z0-9]{8}", content)))
        if tm.name == "close_stop" and "OTP does not match" in content and reply.ask is None:
            reply.ask = {"slot": "otp", "candidates": [], "specialist": "delivery"}
            reply.text += " Ask the customer for the code again and send just the code, e.g. 'code 1234'."
        if tm.name == "suggest_dispatch" and reply.ask is None:
            try:
                rows = [r for r in json.loads(content) if isinstance(r, dict) and r.get("vehicle_id") and r.get("route_id") != "UNROUTED"]
            except (ValueError, TypeError):
                rows = []
            if rows:
                reply.ask = {"slot": "dispatch", "specialist": "godown",
                             "candidates": [{"id": f"dispatch plan {r['route_id']} {r['vehicle_id']} " + " ".join(r.get("order_ids") or []),
                                             "name": self._route_name(r["route_id"])} for r in rows[:5]]}
                if len(rows) == 1:
                    reply.text = visible(reply.text).rstrip(".") + ". Say 'theek he, bana do' and I'll ask for approval of this plan." + (
                        DETAILS + reply.text.split(DETAILS, 1)[1] if DETAILS in reply.text else "")
        if reply.ask is None and re.search(r"\.\.\.(and \d+ more|aur \d+ mazeed)|\.\.\.اور \d+ مزید", visible(reply.text)):
            reply.ask = {"slot": "more", "candidates": []}

    def _route_name(self, rid: str) -> str:
        try:
            return self.repo.get_route(rid).name
        except Exception:
            return rid

    def _turn(self, engine: str, thread_id: str, role: str, text: str, user: str, tr, config: dict | None = None,
              context: list | None = None, force: str | None = None) -> tuple[Reply, dict, bool]:
        """One engine's go at a message: (reply, chat-log meta, understood). `understood` is False only when the
        engine's manager routed nowhere or, in hybrid mode, the rules specialist ended on the explicit
        NOT_UNDERSTOOD outcome. `force` names the specialist when the message completes that specialist's own
        open question (the request goes back to whoever asked)."""
        self._said = text
        manager = self.manager if engine == RULES else self.llm_manager
        specialist = force if force in BUILDERS and (engine == RULES or force != "help") else classify(manager, text, role, config=config, context=context)
        if specialist is None and engine == RULES:
            try:
                specialist = self._bare_entity(text)
            except Exception:
                log.exception("bare-name routing failed on %r", text[:80])
        if engine == RULES and role == "driver" and specialist not in ("delivery", "help"):
            specialist = self._driver_route(text) or specialist
        tr.specialist = specialist
        if specialist is None:
            return Reply(CLARIFY, None, None, thread_id, engine=engine), {"specialist": None}, False

        # A paused graph is NEVER invoked with a new message. While this role has a card waiting with this specialist on
        # this thread: a plain read-only question goes to the Report munshi (its own thread namespace, no write tools for
        # any role); anything else runs on a fresh graph LANE of its own ('{thread}:{role}:{specialist}:q{approval id}',
        # recorded in the chat log so resolve() resumes the right one) -- an independent request (another customer's
        # order) gets its own card, a read gets its answer, and only what can't stand alone (a duplicate of the waiting
        # card, 'yes', a bare verb) is answered with the card it is waiting behind, so it can be decided right there.
        held = sorted((p for p in self.repo.pending_approvals(thread_id) if p["specialist"] == specialist and p["requested_by_role"] == role),
                      key=lambda p: p["created_at"])
        lane = None
        if held:
            if self._read_only_question(text) and self._report_answers(role, thread_id):
                specialist = tr.specialist = "report"
            elif engine == RULES:
                lane = self._new_lane(thread_id, role, specialist)
            else:
                pa = self._pa(held[0])
                txt = self._still_waiting(pa, role, user)
                tr.response_text = txt
                return (Reply(txt, specialist, None, thread_id, waiting=pa, engine=engine),
                        {"specialist": specialist, "waiting_on": pa.approval_id}, True)

        bundle = self._bundle(engine, specialist)
        cfg = {"configurable": {"thread_id": lane[1]}} if lane else self._cfg(thread_id, role, specialist, engine)
        self._clear_orphaned_interrupt(bundle, cfg)
        result = bundle.agent.invoke({"messages": [*(context or []), HumanMessage(text)], "role": role}, config=cfg | (config or {}))
        if lane:
            kept = self._lane_result(bundle, cfg, specialist, thread_id, role, user, text, result, held, tr)
            if kept is not None:
                return kept
        if engine == RULES and self.hybrid and not result.get("__interrupt__") and not_understood(result["messages"][-1]):
            return Reply(self._final_text(result), specialist, None, thread_id, engine=engine), {"specialist": specialist}, False
        msgs = result.get("messages") or []
        h = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=-1)
        called = [tc["name"] for m in msgs[h + 1:] if isinstance(m, AIMessage) for tc in (m.tool_calls or [])]
        ask = ask_of(msgs[-1]) if msgs and not result.get("__interrupt__") else None
        reply = self._settle(bundle, specialist, thread_id, role, user, result, tr, engine=engine, config=config, cfg=cfg if lane else None,
                             aid=lane[0] if lane else None)
        reply.engine, reply.tool = engine, (called[-1] if called else None)
        reply.ask = ask if reply.pending is None else None
        if reply.pending is None:
            self._tool_followups(reply, msgs[h + 1:], text)
        meta = {"specialist": specialist} | ({"approval_id": reply.pending.approval_id} if reply.pending else {})
        if lane and reply.pending:
            meta["lane"] = lane[1]
        return reply, meta, True

    # ------------------------------------------------------------------ graph lanes: more than one card per specialist per thread
    def _new_lane(self, thread_id: str, role: str, specialist: str) -> tuple[str, str]:
        """(the approval id a card raised on it will get, the lane's graph thread id). Rules-engine ids are hex."""
        aid = uuid.uuid4().hex[:10].upper()
        return aid, f"{thread_id}:{role}:{specialist}:q{aid}"

    _IGNORED_ARGS = ("source_text", "reason", "ref", "note", "channel", "approved_by")

    def _same_request(self, tool: str, args: dict, held: list[dict]) -> dict | None:
        """The waiting card this call merely repeats (same action, same record, same lines/amount), if any."""
        key = lambda a: json.dumps({k: v for k, v in (a or {}).items() if k not in self._IGNORED_ARGS}, sort_keys=True, default=str)  # noqa: E731
        return next((p for p in held if p["tool"] == tool and key(p["args"]) == key(args)), None)

    def _lane_result(self, bundle, cfg: dict, specialist: str, thread_id: str, role: str, user: str, text: str, result: dict,
                     held: list[dict], tr) -> tuple[Reply, dict, bool] | None:
        """What a message run on a fresh lane (while this role's card waits) gets: its own card, or a read's answer
        (None: the normal settle does that). Otherwise the waiting card, with the lane's question added when the message
        named someone -- and a card that only repeats the waiting one is declined, never raised twice."""
        interrupts = result.get("__interrupt__")
        msgs = result.get("messages") or []
        h = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=-1)
        pa = self._pa(held[0])
        if interrupts:
            reqs = interrupts[0].value.get("action_requests", [])
            dup = self._same_request(reqs[0]["name"], reqs[0]["args"], held) if len(reqs) == 1 else None
            if dup is None:
                return None
            pa = self._pa(dup)
            try:
                bundle.agent.invoke(Command(resume={"decisions": [{"type": "reject", "message": "Not asked for: the same request is already waiting."}] * len(reqs)}),
                                    config=cfg)
            except Exception:
                log.exception("couldn't decline a duplicate on %s", cfg["configurable"]["thread_id"])
            txt = self._still_waiting(pa, role, user)
            tr.response_text = txt
            return Reply(txt, specialist, None, thread_id, waiting=pa), {"specialist": specialist, "waiting_on": pa.approval_id}, True
        reads = [tc["name"] for m in msgs[h + 1:] if isinstance(m, AIMessage) for tc in (m.tool_calls or []) if not self._is_write(tc["name"])]
        if reads:
            return None
        txt = self._still_waiting(pa, role, user)
        ask = ask_of(msgs[-1]) if msgs else None
        named = customer_resolution(text, self.repo).status != "none" or supplier_resolution(text, self.repo).status != "none"
        reply = Reply(txt, specialist, None, thread_id, waiting=pa)
        if named and msgs and isinstance(msgs[-1], AIMessage) and not not_understood(msgs[-1]) and str(msgs[-1].text).strip():
            reply.text = f"{txt}\n\nYour new message: {msgs[-1].text}"
            reply.ask = ask
        tr.response_text = reply.text
        return reply, {"specialist": specialist, "waiting_on": pa.approval_id}, True

    def _lane_of(self, approval_id: str, thread_id: str) -> str | None:
        """The graph lane a card was raised on (None: the specialist's main thread). Read from the chat log."""
        try:
            rows = self.repo.chat_history(thread_id, 5000)
        except Exception:
            return None
        for r in reversed(rows):
            meta = r.get("meta") or {}
            if meta.get("approval_id") == approval_id and meta.get("lane"):
                return meta["lane"]
            for c in meta.get("cards") or []:
                if c.get("approval_id") == approval_id and c.get("lane"):
                    return c["lane"]
        return None

    def _resume_cfg(self, pa: PendingApproval, engine: str) -> dict:
        lane = self._lane_of(pa.approval_id, pa.thread_id) if engine == RULES else None
        return {"configurable": {"thread_id": lane}} if lane else self._cfg(pa.thread_id, pa.requested_by_role, pa.specialist, engine)

    # ------------------------------------------------------------------ the model engine
    _CONTEXT_LINES = 4

    def _history_lines(self, thread_id: str, n: int = 12) -> list[str]:
        """This conversation's earlier lines (oldest first), without the message being answered."""
        return [str(r["text"]) for r in self.repo.chat_history(thread_id, n + 1)[:-1]]

    def _context(self, thread_id: str, text: str) -> list:
        """One note ahead of the message: the reply script, decided in code from the message (a system-prompt rule
        alone was not enough: the model answered Roman Urdu in Urdu script), and the last few lines of the chat,
        whichever engine answered them (the model engine's own memory doesn't hold the turns the rules answered)."""
        script = ("The next message is written in Urdu script: reply in Urdu script." if is_urdu(text) else
                  "The next message is written in Latin letters (Roman Urdu or English): reply ONLY in Latin letters, in the same "
                  "language as the message -- Roman Urdu for Roman Urdu. Do not use Urdu script.")
        try:
            read = guard.hints(text, self.repo)
        except Exception:
            log.exception("hints failed")
            read = ""
        fc = self._from_chat
        if fc:
            read = (read + " " if read else "") + f"This message is about the {fc['kind']} of the conversation: {fc['name']} = {fc['id']}."
        rows = self.repo.chat_history(thread_id, self._CONTEXT_LINES + 1)[:-1]
        lines = "\n".join(f"{'munshi' if r['role'] == 'munshi' else 'user'}: {str(r['text'])[:240]}" for r in rows)
        earlier = f"\nEarlier in this chat, for context only -- act on the next message:\n{lines}" if rows else ""
        return [HumanMessage(f"(Note from the system, not from the user. {script}" + (f" {read}" if read else "") + f"{earlier})")]

    # A model reply that says something was done (recorded, created, received...) when no write ran and no card was
    # raised this turn. Seen on real traffic: "Green Valley ka payment record kar diya gaya" with no tool call at all.
    _CLAIM = re.compile(r"\b(record(ed)?|darj|saved?|created|posted|added|done|kar dia|kar diya|kar di|kar diye|kr diya|ho gaya|ho gayi|ho gaye|ho gya|"
                        r"ho chuka|ho chuki|bana diya|bana di|tayyar|tayar|jama ho|wusool ho|wasool ho|vasool ho|mil gaye|mil gaya|bhej diya|bhej di)\b"
                        r"|کر دیا|کر دی|کردیا|ہو گیا|ہو گئی|ہو گئے|ہوگیا|تیار|درج|وصول ہو|بھیج دیا|بنا دیا", re.I)

    @classmethod
    def _claims_done(cls, text: str) -> bool:
        """A sentence asserting completion. A question ('delivery ho gayi?') or a negation ('nothing was recorded',
        'darj nahi hua') is not a claim."""
        for s in re.split(r"(?<=[.!۔\n])\s+", str(text)):
            s = s.strip()
            if not s or s.rstrip("*_ )").endswith(("?", "؟")) or cls._NEG.search(s):
                continue
            if cls._CLAIM.search(s):
                return True
        return False

    _NEG = re.compile(r"\b(not|nothing|no|never|nahi|nahin|nai|na|mat|couldn't|can't|cannot|won't|haven't|hasn't|isn't|wasn't)\b|نہیں|نہ ", re.I)

    _URDU_CHARS = re.compile(r"[؀-ۿ]")

    @classmethod
    def _latin_only(cls, text: str) -> str:
        """For a message typed in Latin letters: drop the sentences of a model reply written wholly in Urdu script
        (typically a tacked-on 'کوئی اور مدد؟'), but only when a Latin-letter part remains -- a reply that is all Urdu
        script is left as it is rather than emptied."""
        parts = re.split(r"(?<=[.!?۔؟\n])\s+", str(text))
        keep = [s for s in parts if not (cls._URDU_CHARS.search(s) and not re.search(r"[A-Za-z]{2,}", s))]
        return " ".join(keep).strip() if keep and len(keep) < len(parts) else text

    def _guard_refused(self, reply: Reply, thread_id: str, role: str) -> bool:
        """The reply is the entity guard's own question (agents/guard.py), not model text."""
        try:
            msgs = self._bundle(MODEL, reply.specialist).agent.get_state(self._cfg(thread_id, role, reply.specialist, MODEL)).values.get("messages", [])
            return bool(msgs) and guard.guard_refusal(msgs[-1]) is not None
        except Exception:
            return False

    def _acted(self, bundle, cfg: dict) -> bool:
        """Did this model turn run a write (OTP-gated close_stop) -- i.e. a non-read tool result after the message?"""
        try:
            msgs = bundle.agent.get_state(cfg).values.get("messages", [])
        except Exception:
            return True
        h = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=-1)
        return any(isinstance(m, ToolMessage) and m.name and self._is_write(m.name) and m.status != "error" for m in msgs[h + 1:])

    @staticmethod
    def _is_write(tool: str) -> bool:
        try:
            return risk_of(tool) != RiskTier.READ_ONLY
        except ValueError:
            return True

    def _model_turn(self, thread_id: str, role: str, text: str, user: str, tr, fallback: Reply, fallback_meta: dict) -> tuple[Reply, dict]:
        """The real model's go at a message the rules didn't understand. Any failure -- timeout, rate limit,
        provider error, a malformed reply -- gives the rules' own reply instead; never a stack trace."""
        calls = _ModelCalls()
        if self._model_down_until is not None and datetime.now(UTC) < self._model_down_until:
            # the provider said "rate limited" a moment ago: answer from the rules at once instead of waiting a minute to hear it again
            reply, meta = fallback, dict(fallback_meta)
            reply.model_error = meta["model_error"] = "ModelUnavailable"
            return reply, meta | {"engine": reply.engine, "model_calls": 0, "model_tokens": 0, "model_down_until": self._model_down_until.isoformat()}
        token = guard.set_history(self._history_lines(thread_id))
        ttoken = guard.set_topic(self._topic)
        try:
            reply, meta, _ = self._turn(MODEL, thread_id, role, text, user, tr, config={"callbacks": [calls]}, context=self._context(thread_id, text))
            if reply.specialist is None:                   # the model routed nowhere either: the rules' answer stands
                reply, meta = fallback, dict(fallback_meta)
            elif (reply.pending is None and reply.waiting is None and not self._guard_refused(reply, thread_id, role) and self._claims_done(reply.text)
                  and not self._acted(self._bundle(MODEL, reply.specialist), self._cfg(thread_id, role, reply.specialist, MODEL))):
                # it says it did something it didn't: never let that stand -- nothing was recorded, and the rules' reply says what to send
                log.warning("model claimed an action it didn't take on %s: %r", thread_id, reply.text[:160])
                claim = reply.text
                reply = Reply(f"{NOTHING_DONE_UR if is_urdu(text) else NOTHING_DONE} {fallback.text}", reply.specialist, None, thread_id, engine=MODEL)
                meta = {"specialist": reply.specialist, "claim_blocked": claim[:200]}
            elif reply.pending is None and reply.waiting is None and self._raw(reply.text):
                # the model returned nothing, or the tool's raw data: say it in a sentence instead
                nice = self._last_tool_sentence(self._bundle(MODEL, reply.specialist), self._cfg(thread_id, role, reply.specialist, MODEL))
                if nice:
                    meta = meta | {"model_text_replaced": reply.text[:200]}
                    reply.text = nice
            elif reply.pending is None and not is_urdu(text):
                trimmed = self._latin_only(reply.text)
                if trimmed != reply.text:
                    meta = meta | {"script_trimmed": reply.text[:200]}
                    reply.text = trimmed
        except Exception as e:
            log.warning("model turn failed on %s (%s: %s); answering from the rules", thread_id, type(e).__name__, str(e)[:200])
            self._heal_model_threads(thread_id, role)
            reply, meta = fallback, dict(fallback_meta)
            reply.model_error = meta["model_error"] = type(e).__name__
            wait = self.rate_limit_wait(e)
            if wait:
                self._model_down_until = datetime.now(UTC) + timedelta(seconds=wait)
                log.warning("model rate-limited: answering from the rules until %s", self._model_down_until.isoformat())
        finally:
            guard.reset_history(token)
            guard.reset_topic(ttoken)
        reply.model_calls, reply.model_tokens = calls.n, calls.tokens
        return reply, meta | {"engine": reply.engine, "model_calls": calls.n, "model_tokens": calls.tokens}

    @staticmethod
    def rate_limit_wait(e: Exception) -> float:
        """Seconds to stop asking the model after this error: 0 unless it is a rate limit. A daily token limit (Groq's
        'tokens per day (TPD)') won't lift for hours -- asking again only adds a minute of retries to every message."""
        msg = f"{type(e).__name__} {e}".lower()
        if "ratelimit" not in msg.replace("_", "").replace(" ", "") and "429" not in msg:
            return 0.0
        m = re.search(r"try again in\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:([\d.]+)s)?", msg)
        said = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)) if m and any(m.groups()) else 0.0
        daily = "per day" in msg or "tpd" in msg or "rpd" in msg
        return max(said, 3600.0 if daily else 60.0) if not said else min(max(said, 30.0), 6 * 3600.0)

    @staticmethod
    def _raw(text: str) -> bool:
        """A reply that is empty, a bare 'Done.', or raw data rather than prose."""
        t = str(text or "").strip()
        return not t or t in ("Done.", "Done") or t[:1] in ("{", "[") or t.startswith("Done -- ")

    def _last_tool_sentence(self, bundle, cfg: dict) -> str | None:
        """The readable sentence for the last tool result of this turn on a model-engine thread, if any."""
        try:
            msgs = bundle.agent.get_state(cfg).values.get("messages", [])
        except Exception:
            return None
        h = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=-1)
        i = max((i for i, m in enumerate(msgs) if i > h and isinstance(m, ToolMessage) and m.status != "error"), default=-1)
        return self._readable(msgs, i) if i >= 0 else None

    def _heal_model_threads(self, thread_id: str, role: str) -> None:
        """After a failed model turn, leave no model-engine thread of this conversation holding a tool call with
        no answer (the provider would refuse every later request on it). A call paused behind a card is untouched."""
        for specialist, bundle in (self.llm_specialists or {}).items():
            cfg = self._cfg(thread_id, role, specialist, MODEL)
            try:
                self._heal(bundle, cfg)
            except Exception:
                log.exception("couldn't heal %s", cfg["configurable"]["thread_id"])

    @staticmethod
    def _heal(bundle, cfg: dict) -> None:
        state = bundle.agent.get_state(cfg)
        if not state.values or state.interrupts:
            return
        msgs = state.values.get("messages", [])
        ai_i = max((i for i, m in enumerate(msgs) if isinstance(m, AIMessage) and m.tool_calls), default=-1)
        if ai_i < 0:
            return
        answered = {m.tool_call_id for m in msgs[ai_i + 1:] if isinstance(m, ToolMessage)}
        missing = [tc for tc in msgs[ai_i].tool_calls if tc["id"] not in answered]
        if missing:
            bundle.agent.update_state(cfg, {"messages": [ToolMessage(content="Not run: the turn failed before this call finished.", tool_call_id=tc["id"],
                                                                     name=tc["name"], status="error") for tc in missing]}, as_node="tools")

    # ------------------------------------------------------------------ pausing on a gated call
    _MAX_DECLINES = 3

    def _settle(self, bundle, specialist: str, thread_id: str, role: str, user: str, result: dict, tr, lead: str = "",
                engine: str = RULES, config: dict | None = None, cfg: dict | None = None, aid: str | None = None) -> Reply:
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
            problem = "only one action needing approval can be asked for at a time" if len(reqs) != 1 else (
                self._unresolvable(reqs[0]["name"], reqs[0]["args"]) or self._cannot(reqs[0]["name"], reqs[0]["args"]))
            if problem is None:
                return self._open_card(bundle, specialist, thread_id, role, user, reqs[0], value.get(DEFERRED_KEY, []), problems, tr, lead, engine,
                                       aid=aid)
            log.warning("declined gated call on %s/%s: %s", thread_id, specialist, problem)
            problems.append(problem)
            if len(problems) > self._MAX_DECLINES:
                break
            result = bundle.agent.invoke(Command(resume={"decisions": [{"type": "reject", "message": f"Not asked for: {problem}."}] * max(len(reqs), 1)}),
                                         config=(cfg or self._cfg(thread_id, role, specialist, engine)) | (config or {}))
        if problems and lead:
            txt = f"{lead}A follow-up action couldn't be asked for — {problems[0]}. That follow-up was not done."
        elif problems:
            txt = f"Couldn't ask for approval — {problems[0]}. Nothing was done."
        else:
            txt = self._final_text(result)
        tr.response_text = txt
        return Reply(txt, specialist, None, thread_id)

    def _open_card(self, bundle, specialist: str, thread_id: str, role: str, user: str, req: dict, deferred: list[dict], problems: list[str], tr,
                   lead: str = "", engine: str = RULES, aid: str | None = None) -> Reply:
        tool = req["name"]
        # the approval id records the engine whose graph is paused on this call (see engine_of / resolve)
        aid = aid or ((MODEL_APPROVAL_PREFIX + uuid.uuid4().hex[:9].upper()) if engine == MODEL else uuid.uuid4().hex[:10].upper())
        pa = PendingApproval(aid, thread_id, specialist, tool, req["args"],
                             risk_of(tool).value, self._needs_role(tool, req["args"]), role, user)
        self.repo.save_approval(asdict(pa))
        mem_notes: list[str] = []
        if not lead:                                    # (a follow-up card after an approval has no wording of its own)
            try:
                links = self._memory_links(req["args"], self._said, role, user)
                if links:
                    self.repo.link_card(aid, links)
                mem_notes = [f["value"] for f in self.cards._memory_facts(aid)]
            except Exception:
                log.exception("couldn't link card %s to memory", aid)
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
        # a card whose customer/supplier came from the conversation, not from this message, says so plainly
        fc = self._from_chat
        note = f" -- {RP.t('from_chat', False, name=fc['name'])}" if fc and not lead and fc.get("id") in (
            str(req["args"].get("customer_id") or ""), str(req["args"].get("supplier_id") or "")) else ""
        # ... and one whose customer / supplier / product came from a learned name names that memory
        note += "".join(f" -- {n}" for n in mem_notes)
        # money cards say HOW the money moved, so a wrong method is caught at approval (the card's facts carry it too)
        if tool in ("record_payment", "pay_supplier") and isinstance(req["args"], dict):
            m = str(req["args"].get("method") or "cash")
            ref = str(req["args"].get("ref") or "")
            note += f" -- by {CARD_WORDS_EN.get(m, m)}" + (f" ({ref})" if re.match(r"(cheque|check|chq|chek|tid|trx|txn|ref) ", ref) else "")
        txt = f"{lead}{skipped}{bundle.title} {'next ' if lead else ''}wants to: {self._headline(card)}{note}.{effect} Needs {pa.needs_role} approval.{later}"
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

    def _still_waiting(self, pa: PendingApproval, role: str = "", user: str = "") -> str:
        """The card a message is held behind, and what THIS person can do about it (a salesman is never told to approve)."""
        card = self.card(pa)
        who = "the owner needs to approve" if pa.needs_role == "owner" else "another clerk or the owner needs to approve"
        mine = pa.requested_by_role == role and (not pa.requested_by.strip() or not user.strip() or same_person(pa.requested_by, user))
        if role in ("owner", "clerk") and self.decision_refusal(pa, role, user) is None:
            then = "Approve or reject it here, then ask again."
        elif mine and role in ("owner", "clerk"):
            then = "You can withdraw it here, or correct it (e.g. 'galti, 40 kar do'); anything new gets its own card."
        elif mine:
            then = "The office decides it; send a correction if something on it is wrong (e.g. 'galti, 40 kar do')."
        else:
            then = "The office decides it."
        return f"Still waiting for approval: {self._headline(card)} — {who}. {then}"

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
             "entry_id": "entry to reverse", "expense_id": "expense to reverse", "purchase_id": "purchase to reverse",
             "route_id": "route", "vehicle_id": "vehicle"}

    def _cannot(self, tool: str, args: dict) -> str | None:
        """Why this call would certainly fail if approved, read from the books now (read-only), or None. A card that
        can't succeed wastes an approver's tap and teaches them to tap without reading -- so none is raised: the
        munshi says why instead. (Orders, allocation, dispatch and loading: where state and stock decide.)"""
        g = lambda fn, *a: self.cards._get(fn, *a)  # noqa: E731
        pname = lambda sku: self._entity_name("product", sku) or sku  # noqa: E731
        wname = lambda wid: (g(self.repo.get_warehouse, wid).name if g(self.repo.get_warehouse, wid) else wid)  # noqa: E731
        if tool == "update_order":
            o = g(self.repo.get_order, str(args.get("order_id") or ""))
            if o is None:
                return f"there is no order {args.get('order_id')}"
            if o.status != "draft":
                return f"that order is already {o.status} -- only a draft can be changed"
            if not self.ops.merged_lines(o, [i for i in args.get("items") or [] if isinstance(i, dict)]):
                return "that would leave the order with no lines -- cancel it instead"
        if tool == "allocate_order":
            o = g(self.repo.get_order, str(args.get("order_id") or ""))
            if o is None:
                return f"there is no order {args.get('order_id')}"
            if o.status != "confirmed":
                return f"that order is {o.status} -- only a confirmed order can have stock reserved" + (" (confirm it first)" if o.status == "draft" else "")
            wid = str(args.get("warehouse_id") or "") or (g(self.repo.default_warehouse_id) or "")
            short = [f"{pname(sku)} {self.cards._available(sku, wid)} of {q}" for sku, q in self.cards._need(o.items).items() if self.cards._available(sku, wid) < q]
            if short:
                return f"{wname(wid)} doesn't have enough: " + ", ".join(short)
        if tool == "create_dispatch_plan":
            route, veh = g(self.repo.get_route, str(args.get("route_id") or "")), g(self.repo.get_vehicle, str(args.get("vehicle_id") or ""))
            if route is None or veh is None:
                return "the route or the vehicle doesn't exist"
            units = 0
            for oid in dict.fromkeys(str(x) for x in args.get("order_ids") or []):
                o = g(self.repo.get_order, oid)
                if o is None:
                    return f"there is no order {oid}"
                cname = self._entity_name("customer", o.customer_id) or o.customer_id
                if o.status != "allocated":
                    return f"{cname}'s order is {o.status} -- it must be allocated (stock reserved) before it can go on a plan"
                if o.warehouse_id != route.warehouse_id:
                    return f"{cname}'s order is reserved at {wname(o.warehouse_id)}, but the {route.name} gaari loads at {wname(route.warehouse_id)}"
                if self.repo._one("SELECT 1 FROM stops WHERE order_id=? AND status='pending'", (oid,)):
                    return f"{cname}'s order is already on a plan"
                units += o.load_units
            if units > veh.capacity_units:
                return f"{units} units won't fit on {veh.plate} (holds {veh.capacity_units})"
        if tool == "approve_dispatch_plan":
            plan = g(self.repo.get_plan, str(args.get("plan_id") or ""))
            if plan is None:
                return f"there is no plan {args.get('plan_id')}"
            if plan.status != "planned":
                return f"that plan is already {plan.status}"
            need: dict[str, int] = {}
            for oid in plan.order_ids:
                o = g(self.repo.get_order, oid)
                for sku, q in (self.cards._need(o.items) if o else {}).items():
                    need[sku] = need.get(sku, 0) + q
            short = [f"{pname(sku)} {self.repo.get_stock(plan.warehouse_id, sku).on_hand} of {q}" for sku, q in need.items()
                     if self.repo.get_stock(plan.warehouse_id, sku).on_hand < q]
            if short:
                return f"{wname(plan.warehouse_id)} doesn't have the stock to load: " + ", ".join(short)
        return None

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
            engine = engine_of(pa.approval_id)              # resumed by the SAME engine that paused it
            bundle = self._bundle(engine, pa.specialist)
            cfg = self._resume_cfg(pa, engine)               # its own graph lane, when it was raised on one
            # memory learns only from an approved card that raised no warnings (read before the action changes the books)
            warned = False
            if approve:
                try:
                    warned = bool(self.card(pa).get("warnings"))
                except Exception:
                    warned = True
            calls = _ModelCalls()
            decision = {"type": "approve"} if approve else {"type": "reject", "message": note or f"rejected by {role}"}
            token = guard.set_history([str(r["text"]) for r in self.repo.chat_history(pa.thread_id, 12)]) if engine == MODEL else None
            try:
                with self._trace(f"{pa.specialist}.resume", role, f"{'approve' if approve else 'reject'} {pa.tool}") as tr:
                    tr.approval_decision = "approve" if approve else "reject"
                    # the approval is on record BEFORE the tool runs: the audit trail never shows a write ahead of its approval
                    self.repo.resolve_approval(approval_id, approve, user or role, note)
                    self.repo.audit(role, "approval_" + ("granted" if approve else "rejected"), "approval", approval_id,
                                    {"tool": pa.tool, "args": pa.args, "specialist": pa.specialist, "note": note}, approved_by=role)
                    signature = (f"{role}:{user}" if user.strip() else role) if approve else ""
                    meta = {"specialist": pa.specialist, "resolved": approval_id, "approved": approve}
                    run_cfg = cfg | ({"callbacks": [calls]} if engine == MODEL and self.hybrid else {})
                    # The resumed turn may go on to ask for another gated action ("confirm A, then B"): that gets its
                    # own card, on the requester's behalf, instead of a bare "Done." over a paused graph. Anything the
                    # agent does while settling is the requester's too, but no longer covered by this approval.
                    settle = lambda res: self._settle(bundle, pa.specialist, pa.thread_id, pa.requested_by_role, pa.requested_by, res, tr,  # noqa: E731
                                                      lead="Approved. " if approve else "Rejected. ", engine=engine, config=run_cfg)
                    reply = None
                    try:
                        with self.repo.acting_as(pa.requested_by, approved_by=signature):
                            result = bundle.agent.invoke(Command(resume={"decisions": [decision]}), config=run_cfg)
                        if engine == MODEL:              # a model's follow-up can fail too: covered by the same fallback
                            with self.repo.acting_as(pa.requested_by):
                                reply = settle(result)
                    except Exception as e:
                        reply = self._resume_failed(pa, approve, bundle, cfg, engine, e)
                        if reply is None:   # the graph state is gone (e.g. checkpoints wiped); the approval stays resolved but unexecuted
                            self._settle_memory(pa, approve, False, "action failed", user or role)
                            txt = f"Couldn't resume that action ({type(e).__name__}); please ask the munshi again."
                            self.repo.add_chat(pa.thread_id, "munshi", txt, meta | {"error": True})
                            return Reply(txt, pa.specialist, None, pa.thread_id, engine=engine, model_calls=calls.n)
                    if reply is None:
                        with self.repo.acting_as(pa.requested_by):
                            reply = settle(result)
                    reply.engine, reply.model_calls, reply.model_tokens = engine, calls.n, calls.tokens
                    ran = approve and self._ran(bundle, cfg, pa.tool)
                    self._settle_memory(pa, approve, ran and not warned, "warnings" if warned else "action failed", user or role)
                    if reply.pending:
                        meta["approval_id"] = reply.pending.approval_id
                        main = self._cfg(pa.thread_id, pa.requested_by_role, pa.specialist, engine)["configurable"]["thread_id"]
                        if cfg["configurable"]["thread_id"] != main:
                            meta["lane"] = cfg["configurable"]["thread_id"]
                    else:
                        # the next card of a chain this one belongs to ('sab confirm kar do': 2 of 6), on the requester's behalf
                        try:
                            ch = self._chain_of(approval_id, pa.thread_id)
                            if ch and ch.get("rest") and (approve or ch.get("on") != "approve"):
                                with self.repo.acting_as(pa.requested_by):
                                    nxt, nmeta, extra, notes = self._chain_step(pa.thread_id, ch, tr)
                                more = " ".join(notes)
                                head, sep, tail = reply.text.partition(DETAILS)      # the next card is said BEFORE the folded details
                                if nxt.pending:
                                    reply.text = f"{head}\n\n" + (f"{more} " if more else "") + ("Next, " if ch.get("total") else "Next: ") + nxt.text + sep + tail
                                    reply.pending = nxt.pending
                                    meta |= {"approval_id": nxt.pending.approval_id} | extra
                                elif more:
                                    reply.text = f"{head}\n\n{more}{sep}{tail}"
                        except Exception:
                            log.exception("couldn't raise the next card of the chain after %s", approval_id)
                    self.repo.add_chat(pa.thread_id, "munshi", reply.text, meta)
                    if approve: self.deliver_messages()
                    return reply
            finally:
                if token is not None:
                    guard.reset_history(token)

    def _resume_failed(self, pa: PendingApproval, approve: bool, bundle, cfg: dict, engine: str, e: Exception) -> Reply | None:
        """A model-engine resume that failed AFTER the decision was applied -- typically the provider timing out on
        the follow-up message once the approved tool has already run -- must not read as "couldn't resume": say what
        actually happened, from the graph's own record, and leave the thread healthy. None = nothing was applied."""
        if engine != MODEL:
            log.exception("resume failed for %s", pa.approval_id)
            return None
        try:
            state = bundle.agent.get_state(cfg)
            msgs = state.values.get("messages", []) if state.values else []
        except Exception:
            msgs = []
        call = next((tc for m in reversed(msgs) if isinstance(m, AIMessage) for tc in m.tool_calls if tc["name"] == pa.tool), None)
        out = next((m for m in msgs if isinstance(m, ToolMessage) and call and m.tool_call_id == call["id"]), None)
        if out is None:
            log.exception("resume failed for %s", pa.approval_id)
            return None
        log.warning("model follow-up failed after %s ran (%s); answering from the tool result", pa.approval_id, type(e).__name__)
        try:
            self._heal(bundle, cfg)
        except Exception:
            log.exception("couldn't heal after %s", pa.approval_id)
        if not approve:
            return Reply("Rejected. Nothing was done.", pa.specialist, None, pa.thread_id, model_error=type(e).__name__)
        from munshi.llm.stub_model import StubToolCallingModel
        summary = StubToolCallingModel._summary(str(out.content)).content      # the offline one-line summary ("Done -- ...", redacted)
        nice = self._readable(msgs, msgs.index(out))
        if nice and summary.startswith("Done -- "):
            summary = nice + DETAILS + summary[len("Done -- "):]
        return Reply(f"Approved. {summary}"[:600] if not nice else f"Approved. {summary}", pa.specialist, None, pa.thread_id, model_error=type(e).__name__)

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
