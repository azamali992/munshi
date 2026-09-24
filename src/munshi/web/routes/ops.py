"""Operations: chat with the munshis, approvals, notifications, orders,
dispatch plans and the driver's stops. Form-based writes (a clerk tapping
'Confirm' instead of asking the agent) go straight to the repository with
the human recorded as both actor and approver.

Four-eyes on direct taps: a draft order or a planned dispatch is a request
made by a named person. Confirming, allocating or loading it is the approval,
and it goes through the same policy the chat approval cards use
(safety.risk.approval_refusal). Whoever drafted or edited the request can't
clear it; the owner may clear anything, including their own."""
from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from munshi.domain.models import Order, today_iso
from munshi.safety.risk import approval_refusal, approver_for, role_may_approve, stricter_role
from munshi.web.deps import Ctx, context

router = APIRouter(prefix="/api", tags=["operations"])


class ChatIn(BaseModel):
    thread_id: str = Field(default="main", max_length=40, pattern="^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=1000)


class Decision(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=200)


class OrderLine(BaseModel):
    sku: str
    qty: int = Field(ge=1, le=100000)
    unit_price: float | None = Field(default=None, ge=0)     # negotiated price; clerk/owner only


class OrderPatch(BaseModel):
    items: list[OrderLine] | None = Field(default=None, min_length=1)
    notes: str | None = Field(default=None, max_length=200)


class OrderIn(BaseModel):
    customer_id: str
    items: list[OrderLine] = Field(min_length=1)
    notes: str = Field(default="", max_length=200)


class AllocateIn(BaseModel):
    warehouse_id: str = ""


class CancelIn(BaseModel):
    reason: str = Field(default="", max_length=120)


class PlanIn(BaseModel):
    route_id: str
    vehicle_id: str
    order_ids: list[str] = Field(min_length=1)
    plan_date: str = ""


class StopLineIn(BaseModel):
    """One product on a stop. Repeats of a sku are summed by the repository and capped by what was loaded."""
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(min_length=1, max_length=40)
    qty: int = Field(ge=0, le=100000, strict=True)


class CloseIn(BaseModel):
    delivered_items: list[StopLineIn] = Field(max_length=200)
    returned_items: list[StopLineIn] = Field(default_factory=list, max_length=200)
    cash_collected: float = Field(default=0, ge=0, allow_inf_nan=False)
    otp: str = Field(min_length=4, max_length=4, pattern="^\\d{4}$")
    client_ref: str = Field(default="", max_length=40)     # idempotency key from the offline queue
    note: str = Field(default="", max_length=200)


class DepositIn(BaseModel):
    amount_counted: float = Field(ge=0)


def _pending_out(c: Ctx, p: dict) -> dict:
    return p | {"can_approve": role_may_approve(c.role, p["tool"]) and (c.role == "owner" or p["needs_role"] == "clerk")}


# ---------------------------------------------------------------- four-eyes for direct taps
def _authors(c: Ctx, entity_id: str, actions: set[str], *also: str) -> list[str]:
    """Everyone who put their name to this request: whoever created it, and anyone who edited it since.
    Taken from the audit trail, which records the signed-in user on every write."""
    names = list(also) + [a.get("user") or "" for a in c.repo.audit_log(1000, entity_id) if a["action"] in actions]
    return list(dict.fromkeys(n for n in names if n.strip()))


def _order_authors(c: Ctx, o: Order) -> list[str]:
    # editing a draft rewrites what is being asked for, so an editor is a requester too
    return _authors(c, o.order_id, {"create_order", "order_edited"}, o.created_by)


def _four_eyes(c: Ctx, tool: str, needs_role: str, requesters: list[str]) -> None:
    """The chat approval cards' policy (safety.risk.approval_refusal), applied to a form tap:
    403 unless the caller's role may clear `tool` at `needs_role`, and -- unless they're the
    owner -- they are none of the people who asked for it."""
    for who in requesters or [""]:
        why = approval_refusal(c.role, tool, needs_role, approver=c.who, requester=who)
        if why:
            raise HTTPException(403, why)


def _held_when_drafted(c: Ctx, order_id: str) -> bool:
    """Was this order put on credit hold (routed to the owner) when it was drafted? The hold
    notification is that record; nothing deletes notifications, only marks them read."""
    return c.repo._one("SELECT 1 FROM notifications WHERE kind='credit_hold' AND ref=? LIMIT 1", (order_id,)) is not None


def _confirm_needs(c: Ctx, o: Order) -> tuple[str, str]:
    """(who must confirm this order, why the owner if it's the owner). Like the chat path's
    re-check at decision time, this only ever gets stricter: an order that went on credit hold
    when it was drafted stays the owner's to confirm even if its customer's limit is raised later."""
    need, why = approver_for("confirm_order"), []
    limit = float(c.repo.setting("big_order_limit") or 0)
    if limit and o.total > limit:
        need = stricter_role(need, "owner"); why.append(f"orders above Rs {limit:,.0f} need the owner")
    hold = c.repo.over_credit(o)
    if hold:
        need = stricter_role(need, "owner"); why.append(hold + " — the owner must confirm")
    elif _held_when_drafted(c, o.order_id):
        need = stricter_role(need, "owner")
        why.append(f"order {o.order_id} went on credit hold when it was drafted — the owner must confirm it, even if the credit limit has since been raised")
    return need, "; ".join(why)


# ---------------------------------------------------------------- chat + approvals
@router.post("/chat")
def chat(body: ChatIn, c: Ctx = Depends(context("chat"))):
    r = c.platform.handle_message(body.thread_id, c.role, body.text.strip(), user=c.who)
    pend = None
    if r.pending:
        pend = _pending_out(c, asdict(r.pending) | {"summary": r.pending.describe()})
    return {"text": r.text, "specialist": r.specialist, "pending": pend}


@router.get("/chat/{thread_id}")
def history(thread_id: str, c: Ctx = Depends(context("chat"))):
    return c.repo.chat_history(thread_id)


@router.get("/approvals")
def approvals(c: Ctx = Depends(context("approvals:read"))):
    return [_pending_out(c, p) for p in c.platform.list_pending()]


@router.get("/approvals/history")
def approvals_history(c: Ctx = Depends(context("approvals:read"))):
    return c.repo.approval_history(100)


@router.post("/approvals/{approval_id}")
def decide(approval_id: str, body: Decision, c: Ctx = Depends(context("approvals:decide"))):
    r = c.platform.resolve(approval_id, body.approve, c.role, body.note, user=c.who)
    return {"text": r.text, "specialist": r.specialist}


@router.get("/notifications")
def notifications(unread: bool = False, c: Ctx = Depends(context("notifications"))):
    return c.repo.notifications(c.role, unread)


@router.post("/notifications/read")
def read_notifications(c: Ctx = Depends(context("notifications"))):
    c.repo.mark_notifications_read(c.role); return {"ok": True}


@router.get("/badge")
def badge(c: Ctx = Depends(context("notifications"))):
    """One cheap call the app polls: pending approvals I can act on, unread notifications, my stops."""
    pend = c.platform.list_pending() if c.principal.can("approvals:read") else []
    out = {"approvals": len([p for p in pend if role_may_approve(c.role, p["tool"])]), "unread": len(c.repo.notifications(c.role, unread_only=True))}
    if c.role == "driver":
        out["stops_pending"] = sum(1 for p in c.repo.list_plans(status="approved") for s in c.repo.list_stops(p.plan_id) if s.status == "pending")
    return out


# ---------------------------------------------------------------- orders
@router.get("/orders")
def orders(status: str = "", customer_id: str = "", c: Ctx = Depends(context("orders:read"))):
    return [c.platform.ops._order(o) for o in c.repo.list_orders(status or None, customer_id or None, limit=200)]


@router.get("/orders/{order_id}")
def order(order_id: str, c: Ctx = Depends(context("orders:read"))):
    d = c.platform.ops.get_order(order_id)
    d["audit"] = c.repo.audit_log(20, order_id) if c.role != "driver" else []
    return d


@router.post("/orders", status_code=201)
def create_order(body: OrderIn, c: Ctx = Depends(context("orders:create"))):
    """A form-entered order. Salesmen's orders stay drafts for the office; a clerk's or owner's is a draft too — confirmation is a separate tap."""
    from munshi.domain.repository import CreditHoldError
    lines = [l.model_dump() for l in body.items]
    if c.role == "salesman":
        for l in lines: l["unit_price"] = None          # salesmen book at list price
    try:
        o = c.repo.create_order(body.customer_id, lines, "app", "", c.role, body.notes)
        hold = None
    except CreditHoldError as e:
        hold = str(e); o = c.repo.list_orders(customer_id=body.customer_id, limit=1)[0]
    return c.platform.ops._order(o) | {"credit_hold": hold}


@router.patch("/orders/{order_id}")
def edit_order(order_id: str, body: OrderPatch, c: Ctx = Depends(context("orders:create"))):
    lines = [l.model_dump() for l in body.items] if body.items is not None else None
    if lines and c.role == "salesman":
        for l in lines: l["unit_price"] = None
    return c.platform.ops._order(c.repo.update_order(order_id, lines, body.notes, c.role, c.signature))


@router.post("/orders/{order_id}/confirm")
def confirm(order_id: str, c: Ctx = Depends(context("dispatch:write"))):
    o = c.repo.get_order(order_id)
    need, why_owner = _confirm_needs(c, o)
    if need == "owner" and c.role != "owner":
        raise HTTPException(403, why_owner)
    _four_eyes(c, "confirm_order", need, _order_authors(c, o))
    return c.platform.ops._order(c.repo.confirm_order(order_id, c.role, c.signature, override_credit=c.role == "owner"))


@router.post("/orders/{order_id}/cancel")
def cancel(order_id: str, body: CancelIn, c: Ctx = Depends(context("dispatch:write"))):
    return c.platform.ops._order(c.repo.cancel_order(order_id, body.reason or "cancelled in app", c.role, c.signature))


@router.post("/orders/{order_id}/allocate")
def allocate(order_id: str, body: AllocateIn, c: Ctx = Depends(context("dispatch:write"))):
    _four_eyes(c, "allocate_order", approver_for("allocate_order"), _order_authors(c, c.repo.get_order(order_id)))
    return c.repo.allocate_order(order_id, body.warehouse_id or c.repo.default_warehouse_id(), c.role, c.signature)


# ---------------------------------------------------------------- dispatch
def _plan_out(c: Ctx, p) -> dict:
    d = asdict(p)
    d["stops"] = [asdict(s) for s in c.repo.list_stops(p.plan_id)]
    d["route_name"] = c.repo.get_route(p.route_id).name if _safe(c.repo.get_route, p.route_id) else p.route_id
    d["plate"] = c.repo.get_vehicle(p.vehicle_id).plate if _safe(c.repo.get_vehicle, p.vehicle_id) else p.vehicle_id
    d["cash_collected"] = round(sum(s["cash_collected"] for s in d["stops"]), 2)
    d["deposited"] = round(sum(x.amount_counted for x in c.repo.deposits(p.plan_id)), 2)
    for s in d["stops"]:
        cust = c.repo.get_customer(s["customer_id"])
        s["customer_name"], s["address"], s["phone"] = cust.name, cust.address, cust.phone
        o = c.repo.get_order(s["order_id"]); s["items"] = [i.__dict__ for i in o.items]; s["order_total"] = o.total
        if c.role == "driver":
            s["otp"] = None           # the driver never sees the OTP; the customer holds it
    return d


def _safe(fn, *a) -> bool:
    try:
        fn(*a); return True
    except Exception:
        return False


@router.get("/plans")
def plans(date: str = "", status: str = "", c: Ctx = Depends(context("dispatch:read"))):
    return [_plan_out(c, p) for p in c.repo.list_plans(date or None, status or None)]


@router.get("/plans/{plan_id}")
def plan(plan_id: str, c: Ctx = Depends(context("dispatch:read"))):
    return _plan_out(c, c.repo.get_plan(plan_id))


@router.get("/dispatch/suggest")
def suggest(date: str = "", c: Ctx = Depends(context("dispatch:write"))):
    return c.platform.ops.suggest_dispatch(date)


@router.post("/plans", status_code=201)
def create_plan(body: PlanIn, c: Ctx = Depends(context("dispatch:write"))):
    p = c.repo.create_dispatch_plan(body.plan_date or today_iso(), body.route_id, body.vehicle_id, body.order_ids, c.role)
    return _plan_out(c, p)


@router.post("/plans/{plan_id}/approve")
def approve_plan(plan_id: str, c: Ctx = Depends(context("dispatch:write"))):
    c.repo.get_plan(plan_id)       # 404 before anything else
    _four_eyes(c, "approve_dispatch_plan", approver_for("approve_dispatch_plan"), _authors(c, plan_id, {"create_dispatch_plan"}))
    p = c.repo.approve_dispatch_plan(plan_id, c.role, c.signature)
    c.platform.deliver_messages()
    return _plan_out(c, p)


@router.post("/plans/{plan_id}/cancel")
def cancel_plan(plan_id: str, c: Ctx = Depends(context("dispatch:write"))):
    return _plan_out(c, c.repo.cancel_plan(plan_id, c.role, c.signature))


@router.post("/plans/{plan_id}/deposit")
def deposit(plan_id: str, body: DepositIn, c: Ctx = Depends(context("payments:write"))):
    r = c.repo.record_deposit(plan_id, body.amount_counted, c.who, c.role)
    if all(s.status != "pending" for s in c.repo.list_stops(plan_id)):
        c.repo.complete_plan(plan_id, c.role)
    return r


# ---------------------------------------------------------------- driver
@router.get("/driver/today")
def driver_today(c: Ctx = Depends(context("stops:close"))):
    out = [_plan_out(c, p) for p in c.repo.list_plans(status="approved")] + [_plan_out(c, p) for p in c.repo.list_plans(status="loaded")]
    return sorted(out, key=lambda p: p["plan_date"], reverse=True)


@router.post("/stops/{stop_id}/close")
def close_stop(stop_id: str, body: CloseIn, c: Ctx = Depends(context("stops:close"))):
    # Idempotency and the race guard live in the repository (one write-locked transaction, guarded UPDATE,
    # client_ref stored UNIQUE): an exact replay from the offline queue returns the recorded result with
    # replayed=True; any other close of an already-closed stop is a 409 "already ...".
    r = c.repo.close_stop(stop_id, [ln.model_dump() for ln in body.delivered_items], [ln.model_dump() for ln in body.returned_items],
                          body.cash_collected, body.otp, c.role, body.note, client_ref=body.client_ref)
    if r.get("invoice_id") and not r.get("replayed"):     # a replay must not message the customer again
        cust = c.repo.get_customer(r["customer_id"])
        c.repo.queue_message("whatsapp", cust.phone, f"{c.repo.business_name}: delivered. Invoice {r['invoice_id']} Rs {r['invoiced']:,.0f}, cash received Rs {r['cash_collected']:,.0f}. Balance Rs {c.repo.outstanding(r['customer_id']):,.0f}.", r["invoice_id"])
        c.platform.deliver_messages()
    return r


# ---------------------------------------------------------------- voice (optional, needs GROQ_API_KEY)
@router.post("/voice")
async def voice(thread_id: str = "main", audio: UploadFile = File(...), c: Ctx = Depends(context("chat"))):
    key = os.environ.get("GROQ_API_KEY")
    if not key: raise HTTPException(503, "Voice needs GROQ_API_KEY (free at console.groq.com)")
    data = await audio.read()
    if len(data) > 6 * 1024 * 1024: raise HTTPException(413, "voice note too long")
    from groq import Groq
    tr = Groq(api_key=key).audio.transcriptions.create(file=(audio.filename or "note.webm", data), model="whisper-large-v3",
                                                        language="ur" if os.environ.get("VOICE_LANG", "auto") == "ur" else None)
    text = tr.text.strip()
    r = c.platform.handle_message(thread_id, c.role, text, user=c.who)
    return {"transcript": text, "text": r.text, "specialist": r.specialist,
            "pending": _pending_out(c, asdict(r.pending) | {"summary": r.pending.describe()}) if r.pending else None}
