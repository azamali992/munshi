"""Money: khata, payments received, credit notes, purchases and payables,
expenses, reminders and promises, and the outbox of messages to customers."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from munshi.domain.repository import EXPENSE_CATEGORIES, PAYMENT_METHODS
from munshi.web.deps import Ctx, context

router = APIRouter(prefix="/api", tags=["money"])


class PaymentIn(BaseModel):
    customer_id: str
    amount: float = Field(gt=0)
    method: str = Field(default="cash", pattern="^(cash|bank|jazzcash|easypaisa|cheque)$")
    ref: str = Field(default="", max_length=80)


class CreditNoteIn(BaseModel):
    customer_id: str
    amount: float = Field(gt=0)
    reason: str = Field(min_length=3, max_length=120)


class PurchaseLine(BaseModel):
    sku: str
    qty: int = Field(ge=1)
    unit_cost: float = Field(default=0, ge=0)


class PurchaseIn(BaseModel):
    supplier_id: str
    warehouse_id: str = ""
    items: list[PurchaseLine] = Field(min_length=1)
    invoice_ref: str = Field(default="", max_length=40)
    paid_amount: float = Field(default=0, ge=0)


class SupplierPayIn(BaseModel):
    amount: float = Field(gt=0)
    method: str = Field(default="cash", pattern="^(cash|bank|jazzcash|easypaisa|cheque)$")
    ref: str = Field(default="", max_length=80)


class ExpenseIn(BaseModel):
    category: str = Field(pattern="^(" + "|".join(EXPENSE_CATEGORIES) + ")$")
    amount: float = Field(gt=0)
    note: str = Field(default="", max_length=120)
    method: str = Field(default="cash", pattern="^(cash|bank|jazzcash|easypaisa|cheque)$")
    expense_date: str | None = Field(default=None, pattern="^\\d{4}-\\d{2}-\\d{2}$")


class PromiseIn(BaseModel):
    customer_id: str
    amount: float = Field(gt=0)
    promised_date: str = Field(pattern="^\\d{4}-\\d{2}-\\d{2}$")


class ReminderDraftIn(BaseModel):
    customer_id: str
    tier: str = Field(default="", pattern="^(|gentle|firm|final)$")


class AdjustIn(BaseModel):
    warehouse_id: str
    sku: str
    delta: int
    reason: str = Field(min_length=3, max_length=120)


class TransferIn(BaseModel):
    from_warehouse: str
    to_warehouse: str
    sku: str
    qty: int = Field(ge=1)


# ---------------------------------------------------------------- khata
@router.get("/khata")
def khata(c: Ctx = Depends(context("khata:read"))):
    return {"summary": c.repo.aging_summary(), "rows": c.repo.aging(), "broken_promises": c.repo.broken_promises()}


@router.get("/khata/{customer_id}")
def khata_one(customer_id: str, c: Ctx = Depends(context("khata:read"))):
    d = c.platform.ops.get_customer_khata(customer_id)
    d["ledger"] = [asdict(e) for e in c.repo.ledger_for(customer_id)]
    d["orders"] = [c.platform.ops._order(o) for o in c.repo.list_orders(customer_id=customer_id, limit=20)]
    d["promises"] = [asdict(p) for p in c.repo.list_promises(customer_id)]
    return d


@router.post("/payments", status_code=201)
def payment(body: PaymentIn, c: Ctx = Depends(context("payments:write"))):
    r = c.platform.ops.record_payment(body.customer_id, body.amount, body.method, body.ref, approved_by=c.signature)
    c.platform.deliver_messages()
    return r


@router.post("/credit-notes", status_code=201)
def credit_note(body: CreditNoteIn, c: Ctx = Depends(context("settings:write"))):     # owner only
    return c.platform.ops.credit_note(body.customer_id, body.amount, body.reason, approved_by=c.signature)


# ---------------------------------------------------------------- purchases + suppliers
@router.get("/purchases")
def purchases(supplier_id: str = "", c: Ctx = Depends(context("purchases:read"))):
    names = {s.supplier_id: s.name for s in c.repo.list_suppliers()}
    return [asdict(p) | {"supplier_name": names.get(p.supplier_id, p.supplier_id)} for p in c.repo.list_purchases(100, supplier_id or None)]


@router.post("/purchases", status_code=201)
def purchase(body: PurchaseIn, c: Ctx = Depends(context("purchases:write"))):
    return c.platform.ops.record_purchase(body.supplier_id, [l.model_dump() for l in body.items], body.warehouse_id, body.invoice_ref, body.paid_amount, approved_by=c.signature)


@router.get("/payables")
def payables(c: Ctx = Depends(context("purchases:read"))):
    return {"rows": c.repo.payables(), "total": round(sum(p["balance"] for p in c.repo.payables()), 2)}


@router.get("/suppliers/{supplier_id}/khata")
def supplier_khata(supplier_id: str, c: Ctx = Depends(context("purchases:read"))):
    return c.platform.ops.supplier_khata(supplier_id)


@router.post("/suppliers/{supplier_id}/pay", status_code=201)
def pay_supplier(supplier_id: str, body: SupplierPayIn, c: Ctx = Depends(context("settings:write"))):   # owner only
    return c.platform.ops.pay_supplier(supplier_id, body.amount, body.method, body.ref, approved_by=c.signature)


# ---------------------------------------------------------------- stock writes (form-based)
@router.post("/stock/adjust")
def adjust(body: AdjustIn, c: Ctx = Depends(context("settings:write"))):     # owner only
    s = c.repo.adjust_stock(body.warehouse_id, body.sku, body.delta, body.reason, c.role, c.signature)
    return asdict(s) | {"available": s.available}


@router.post("/stock/transfer")
def transfer(body: TransferIn, c: Ctx = Depends(context("dispatch:write"))):
    return c.repo.transfer_stock(body.from_warehouse, body.to_warehouse, body.sku, body.qty, c.role, c.signature)


@router.get("/stock")
def stock(c: Ctx = Depends(context("stock:read"))):
    names = {p.sku: p for p in c.repo.list_products(include_inactive=True)}
    whs = {w.warehouse_id: w.name for w in c.repo.list_warehouses()}
    return [asdict(s) | {"available": s.available, "name": names[s.sku].name if s.sku in names else s.sku, "unit": names[s.sku].unit if s.sku in names else "",
                         "min_stock": names[s.sku].min_stock if s.sku in names else 10, "warehouse": whs.get(s.warehouse_id, s.warehouse_id)}
            for s in c.repo.list_stock() if s.sku in names and names[s.sku].active]


# ---------------------------------------------------------------- expenses
@router.get("/expenses")
def expenses(start: str = "", end: str = "", c: Ctx = Depends(context("reports:read"))):
    from datetime import date, timedelta

    from munshi.domain.models import today_iso
    end = end or today_iso(); start = start or (date.fromisoformat(end) - timedelta(days=29)).isoformat()
    rows = [asdict(x) for x in c.repo.expenses_between(start, end)]
    return {"start": start, "end": end, "rows": rows, "total": round(sum(r["amount"] for r in rows), 2), "categories": list(EXPENSE_CATEGORIES)}


@router.post("/expenses", status_code=201)
def add_expense(body: ExpenseIn, c: Ctx = Depends(context("expenses:write"))):
    return asdict(c.repo.record_expense(body.category, body.amount, body.note, body.method, c.who, c.role, c.signature, body.expense_date))


# ---------------------------------------------------------------- reminders + promises
@router.get("/reminders")
def reminders(status: str = "", c: Ctx = Depends(context("reminders:read"))):
    names = {x.customer_id: x for x in c.repo.list_customers(include_inactive=True)}
    return [asdict(r) | {"customer_name": names[r.customer_id].name if r.customer_id in names else r.customer_id, "phone": names[r.customer_id].phone if r.customer_id in names else ""}
            for r in c.repo.list_reminders(status or None)]


@router.post("/reminders", status_code=201)
def draft_reminder(body: ReminderDraftIn, c: Ctx = Depends(context("reminders:send"))):
    return c.platform.ops.draft_reminder(body.customer_id, body.tier)


@router.post("/reminders/due")
def draft_due(min_days: int = 1, c: Ctx = Depends(context("reminders:send"))):
    return c.platform.ops.draft_due_reminders(max(1, min_days))


@router.post("/reminders/{reminder_id}/send")
def send_reminder(reminder_id: str, c: Ctx = Depends(context("reminders:send"))):
    r = c.platform.ops.send_reminder(reminder_id, approved_by=c.signature)
    c.platform.deliver_messages()
    return r


@router.post("/reminders/{reminder_id}/discard")
def discard_reminder(reminder_id: str, c: Ctx = Depends(context("reminders:send"))):
    return asdict(c.repo.set_reminder_status(reminder_id, "discarded", c.role, c.signature))


@router.get("/promises")
def promises(c: Ctx = Depends(context("khata:read"))):
    names = {x.customer_id: x.name for x in c.repo.list_customers(include_inactive=True)}
    out = []
    for p in c.repo.list_promises():
        st = c.repo.open_promise(p.customer_id) or {}
        out.append(asdict(p) | {"customer_name": names.get(p.customer_id, p.customer_id), "kept": st.get("kept") if st.get("promise_id") == p.promise_id else None,
                                "broken": st.get("broken") if st.get("promise_id") == p.promise_id else None})
    return out


@router.post("/promises", status_code=201)
def log_promise(body: PromiseIn, c: Ctx = Depends(context("khata:read"))):
    # a salesman may log a promise directly (it is a note, not money); still audited with his name
    return asdict(c.repo.log_promise(body.customer_id, body.amount, body.promised_date, c.role, c.signature))


# ---------------------------------------------------------------- outbox
@router.get("/outbox")
def outbox(status: str = "", c: Ctx = Depends(context("reminders:send"))):
    rows = c.repo.outbox(status or None, 100)
    for r in rows:
        digits = "".join(ch for ch in (r["to_phone"] or "") if ch.isdigit())
        if digits.startswith("0") and len(digits) == 11: digits = "92" + digits[1:]
        from urllib.parse import quote
        r["wa_link"] = f"https://wa.me/{digits}?text={quote(r['text'])}" if digits else None
    return {"channel": c.platform.channel.name, "rows": rows}


@router.post("/outbox/{msg_id}/sent")
def mark_sent(msg_id: str, c: Ctx = Depends(context("reminders:send"))):
    c.repo.mark_message(msg_id, "sent"); return {"ok": True}


@router.post("/outbox/{msg_id}/retry")
def retry_message(msg_id: str, c: Ctx = Depends(context("reminders:send"))):
    c.repo.mark_message(msg_id, "queued"); return c.platform.deliver_messages()


@router.post("/outbox/deliver")
def deliver(c: Ctx = Depends(context("reminders:send"))):
    return c.platform.deliver_messages()


@router.get("/methods")
def methods(c: Ctx = Depends(context("chat"))):
    return {"payment_methods": [m for m in PAYMENT_METHODS if m != "adjustment"], "expense_categories": list(EXPENSE_CATEGORIES)}
