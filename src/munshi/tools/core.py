"""Every tool as a plain method on MunshiTools. Framework-free: the LangChain
wrappers, the HTTP API and the tests all call these same methods. Each
method knows which agent it belongs to (the `actor` it writes to the audit)."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta

from munshi.domain.models import today_iso
from munshi.domain.repository import MunshiRepository

REMINDER_TEMPLATES = {
    "gentle": {
        "ur-en": "Assalam o Alaikum {name} sahib. {business} ki taraf se yaad-dehani: aap ke khate mein Rs {amount:,.0f} baqi hain ({days} din). Jab sahoolat ho, adaigi kar dein. Shukriya.",
        "en": "Dear {name}, a friendly reminder from {business}: Rs {amount:,.0f} is outstanding on your account ({days} days). Please arrange payment at your convenience. Thank you.",
    },
    "firm": {
        "ur-en": "{name} sahib, {business}. Aap ke khate mein Rs {amount:,.0f} {days} din se baqi hain. Barah-e-karam is hafte adaigi yaqeeni banayein taake supply jari rahe.",
        "en": "{name}, this is {business}. Rs {amount:,.0f} has been outstanding for {days} days. Please settle this week so we can keep supply uninterrupted.",
    },
    "final": {
        "ur-en": "{name} sahib, {business}. Rs {amount:,.0f} {days} din se baqi hain. Adaigi na hone par nayi supply rok di jaye gi. Barah-e-karam foran rabta karein.",
        "en": "{name}, final notice from {business}: Rs {amount:,.0f} is {days} days overdue. New supply will be held until this is settled. Please contact us today.",
    },
}


class MunshiTools:
    def __init__(self, repo: MunshiRepository) -> None:
        self.repo = repo

    # ---------- lookups ----------
    def find_customer(self, text: str) -> dict:
        c = self.repo.find_customer(text)
        if not c: return {"found": False, "query": text}
        return {"found": True, **asdict(c), "outstanding": self.repo.outstanding(c.customer_id)}

    def get_customer_khata(self, customer_id: str) -> dict:
        c = self.repo.get_customer(customer_id)
        ag = next((a for a in self.repo.aging(customer_id=customer_id)), None)
        entries = self.repo.ledger_for(customer_id)[-10:]
        return {"customer": asdict(c), "outstanding": self.repo.outstanding(customer_id), "aging": ag,
                "recent": [asdict(e) for e in entries], "promise": self.repo.open_promise(customer_id)}

    def search_products(self, text: str) -> list[dict]:
        t = text.lower()
        hits = []
        for p in self.repo.list_products():
            hay = [p.sku.lower(), p.name.lower(), *[a.lower() for a in p.aliases]]
            if any(t in h or h in t for h in hay):
                hits.append(asdict(p))
        return hits[:8]

    def get_stock(self, sku: str) -> list[dict]:
        self.repo.get_product(sku)
        return [asdict(s) | {"available": s.available} for s in self.repo.stock_by_sku(sku)]

    def list_orders(self, status: str = "") -> list[dict]:
        return [self._order(o) for o in self.repo.list_orders(status or None, limit=40)]

    def get_order(self, order_id: str) -> dict:
        return self._order(self.repo.get_order(order_id))

    def list_routes(self) -> list[dict]:
        return [asdict(r) for r in self.repo.list_routes()]

    def list_vehicles(self) -> list[dict]:
        return [asdict(v) for v in self.repo.list_vehicles()]

    def get_plan(self, plan_id: str) -> dict:
        p = self.repo.get_plan(plan_id)
        return asdict(p) | {"stops": [asdict(s) for s in self.repo.list_stops(plan_id)]}

    def list_stops(self, plan_id: str) -> list[dict]:
        return [asdict(s) | {"customer_name": self.repo.get_customer(s.customer_id).name} for s in self.repo.list_stops(plan_id)]

    def aging_report(self, limit: int = 30) -> list[dict]:
        return self.repo.aging()[:max(1, min(int(limit or 30), 200))]

    def get_digest(self) -> dict:
        return self.repo.digest()

    def suggest_dispatch(self, plan_date: str = "") -> list[dict]:
        """Group allocated orders by their customer's route and pick the smallest
        vehicle that fits each load. Pure planning; creates nothing."""
        plan_date = plan_date or today_iso()
        allocated = [o for o in self.repo.list_orders("allocated", limit=200)]
        on_plan = {r["order_id"] for r in self.repo._all("SELECT order_id FROM stops WHERE status='pending'")}
        by_route: dict[str, list] = {}
        for o in allocated:
            if o.order_id in on_plan: continue
            c = self.repo.get_customer(o.customer_id)
            rid = c.route_id or "UNROUTED"
            by_route.setdefault(rid, []).append(o)
        vehicles = sorted(self.repo.list_vehicles(), key=lambda v: v.capacity_units)
        out = []
        for rid, orders in by_route.items():
            load = sum(o.load_units for o in orders)
            fit = next((v for v in vehicles if v.capacity_units >= load), None)
            out.append({"route_id": rid, "plan_date": plan_date, "order_ids": [o.order_id for o in orders],
                        "load_units": load, "vehicle_id": fit.vehicle_id if fit else None,
                        "note": "" if fit else f"no single vehicle fits {load} units; split the route"})
        return out

    # ---------- order desk ----------
    def create_order(self, customer_id: str, items: list[dict], source_text: str = "", channel: str = "chat") -> dict:
        o = self.repo.create_order(customer_id, items, channel, source_text, "order_munshi")
        return self._order(o)

    def confirm_order(self, order_id: str, approved_by: str = "clerk") -> dict:
        # the platform escalates an over-limit confirmation to the owner before this runs, so an approved call may override
        return self._order(self.repo.confirm_order(order_id, "order_munshi", approved_by, override_credit=True))

    def cancel_order(self, order_id: str, reason: str = "", approved_by: str = "clerk") -> dict:
        return self._order(self.repo.cancel_order(order_id, reason or "cancelled", "order_munshi", approved_by))

    # ---------- godown ----------
    def allocate_order(self, order_id: str, warehouse_id: str = "", approved_by: str = "clerk") -> dict:
        return self.repo.allocate_order(order_id, warehouse_id or self.repo.default_warehouse_id(), "godown_munshi", approved_by)

    def create_dispatch_plan(self, route_id: str, vehicle_id: str, order_ids: list[str], plan_date: str = "") -> dict:
        p = self.repo.create_dispatch_plan(plan_date or today_iso(), route_id, vehicle_id, order_ids, "godown_munshi")
        return asdict(p)

    def approve_dispatch_plan(self, plan_id: str, approved_by: str = "clerk") -> dict:
        return asdict(self.repo.approve_dispatch_plan(plan_id, "godown_munshi", approved_by))

    def adjust_stock(self, warehouse_id: str, sku: str, delta: int, reason: str, approved_by: str = "owner") -> dict:
        s = self.repo.adjust_stock(warehouse_id or self.repo.default_warehouse_id(), sku, delta, reason, "godown_munshi", approved_by)
        return asdict(s) | {"available": s.available}

    def transfer_stock(self, from_warehouse: str, to_warehouse: str, sku: str, qty: int, approved_by: str = "clerk") -> dict:
        return self.repo.transfer_stock(from_warehouse, to_warehouse, sku, qty, "godown_munshi", approved_by)

    # ---------- delivery ----------
    def close_stop(self, stop_id: str, delivered_items: list[dict], returned_items: list[dict], cash_collected: float, otp: str, note: str = "") -> dict:
        return self.repo.close_stop(stop_id, delivered_items, returned_items, cash_collected, otp, "delivery_munshi", note)

    # ---------- hisaab ----------
    def record_deposit(self, plan_id: str, amount_counted: float, counted_by: str = "cashier") -> dict:
        r = self.repo.record_deposit(plan_id, amount_counted, counted_by, "hisaab_munshi")
        if all(s.status != "pending" for s in self.repo.list_stops(plan_id)):
            self.repo.complete_plan(plan_id, "hisaab_munshi")
        return r

    def record_payment(self, customer_id: str, amount: float, method: str = "cash", ref: str = "", approved_by: str = "clerk") -> dict:
        e = self.repo.record_payment(customer_id, amount, method or "cash", ref, "hisaab_munshi", approved_by)
        c = self.repo.get_customer(customer_id)
        self.repo.queue_message("whatsapp", c.phone, f"{self.repo.business_name}: Rs {abs(e.amount):,.0f} received ({e.method}). Receipt {e.entry_id}. Balance now Rs {self.repo.outstanding(customer_id):,.0f}. Shukriya.", e.entry_id)
        return asdict(e) | {"customer_name": c.name, "outstanding": self.repo.outstanding(customer_id)}

    def record_expense(self, category: str, amount: float, note: str = "", method: str = "cash", approved_by: str = "clerk") -> dict:
        return asdict(self.repo.record_expense(category, amount, note, method, "", "hisaab_munshi", approved_by))

    def credit_note(self, customer_id: str, amount: float, reason: str, approved_by: str = "owner") -> dict:
        e = self.repo.add_ledger(customer_id, "credit_note", -abs(float(amount)), reason, None, "hisaab_munshi", approved_by, "adjustment")
        return asdict(e)

    def cashbook(self, day: str = "") -> dict:
        return self.repo.cashbook(day or None)

    # ---------- khareed (purchases) ----------
    def find_supplier(self, text: str) -> dict:
        s = self.repo.find_supplier(text)
        if not s: return {"found": False, "query": text}
        return {"found": True, **asdict(s), "balance": self.repo.supplier_balance(s.supplier_id)}

    def list_suppliers(self) -> list[dict]:
        return [asdict(s) | {"balance": self.repo.supplier_balance(s.supplier_id)} for s in self.repo.list_suppliers()]

    def supplier_khata(self, supplier_id: str) -> dict:
        s = self.repo.get_supplier(supplier_id)
        return {"supplier": asdict(s), "balance": self.repo.supplier_balance(supplier_id), "recent": [asdict(e) for e in self.repo.supplier_ledger(supplier_id)[-10:]],
                "purchases": [asdict(p) for p in self.repo.list_purchases(5, supplier_id)]}

    def payables_report(self) -> list[dict]:
        return self.repo.payables()

    def record_purchase(self, supplier_id: str, items: list[dict], warehouse_id: str = "", invoice_ref: str = "", paid_amount: float = 0, approved_by: str = "clerk") -> dict:
        p = self.repo.record_purchase(supplier_id, warehouse_id or self.repo.default_warehouse_id(), items, invoice_ref, paid_amount, "khareed_munshi", approved_by)
        return asdict(p) | {"supplier_name": self.repo.get_supplier(supplier_id).name, "balance": self.repo.supplier_balance(supplier_id)}

    def pay_supplier(self, supplier_id: str, amount: float, method: str = "cash", ref: str = "", approved_by: str = "owner") -> dict:
        e = self.repo.pay_supplier(supplier_id, amount, method or "cash", ref, "khareed_munshi", approved_by)
        return asdict(e) | {"supplier_name": self.repo.get_supplier(supplier_id).name, "balance": self.repo.supplier_balance(supplier_id)}

    # ---------- wasooli ----------
    def draft_reminder(self, customer_id: str, tier: str = "") -> dict:
        ag = next((a for a in self.repo.aging(customer_id=customer_id)), None)
        if not ag: return {"drafted": False, "reason": f"{customer_id} has nothing outstanding"}
        tier = tier or ("final" if ag["days_overdue"] > 60 else "firm" if ag["days_overdue"] > 30 else "gentle")
        c = self.repo.get_customer(customer_id)
        lang = "ur-en" if c.language.startswith("ur") else "en"
        msg = REMINDER_TEMPLATES[tier][lang].format(name=c.name, amount=ag["balance"], days=ag["days_overdue"], business=self.repo.business_name)
        r = self.repo.draft_reminder(customer_id, tier, ag["balance"], ag["days_overdue"], msg, "wasooli_munshi")
        return asdict(r)

    def draft_due_reminders(self, min_days_overdue: int = 1) -> list[dict]:
        """Draft one templated reminder for every customer past the threshold
        who doesn't already have one waiting. Returns the drafts for approval."""
        waiting = {r.customer_id for r in self.repo.list_reminders("drafted")}
        drafts = []
        for a in self.repo.aging():
            if a["days_overdue"] >= min_days_overdue and a["customer_id"] not in waiting:
                drafts.append(self.draft_reminder(a["customer_id"]))
        return drafts

    def send_reminder(self, reminder_id: str, approved_by: str = "clerk") -> dict:
        # Munshi never composes free text to a customer; only templated tiers go out, via the outbox/channel.
        r = self.repo.get_reminder(reminder_id)
        c = self.repo.get_customer(r.customer_id)
        self.repo.queue_message("whatsapp", c.phone, r.message, reminder_id)
        r = self.repo.set_reminder_status(reminder_id, "sent", "wasooli_munshi", approved_by)
        return asdict(r)

    def log_promise(self, customer_id: str, amount: float, promised_date: str, approved_by: str = "clerk") -> dict:
        return asdict(self.repo.log_promise(customer_id, amount, promised_date, "wasooli_munshi", approved_by))

    def broken_promises(self) -> list[dict]:
        return self.repo.broken_promises()

    # ---------- reports (read-only) ----------
    def sales_report(self, start: str = "", end: str = "") -> dict:
        start, end = self._range(start, end)
        return self.repo.sales_report(start, end)

    def profit_summary(self, start: str = "", end: str = "") -> dict:
        start, end = self._range(start, end)
        return self.repo.profit_summary(start, end)

    def collection_report(self, start: str = "", end: str = "") -> dict:
        start, end = self._range(start, end)
        return self.repo.collection_report(start, end)

    def stock_ledger(self, sku: str, warehouse_id: str = "") -> dict:
        return self.repo.stock_ledger(sku, warehouse_id or None)

    def stock_valuation(self) -> dict:
        return self.repo.stock_valuation()

    def slow_stock(self, days: int = 30) -> list[dict]:
        return self.repo.slow_stock(days)

    def top_customers(self, days: int = 30) -> list[dict]:
        return self.repo.top_customers(days)

    # ---------- helpers ----------
    def _order(self, o) -> dict:
        d = asdict(o); d["total"] = o.total; d["load_units"] = o.load_units
        d["customer_name"] = self.repo.get_customer(o.customer_id).name
        return d

    @staticmethod
    def _range(start: str, end: str) -> tuple[str, str]:
        end = end or today_iso()
        start = start or (date.fromisoformat(end) - timedelta(days=29)).isoformat()
        return start, end
