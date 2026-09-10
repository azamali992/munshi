"""Operations: chat with the munshis, approvals, notifications, orders,
dispatch plans and the driver's stops. Form-based writes (a clerk tapping
'Confirm' instead of asking the agent) go straight to the repository with
the human recorded as both actor and approver."""
from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from munshi.domain.models import today_iso
from munshi.safety.risk import role_may_approve
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


class CloseIn(BaseModel):
    delivered_items: list[dict]
    returned_items: list[dict] = []
    cash_collected: float = Field(default=0, ge=0)
    otp: str = Field(min_length=4, max_length=4, pattern="^\\d{4}$")
    client_ref: str = Field(default="", max_length=40)     # idempotency key from the offline queue
    note: str = Field(default="", max_length=200)


class DepositIn(BaseModel):
    amount_counted: float = Field(ge=0)


def _pending_out(c: Ctx, p: dict) -> dict:
    return p | {"can_approve": role_may_approve(c.role, p["tool"]) and (c.role == "owner" or p["needs_role"] == "clerk")}


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
    limit = float(c.repo.setting("big_order_limit") or 0)
    if limit and o.total > limit and c.role != "owner":
        raise HTTPException(403, f"orders above Rs {limit:,.0f} need the owner")
    hold = c.repo.over_credit(o)
    if hold and c.role != "owner":
        raise HTTPException(403, hold + " — the owner must confirm")
    return c.platform.ops._order(c.repo.confirm_order(order_id, c.role, c.signature, override_credit=c.role == "owner"))


@router.post("/orders/{order_id}/cancel")
def cancel(order_id: str, body: CancelIn, c: Ctx = Depends(context("dispatch:write"))):
    return c.platform.ops._order(c.repo.cancel_order(order_id, body.reason or "cancelled in app", c.role, c.signature))


@router.post("/orders/{order_id}/allocate")
def allocate(order_id: str, body: AllocateIn, c: Ctx = Depends(context("dispatch:write"))):
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
    # idempotent for the offline queue: a replayed close of an already-closed stop returns the recorded result
    st = c.repo.get_stop(stop_id)
    if st.status != "pending" and body.client_ref:
        rows = c.repo.audit_log(5, stop_id)
        if rows:
            p = rows[0]["payload"]
            return {"stop_id": stop_id, "status": st.status, "invoiced": p.get("value", 0), "cash_collected": st.cash_collected, "invoice_id": p.get("invoice"), "replayed": True}
    r = c.repo.close_stop(stop_id, body.delivered_items, body.returned_items, body.cash_collected, body.otp, c.role, body.note)
    if r.get("invoice_id"):
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
