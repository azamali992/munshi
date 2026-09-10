"""Receivables: aging, reminders and promises to pay."""
from __future__ import annotations

from datetime import date

from munshi.domain.models import Promise, Reminder, today_iso
from munshi.domain.repository.base import NotFoundError, new_id
from munshi.domain.repository.cash import CashMixin


class CollectionsMixin(CashMixin):
    def aging(self, as_of: str | None = None, customer_id: str | None = None) -> list[dict]:
        """Per customer: outstanding balance and the oldest unpaid invoice age.
        Payments and credits are applied oldest-first (FIFO), the way a munshi does it."""
        as_of_d = date.fromisoformat(as_of or today_iso())
        out = []
        customers = [self.get_customer(customer_id)] if customer_id else self.list_customers(include_inactive=True)
        for cust in customers:
            entries = self.ledger_for(cust.customer_id)
            if not entries: continue
            invoices = [e for e in entries if e.kind == "invoice"]
            credits = -sum(e.amount for e in entries if e.kind != "invoice")
            open_inv = []
            for inv in invoices:
                if credits >= inv.amount: credits -= inv.amount
                else:
                    open_inv.append((inv, inv.amount - credits)); credits = 0
            bal = round(sum(a for _, a in open_inv), 2)
            if bal <= 0: continue
            oldest = min(open_inv, key=lambda t: t[0].created_at)[0]
            due = date.fromisoformat(oldest.due_date) if oldest.due_date else as_of_d
            days_over = max(0, (as_of_d - due).days)
            bucket = "current" if days_over == 0 else "1-30" if days_over <= 30 else "31-60" if days_over <= 60 else "60+"
            promise = self.open_promise(cust.customer_id)
            out.append({"customer_id": cust.customer_id, "name": cust.name, "phone": cust.phone, "language": cust.language,
                        "balance": bal, "days_overdue": days_over, "bucket": bucket, "credit_limit": cust.credit_limit,
                        "oldest_invoice": oldest.entry_id, "promise": promise})
        return sorted(out, key=lambda r: (-r["days_overdue"], -r["balance"]))

    def aging_summary(self) -> dict:
        rows = self.aging()
        buckets = {"current": 0.0, "1-30": 0.0, "31-60": 0.0, "60+": 0.0}
        for r in rows: buckets[r["bucket"]] += r["balance"]
        return {"total": round(sum(buckets.values()), 2), "customers": len(rows), "buckets": {k: round(v, 2) for k, v in buckets.items()}}

    # ------------------------------------------------------------ reminders
    def draft_reminder(self, customer_id: str, tier: str, amount_due: float, days_overdue: int, message: str, actor: str) -> Reminder:
        r = Reminder(new_id("REM"), customer_id, tier, round(float(amount_due), 2), int(days_overdue), message)
        with self._tx() as c:
            c.execute("INSERT INTO reminders VALUES (?,?,?,?,?,?,?,?)", (r.reminder_id, customer_id, tier, r.amount_due, r.days_overdue, message, "drafted", r.created_at))
        self.audit(actor, "draft_reminder", "reminder", r.reminder_id, {"customer": customer_id, "tier": tier})
        return r

    def get_reminder(self, reminder_id: str) -> Reminder:
        r = self._one("SELECT * FROM reminders WHERE reminder_id=?", (reminder_id,))
        if not r: raise NotFoundError(f"no such reminder: {reminder_id}")
        return Reminder(**dict(r))

    def set_reminder_status(self, reminder_id: str, status: str, actor: str, approved_by: str | None = None) -> Reminder:
        self.get_reminder(reminder_id)
        with self._tx() as c:
            c.execute("UPDATE reminders SET status=? WHERE reminder_id=?", (status, reminder_id))
        self.audit(actor, f"reminder_{status}", "reminder", reminder_id, {}, approved_by)
        return self.get_reminder(reminder_id)

    def list_reminders(self, status: str | None = None, limit: int = 100) -> list[Reminder]:
        rows = self._all("SELECT * FROM reminders WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)) if status else self._all("SELECT * FROM reminders ORDER BY created_at DESC LIMIT ?", (limit,))
        return [Reminder(**dict(r)) for r in rows]

    # ------------------------------------------------------------ promises
    def log_promise(self, customer_id: str, amount: float, promised_date: str, actor: str, approved_by: str | None = None) -> Promise:
        self.get_customer(customer_id)
        if float(amount) <= 0: raise ValueError("promised amount must be positive")
        date.fromisoformat(promised_date)   # validates
        p = Promise(new_id("PRM"), customer_id, round(float(amount), 2), promised_date)
        with self._tx() as c:
            c.execute("INSERT INTO promises VALUES (?,?,?,?,?)", (p.promise_id, customer_id, p.amount, promised_date, p.created_at))
        self.audit(actor, "log_promise", "promise", p.promise_id, {"customer": customer_id, "amount": p.amount, "date": promised_date}, approved_by)
        return p

    def list_promises(self, customer_id: str | None = None) -> list[Promise]:
        q, a = ("SELECT * FROM promises WHERE customer_id=? ORDER BY promised_date", (customer_id,)) if customer_id else ("SELECT * FROM promises ORDER BY promised_date", ())
        return [Promise(**dict(r)) for r in self._all(q, a)]

    def open_promise(self, customer_id: str) -> dict | None:
        """The latest promise, and whether it has been kept (a payment of at least that amount since it was made)."""
        ps = self.list_promises(customer_id)
        if not ps: return None
        p = max(ps, key=lambda x: x.created_at)
        paid_since = -sum(e.amount for e in self.ledger_for(customer_id) if e.kind == "payment" and e.created_at >= p.created_at)
        kept = paid_since >= p.amount
        broken = (not kept) and date.fromisoformat(p.promised_date) < date.today()
        return {"promise_id": p.promise_id, "amount": p.amount, "date": p.promised_date, "kept": kept, "broken": broken}

    def broken_promises(self) -> list[dict]:
        out = []
        for c in self.list_customers():
            p = self.open_promise(c.customer_id)
            if p and p["broken"]: out.append({"customer_id": c.customer_id, "name": c.name, **p})
        return out
