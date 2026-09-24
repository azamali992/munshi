"""Money: the customer khata (invoices, payments, credit notes), driver
deposits and their reconciliation, office payments, expenses, purchases
and what is owed to suppliers."""
from __future__ import annotations

import json
from datetime import date

from munshi.domain.models import (
    CashDeposit,
    Expense,
    LedgerEntry,
    Purchase,
    SupplierLedgerEntry,
    business_today,
    sql_business_date,
    today_iso,
)
from munshi.domain.repository.base import NotFoundError, StateError, new_id
from munshi.domain.repository.dispatch import DispatchMixin

PAYMENT_METHODS = ("cash", "bank", "jazzcash", "easypaisa", "cheque", "adjustment")
EXPENSE_CATEGORIES = ("fuel", "salary", "rent", "repair", "utilities", "loading", "food", "misc")


class CashMixin(DispatchMixin):
    # ------------------------------------------------------------ deposits
    def record_deposit(self, plan_id: str, amount_counted: float, counted_by: str, actor: str) -> dict:
        plan = self.get_plan(plan_id)
        if plan.status == "planned": raise StateError(f"plan {plan_id} hasn't been loaded yet")
        if float(amount_counted) < 0: raise ValueError("counted amount cannot be negative")
        stops = self.list_stops(plan_id)
        expected = round(sum(s.cash_collected for s in stops), 2)
        already = round(sum(d.amount_counted for d in self.deposits(plan_id)), 2)
        variance = round(float(amount_counted) + already - expected, 2)
        dep = CashDeposit(new_id("DEP"), plan_id, float(amount_counted), counted_by)
        with self._tx() as c:
            c.execute("INSERT INTO deposits VALUES (?,?,?,?,?)", (dep.deposit_id, plan_id, dep.amount_counted, counted_by, dep.deposited_at))
        # attribution: the stop whose cash is closest to the shortfall is the first place to look
        suspects = []
        if variance < 0:
            gap = -variance
            suspects = sorted((s for s in stops if s.cash_collected > 0), key=lambda s: abs(s.cash_collected - gap))[:2]
            self.notify("owner", "variance", f"Cash short by Rs {gap:,.0f} on {plan_id} ({self.get_vehicle(plan.vehicle_id).plate})", plan_id)
        self.audit(actor, "record_deposit", "plan", plan_id, {"expected": expected, "counted": amount_counted, "variance": variance})
        return {"plan_id": plan_id, "expected": expected, "counted": float(amount_counted), "previously_deposited": already, "variance": variance,
                "suspect_stops": [{"stop_id": s.stop_id, "customer_id": s.customer_id, "customer_name": self.get_customer(s.customer_id).name, "cash_collected": s.cash_collected} for s in suspects]}

    def deposits(self, plan_id: str) -> list[CashDeposit]:
        return [CashDeposit(**dict(r)) for r in self._all("SELECT * FROM deposits WHERE plan_id=?", (plan_id,))]

    # ------------------------------------------------------------ khata
    def _ledger(self, r) -> LedgerEntry:
        d = dict(r); d.setdefault("method", ""); d.setdefault("received_by", ""); return LedgerEntry(**d)

    def ledger_for(self, customer_id: str) -> list[LedgerEntry]:
        return [self._ledger(r) for r in self._all("SELECT * FROM ledger WHERE customer_id=? ORDER BY created_at, rowid", (customer_id,))]

    def get_ledger_entry(self, entry_id: str) -> LedgerEntry:
        r = self._one("SELECT * FROM ledger WHERE entry_id=?", (entry_id,))
        if not r: raise NotFoundError(f"no such entry: {entry_id}")
        return self._ledger(r)

    def ledger_between(self, start: str, end: str, kind: str | None = None) -> list[LedgerEntry]:
        """Entries whose Pakistan business day falls in [start, end] (inclusive, YYYY-MM-DD)."""
        q, a = f"SELECT * FROM ledger WHERE {sql_business_date('created_at')} BETWEEN ? AND ?", [start, end]
        if kind: q += " AND kind=?"; a.append(kind)
        return [self._ledger(r) for r in self._all(q + " ORDER BY created_at, rowid", tuple(a))]

    def add_ledger(self, customer_id: str, kind: str, amount: float, ref: str, due_date: str | None, actor: str,
                   approved_by: str | None = None, method: str = "", received_by: str = "") -> LedgerEntry:
        self.get_customer(customer_id)
        prefix = {"invoice": self.setting("invoice_prefix") or "INV", "payment": "RCP", "credit_note": "CRN"}.get(kind, kind[:3].upper())
        e = LedgerEntry(new_id(prefix), customer_id, kind, round(float(amount), 2), ref, due_date, method=method, received_by=received_by)
        with self._tx() as c:
            c.execute("INSERT INTO ledger (entry_id, customer_id, kind, amount, ref, due_date, created_at, method, received_by) VALUES (?,?,?,?,?,?,?,?,?)",
                      (e.entry_id, customer_id, kind, e.amount, ref, due_date, e.created_at, method, received_by))
        self.audit(actor, f"ledger_{kind}", "ledger", e.entry_id, {"customer": customer_id, "amount": e.amount, "method": method}, approved_by)
        return e

    def record_payment(self, customer_id: str, amount: float, method: str, ref: str, actor: str, approved_by: str | None = None, received_by: str = "") -> LedgerEntry:
        """A payment received away from the delivery: at the counter, by bank transfer, JazzCash, Easypaisa or cheque."""
        if float(amount) <= 0: raise ValueError("payment must be positive")
        if method not in PAYMENT_METHODS: raise ValueError(f"method must be one of {', '.join(PAYMENT_METHODS)}")
        return self.add_ledger(customer_id, "payment", -abs(float(amount)), ref or method, None, actor, approved_by, method, received_by or self._current_user())

    def opening_balance(self, customer_id: str, amount: float, actor: str, approved_by: str | None = None) -> LedgerEntry:
        """Bring a customer's paper khata in: one invoice-like entry dated today (Pakistan business day)."""
        due = today_iso()
        return self.add_ledger(customer_id, "invoice", abs(float(amount)), "opening balance", due, actor, approved_by, "adjustment")

    # ------------------------------------------------------------ expenses
    def record_expense(self, category: str, amount: float, note: str, method: str, paid_by: str, actor: str, approved_by: str | None = None, expense_date: str | None = None) -> Expense:
        if float(amount) <= 0: raise ValueError("expense must be positive")
        category = category if category in EXPENSE_CATEGORIES else "misc"
        e = Expense(new_id("EXP"), category, round(float(amount), 2), note[:120], method or "cash", paid_by or self._current_user(), expense_date or today_iso())
        with self._tx() as c:
            c.execute("INSERT INTO expenses VALUES (?,?,?,?,?,?,?,?)", (e.expense_id, e.category, e.amount, e.note, e.method, e.paid_by, e.expense_date, e.created_at))
        self.audit(actor, "record_expense", "expense", e.expense_id, {"category": category, "amount": e.amount}, approved_by)
        return e

    def expenses_between(self, start: str, end: str) -> list[Expense]:
        return [Expense(**dict(r)) for r in self._all("SELECT * FROM expenses WHERE expense_date BETWEEN ? AND ? ORDER BY expense_date, rowid", (start, end))]

    # ------------------------------------------------------------ purchases + suppliers
    def record_purchase(self, supplier_id: str, warehouse_id: str, items: list[dict], invoice_ref: str, paid_amount: float, actor: str, approved_by: str | None = None) -> Purchase:
        sup = self.get_supplier(supplier_id); self.get_warehouse(warehouse_id)
        if not items: raise ValueError("a purchase needs at least one line")
        lines, total = [], 0.0
        for it in items:
            p = self.get_product(str(it["sku"])); qty = int(it["qty"])
            if qty <= 0: raise ValueError(f"bad quantity for {p.sku}")
            cost = float(it.get("unit_cost") or p.cost_price or 0)
            lines.append({"sku": p.sku, "qty": qty, "unit_cost": cost}); total += qty * cost
        if float(paid_amount) < 0 or float(paid_amount) > total + 0.01: raise ValueError("paid amount must be between 0 and the bill total")
        pur = Purchase(new_id("PUR"), supplier_id, warehouse_id, lines, round(total, 2), invoice_ref, float(paid_amount))
        with self._tx() as c:
            c.execute("INSERT INTO purchases VALUES (?,?,?,?,?,?,?,?)", (pur.purchase_id, supplier_id, warehouse_id, json.dumps(lines), pur.total, invoice_ref, pur.paid_amount, pur.created_at))
            for ln in lines:
                self.move_stock(warehouse_id, ln["sku"], ln["qty"], "purchase", pur.purchase_id)
                if ln["unit_cost"] > 0:
                    c.execute("UPDATE products SET cost_price=? WHERE sku=?", (ln["unit_cost"], ln["sku"]))
            c.execute("INSERT INTO supplier_ledger VALUES (?,?,?,?,?,?,?)", (new_id("BIL"), supplier_id, "bill", pur.total, pur.purchase_id, "", pur.created_at))
            if pur.paid_amount > 0:
                c.execute("INSERT INTO supplier_ledger VALUES (?,?,?,?,?,?,?)", (new_id("SPY"), supplier_id, "payment", -pur.paid_amount, pur.purchase_id, "cash", pur.created_at))
        self.audit(actor, "record_purchase", "purchase", pur.purchase_id, {"supplier": sup.name, "total": pur.total, "paid": pur.paid_amount, "warehouse": warehouse_id}, approved_by)
        return pur

    def list_purchases(self, limit: int = 50, supplier_id: str | None = None) -> list[Purchase]:
        q, a = ("SELECT * FROM purchases WHERE supplier_id=? ORDER BY created_at DESC LIMIT ?", (supplier_id, limit)) if supplier_id else ("SELECT * FROM purchases ORDER BY created_at DESC LIMIT ?", (limit,))
        out = []
        for r in self._all(q, a):
            d = dict(r); d["items"] = json.loads(d["items"]); out.append(Purchase(**d))
        return out

    def get_purchase(self, purchase_id: str) -> Purchase:
        r = self._one("SELECT * FROM purchases WHERE purchase_id=?", (purchase_id,))
        if not r: raise NotFoundError(f"no such purchase: {purchase_id}")
        d = dict(r); d["items"] = json.loads(d["items"]); return Purchase(**d)

    def pay_supplier(self, supplier_id: str, amount: float, method: str, ref: str, actor: str, approved_by: str | None = None) -> SupplierLedgerEntry:
        self.get_supplier(supplier_id)
        if float(amount) <= 0: raise ValueError("payment must be positive")
        if method not in PAYMENT_METHODS: raise ValueError(f"method must be one of {', '.join(PAYMENT_METHODS)}")
        e = SupplierLedgerEntry(new_id("SPY"), supplier_id, "payment", -abs(float(amount)), ref or method, method)
        with self._tx() as c:
            c.execute("INSERT INTO supplier_ledger VALUES (?,?,?,?,?,?,?)", (e.entry_id, supplier_id, "payment", e.amount, e.ref, method, e.created_at))
        self.audit(actor, "pay_supplier", "supplier", supplier_id, {"amount": abs(e.amount), "method": method, "ref": ref}, approved_by)
        return e

    def supplier_ledger(self, supplier_id: str) -> list[SupplierLedgerEntry]:
        return [SupplierLedgerEntry(**dict(r)) for r in self._all("SELECT * FROM supplier_ledger WHERE supplier_id=? ORDER BY created_at, rowid", (supplier_id,))]

    def supplier_balance(self, supplier_id: str) -> float:
        r = self._one("SELECT COALESCE(SUM(amount),0) s FROM supplier_ledger WHERE supplier_id=?", (supplier_id,))
        return round(float(r["s"]), 2)

    def payables(self) -> list[dict]:
        out = []
        for s in self.list_suppliers():
            bal = self.supplier_balance(s.supplier_id)
            if bal > 0: out.append({"supplier_id": s.supplier_id, "name": s.name, "phone": s.phone, "balance": bal})
        return sorted(out, key=lambda r: -r["balance"])

    # ------------------------------------------------------------ cashbook
    def cashbook(self, day: str | None = None) -> dict:
        """Everything that touched physical cash on a Pakistan business day, in and out."""
        day = day or today_iso()
        ins, outs = [], []
        for e in self.ledger_between(day, day, "payment"):
            if (e.method or "cash") == "cash" and e.received_by != "driver":   # driver cash arrives as a hand-in
                who = self.get_customer(e.customer_id).name
                ins.append({"kind": "customer payment", "who": who, "amount": abs(e.amount), "ref": e.entry_id, "by": e.received_by, "at": e.created_at})
        for x in self.expenses_between(day, day):
            if x.method == "cash": outs.append({"kind": f"expense · {x.category}", "who": x.note or x.category, "amount": x.amount, "ref": x.expense_id, "by": x.paid_by, "at": x.created_at})
        for r in self._all("SELECT * FROM supplier_ledger WHERE kind='payment' AND method='cash' AND " + sql_business_date("created_at") + "=?", (day,)):
            outs.append({"kind": "supplier payment", "who": self.get_supplier(r["supplier_id"]).name, "amount": abs(r["amount"]), "ref": r["entry_id"], "by": "", "at": r["created_at"]})
        deposits = [{"kind": "driver hand-in", "who": self.get_vehicle(self.get_plan(r["plan_id"]).vehicle_id).plate, "amount": r["amount_counted"], "ref": r["deposit_id"], "by": r["counted_by"], "at": r["deposited_at"]}
                    for r in self._all("SELECT * FROM deposits WHERE " + sql_business_date("deposited_at") + "=?", (day,))]
        total_in = round(sum(i["amount"] for i in ins), 2); total_out = round(sum(o["amount"] for o in outs), 2)
        return {"date": day, "cash_in": ins, "driver_handins": deposits, "cash_out": outs,
                "total_in": total_in, "total_handins": round(sum(d["amount"] for d in deposits), 2), "total_out": total_out,
                "net": round(total_in + sum(d["amount"] for d in deposits) - total_out, 2)}

    @staticmethod
    def _today() -> date:
        return business_today()
