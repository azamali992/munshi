"""Money: the customer khata (invoices, payments, credit notes), driver
deposits and their reconciliation, office payments, expenses, purchases
and what is owed to suppliers.

Amounts are integer paisa in the database and in every computation here;
the public methods take and return rupees (see models.to_paisa / to_rupees).

History is append-only (V5 triggers refuse UPDATE/DELETE on ledger,
supplier_ledger, expenses, purchases). A mistake is corrected by a reversing
entry: a new row carrying the exact negation of the original's amount and
`reversal_of` = the original's id. The original stays as it was; balances,
the cashbook and reports net the pair to zero from the reversal's date on.
An entry can be reversed once, and a reversal cannot itself be reversed
(post a fresh, correct entry instead). Reversals are an owner action: the
repository requires a named approver; the web/agent layer must gate them
like credit_note / pay_supplier / adjust_stock (HIGH_RISK, owner)."""
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
    now_iso,
    sql_business_date,
    to_paisa,
    to_rupees,
    today_iso,
)
from munshi.domain.repository.base import NotFoundError, StateError, new_id
from munshi.domain.repository.dispatch import DispatchMixin
from munshi.domain.repository.guarded import immediate_tx
from munshi.domain.repository.numbering import next_doc_no

PAYMENT_METHODS = ("cash", "bank", "jazzcash", "easypaisa", "cheque", "adjustment")
EXPENSE_CATEGORIES = ("fuel", "salary", "rent", "repair", "utilities", "loading", "food", "misc")
# Posted only by record_deposit when a driver hands in less cash than his stops collected. Deliberately
# NOT in EXPENSE_CATEGORIES: nobody can key one by hand (the web form's pattern refuses it and
# record_expense would coerce it to "misc"), so every row in this category is system-derived and
# traceable to a deposit. Its note always starts "<plan_id> ", which is how a plan's rows are found.
SHORTAGE_CATEGORY = "cash_shortage"

_LEDGER_COLS = "entry_id, customer_id, kind, amount, ref, due_date, created_at, method, received_by, doc_no, reversal_of"
_SUP_COLS = "entry_id, supplier_id, kind, amount, ref, method, created_at, reversal_of"
_EXP_COLS = "expense_id, category, amount, note, method, paid_by, expense_date, created_at, reversal_of"
_PUR_COLS = "purchase_id, supplier_id, warehouse_id, items, total, invoice_ref, paid_amount, created_at, doc_no, reversal_of"


def _reason(reason: str) -> str:
    r = (reason or "").strip()
    if len(r) < 3: raise ValueError("a reversal needs a reason (at least 3 characters)")
    return r[:120]


def _approver(approved_by: str | None) -> str:
    if not (approved_by or "").strip():
        raise StateError("a reversal changes the books: it must be approved by the owner")
    return approved_by


class CashMixin(DispatchMixin):
    # ------------------------------------------------------------ deposits
    def record_deposit(self, plan_id: str, amount_counted: float, counted_by: str, actor: str) -> dict:
        counted_p = to_paisa(amount_counted)
        if counted_p < 0: raise ValueError("counted amount cannot be negative")
        with immediate_tx(self) as c:
            plan = self.get_plan(plan_id)
            if plan.status == "planned": raise StateError(f"plan {plan_id} hasn't been loaded yet")
            stops = self._stop_cash_paisa(plan_id)
            expected_p = sum(cash for _, _, cash in stops)
            already_p = self._deposited_paisa(plan_id)
            variance_p = counted_p + already_p - expected_p
            dep = CashDeposit(new_id("DEP"), plan_id, to_rupees(counted_p), counted_by)
            c.execute("INSERT INTO deposits (deposit_id, plan_id, amount_counted, counted_by, deposited_at) VALUES (?,?,?,?,?)",
                      (dep.deposit_id, plan_id, counted_p, counted_by, dep.deposited_at))
            # attribution: the stop whose cash is closest to the shortfall is the first place to look
            suspects = []
            plate = self.get_vehicle(plan.vehicle_id).plate
            if variance_p < 0:
                gap_p = -variance_p
                suspects = sorted((s for s in stops if s[2] > 0), key=lambda s: abs(s[2] - gap_p))[:2]
                self.notify("owner", "variance", f"Cash short by Rs {to_rupees(gap_p):,.0f} on {plan_id} ({plate})", plan_id)
            shortage = self._book_shortage(c, plan_id, plate, max(0, -variance_p), dep.deposit_id, suspects, counted_by, actor)
            self.audit(actor, "record_deposit", "plan", plan_id, {"expected": to_rupees(expected_p), "counted": to_rupees(counted_p), "variance": to_rupees(variance_p),
                                                                  "shortage_entry": shortage["expense_id"] if shortage else None})
        return {"plan_id": plan_id, "expected": to_rupees(expected_p), "counted": to_rupees(counted_p), "previously_deposited": to_rupees(already_p),
                "variance": to_rupees(variance_p),
                "suspect_stops": [{"stop_id": sid, "customer_id": cid, "customer_name": self.get_customer(cid).name, "cash_collected": to_rupees(cash)}
                                  for sid, cid, cash in suspects],
                "shortage_entry": shortage}

    # A driver's cash shortfall is a loss to the business the moment it is found, so it is booked as an
    # expense (category cash_shortage) dated the day of the deposit. ACCOUNTING JUDGMENT CALL (default,
    # owner may revisit): expense now, not a receivable against the driver pending recovery. It keeps
    # profit honest immediately instead of hiding the loss behind an investigation that may never
    # formally close, and nothing is lost by it:
    #   * counting error / money found  -> the owner reverses the entry (reverse_expense, audited);
    #   * the driver makes it good      -> he hands the cash in as another deposit on the same plan,
    #                                      and that deposit books a negative "recovery" row here;
    #   * a paid-by-instalment hand-in  -> the same mechanism: the first part books the gap, the rest
    #                                      recovers it, and the plan nets to zero once square.
    # Not built (possible future enhancement): a per-driver shortfall record for trust/performance.
    #
    # method is "adjustment", not "cash": the cash never reached the drawer, so it must not appear as a
    # cash outflow (the hand-in is already the true cash-in). The cashbook shows it as a memo line.
    #
    # Each deposit reconciles the plan's booked shortage to the current gap. "Booked" counts the
    # system's own rows (shortages and recoveries) but not owner reversals, so a later deposit never
    # re-posts a loss the owner has already cancelled; a recovery is capped at what is still on the
    # books net of reversals, so a cancelled shortage is never "recovered" into a gain.
    def _book_shortage(self, c, plan_id: str, plate: str, gap_p: int, deposit_id: str, suspects: list, counted_by: str, actor: str) -> dict | None:
        own = "SELECT expense_id FROM expenses WHERE category=? AND reversal_of IS NULL AND substr(note, 1, ?)=?"
        key = (SHORTAGE_CATEGORY, len(plan_id) + 1, plan_id + " ")
        booked_p = int(self._one(f"SELECT COALESCE(SUM(amount), 0) s FROM expenses WHERE expense_id IN ({own})", key)["s"])
        reversed_p = int(self._one(f"SELECT COALESCE(SUM(amount), 0) s FROM expenses WHERE reversal_of IN ({own})", key)["s"])
        delta_p = gap_p - booked_p
        if delta_p < 0: delta_p = -max(0, min(-delta_p, booked_p + reversed_p))     # recover at most what is still on the books
        if delta_p == 0: return None
        if delta_p > 0:
            look = "; ".join(f"{sid} {self.get_customer(cid).name} Rs {to_rupees(cash):,.0f}" for sid, cid, cash in suspects)
            note = f"{plan_id} ({plate}) cash short on {deposit_id}" + (f"; check {look}" if look else "")
        else:
            note = f"{plan_id} ({plate}) shortage made good by {deposit_id}"
        e = Expense(new_id("EXP"), SHORTAGE_CATEGORY, to_rupees(delta_p), note[:120], "adjustment", counted_by or self._current_user(), today_iso())
        c.execute(f"INSERT INTO expenses ({_EXP_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                  (e.expense_id, e.category, delta_p, e.note, e.method, e.paid_by, e.expense_date, e.created_at, None))
        self.audit(actor, "cash_shortage" if delta_p > 0 else "cash_shortage_recovered", "expense", e.expense_id,
                   {"plan": plan_id, "deposit": deposit_id, "amount": e.amount, "gap": to_rupees(gap_p), "suspect_stops": [s[0] for s in suspects]})
        return {"expense_id": e.expense_id, "amount": e.amount, "kind": "shortage" if delta_p > 0 else "recovery", "note": e.note}

    def shortfalls(self, day: str) -> list[Expense]:
        """Driver cash shortfalls booked (and recovered / reversed) on a business day."""
        return [self._expense(r) for r in self._all(f"SELECT {_EXP_COLS} FROM expenses WHERE category=? AND expense_date=? ORDER BY rowid", (SHORTAGE_CATEGORY, day))]

    def _deposited_paisa(self, plan_id: str) -> int:
        return int(self._one("SELECT COALESCE(SUM(amount_counted), 0) s FROM deposits WHERE plan_id=?", (plan_id,))["s"])

    @staticmethod
    def _deposit(r) -> CashDeposit:
        d = dict(r); d["amount_counted"] = to_rupees(int(d["amount_counted"] or 0)); return CashDeposit(**d)

    def deposits(self, plan_id: str) -> list[CashDeposit]:
        return [self._deposit(r) for r in self._all("SELECT * FROM deposits WHERE plan_id=?", (plan_id,))]

    # ------------------------------------------------------------ khata
    @staticmethod
    def _ledger(r) -> LedgerEntry:
        d = dict(r); d.setdefault("method", ""); d.setdefault("received_by", "")
        d["amount"] = to_rupees(int(d["amount"] or 0)); return LedgerEntry(**d)

    def ledger_for(self, customer_id: str) -> list[LedgerEntry]:
        return [self._ledger(r) for r in self._all(f"SELECT {_LEDGER_COLS} FROM ledger WHERE customer_id=? ORDER BY created_at, rowid", (customer_id,))]

    def get_ledger_entry(self, entry_id: str) -> LedgerEntry:
        r = self._one(f"SELECT {_LEDGER_COLS} FROM ledger WHERE entry_id=?", (entry_id,))
        if not r: raise NotFoundError(f"no such entry: {entry_id}")
        return self._ledger(r)

    def ledger_between(self, start: str, end: str, kind: str | None = None) -> list[LedgerEntry]:
        """Entries whose Pakistan business day falls in [start, end] (inclusive, YYYY-MM-DD)."""
        q, a = f"SELECT {_LEDGER_COLS} FROM ledger WHERE {sql_business_date('created_at')} BETWEEN ? AND ?", [start, end]
        if kind: q += " AND kind=?"; a.append(kind)
        return [self._ledger(r) for r in self._all(q + " ORDER BY created_at, rowid", tuple(a))]

    def receivables_paisa(self) -> int:
        """Everything customers owe, net: the sum of the whole khata."""
        return int(self._one("SELECT COALESCE(SUM(amount), 0) s FROM ledger")["s"])

    def add_ledger(self, customer_id: str, kind: str, amount: float, ref: str, due_date: str | None, actor: str,
                   approved_by: str | None = None, method: str = "", received_by: str = "") -> LedgerEntry:
        """Post one khata entry. Invoices are positive, payments and credit notes negative. Invoices,
        receipts, credit notes and opening balances take the next gapless number in their series,
        in the same transaction as the insert."""
        amount_p = to_paisa(amount)
        if amount_p == 0: raise ValueError("an entry needs a non-zero amount")
        if kind == "invoice" and amount_p < 0: raise ValueError("an invoice must be positive")
        if kind in ("payment", "credit_note") and amount_p > 0: raise ValueError(f"a {kind.replace('_', ' ')} must be negative on the khata")
        series = {"invoice": "opening" if method == "adjustment" else "invoice", "payment": "receipt", "credit_note": "credit_note"}.get(kind)
        with immediate_tx(self) as c:
            self.get_customer(customer_id)
            created_at = now_iso()
            entry_id = next_doc_no(self, c, series, created_at) if series else new_id(kind[:3].upper())
            c.execute(f"INSERT INTO ledger ({_LEDGER_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (entry_id, customer_id, kind, amount_p, ref, due_date, created_at, method, received_by, entry_id if series else None, None))
            self.audit(actor, f"ledger_{kind}", "ledger", entry_id, {"customer": customer_id, "amount": to_rupees(amount_p), "method": method}, approved_by)
        return self.get_ledger_entry(entry_id)

    def record_payment(self, customer_id: str, amount: float, method: str, ref: str, actor: str, approved_by: str | None = None, received_by: str = "") -> LedgerEntry:
        """A payment received away from the delivery: at the counter, by bank transfer, JazzCash, Easypaisa or cheque."""
        amount_p = to_paisa(amount)
        if amount_p <= 0: raise ValueError("payment must be positive")
        if method not in PAYMENT_METHODS: raise ValueError(f"method must be one of {', '.join(PAYMENT_METHODS)}")
        return self.add_ledger(customer_id, "payment", to_rupees(-amount_p), ref or method, None, actor, approved_by, method, received_by or self._current_user())

    def opening_balance(self, customer_id: str, amount: float, actor: str, approved_by: str | None = None) -> LedgerEntry:
        """Bring a customer's paper khata in: one invoice-like entry dated today (Pakistan business day)."""
        due = today_iso()
        return self.add_ledger(customer_id, "invoice", to_rupees(abs(to_paisa(amount))), "opening balance", due, actor, approved_by, "adjustment")

    def reversal_of_ledger(self, entry_id: str) -> str | None:
        r = self._one("SELECT entry_id FROM ledger WHERE reversal_of=?", (entry_id,))
        return r["entry_id"] if r else None

    def reverse_ledger_entry(self, entry_id: str, reason: str, actor: str, approved_by: str) -> LedgerEntry:
        """Cancel a khata entry (a bounced cheque, a payment keyed to the wrong customer, a wrong
        invoice) with its exact negation, numbered in the REV series. The original is untouched."""
        reason, approved_by = _reason(reason), _approver(approved_by)
        with immediate_tx(self) as c:
            r = self._one(f"SELECT {_LEDGER_COLS} FROM ledger WHERE entry_id=?", (entry_id,))
            if not r: raise NotFoundError(f"no such entry: {entry_id}")
            if r["reversal_of"]: raise StateError(f"{entry_id} is itself a reversal (of {r['reversal_of']}); post a fresh entry instead")
            done = self.reversal_of_ledger(entry_id)
            if done: raise StateError(f"{entry_id} was already reversed by {done}")
            created_at = now_iso()
            rid = next_doc_no(self, c, "reversal", created_at)
            c.execute(f"INSERT INTO ledger ({_LEDGER_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, r["customer_id"], r["kind"], -int(r["amount"]), r["ref"], None, created_at, r["method"] or "", r["received_by"] or "", rid, entry_id))
            payload = {"reverses": entry_id, "kind": r["kind"], "amount": to_rupees(-int(r["amount"])), "customer": r["customer_id"], "reason": reason}
            self.audit(actor, "reverse_ledger_entry", "ledger", entry_id, payload | {"reversal": rid}, approved_by)
            self.audit(actor, "ledger_reversal", "ledger", rid, payload, approved_by)
            self._queue_reversal_correction(r, rid)
        return self.get_ledger_entry(rid)

    def _customer_messages_for(self, r) -> list[str]:
        """Outbox refs of the customer messages that stated this entry (and so the balance after it). A receipt
        or invoice sent from chat is queued with ref = the entry id; the message at the door after a delivery is
        queued with ref = the stop's invoice and states the cash taken, so a driver's receipt is covered by it."""
        refs = [r["entry_id"]]
        if r["kind"] == "payment" and (r["received_by"] or "") == "driver" and (r["ref"] or "").startswith("STP-"):
            done = self._one("SELECT result FROM stop_closes WHERE stop_id=?", (r["ref"],))
            inv = json.loads(done["result"]).get("invoice_id") if done else None
            if inv: refs.append(inv)
        marks = ",".join("?" * len(refs))
        return [m["ref"] for m in self._all(f"SELECT ref FROM outbox WHERE ref IN ({marks}) AND status IN ('queued', 'sent')", tuple(refs))]

    def _queue_reversal_correction(self, r, rid: str) -> None:
        """The customer was told this entry's amount and the balance after it (a WhatsApp receipt, an invoice at the
        door). Reversing it makes that balance wrong, so a correction goes out the same way, templated: what was
        cancelled, the reversal's number, and the balance now. Never the reason (free text is not sent to customers).
        Nothing is sent for an entry the customer was never told about, or whose message failed.

        This is the ONE place a reversal's customer message is queued (exactly one per reversal, whichever path
        reversed it: the chat tool, the web form or a script). Callers must not queue their own on top of it."""
        if not self._customer_messages_for(r): return
        cust = self.get_customer(r["customer_id"])
        if not (cust.phone or "").strip(): return
        what = {"payment": "Receipt", "invoice": "Invoice", "credit_note": "Credit note"}.get(r["kind"], "Entry")
        method = f", {r['method']}" if r["kind"] == "payment" and r["method"] else ""
        why = " -- cheque returned unpaid" if r["kind"] == "payment" and (r["method"] or "") == "cheque" else ""
        self.queue_message("whatsapp", cust.phone,
                           f"{self.business_name}: Correction. {what} {r['entry_id']} (Rs {to_rupees(abs(int(r['amount']))):,.0f}{method}) has been cancelled "
                           f"({rid}){why}. Balance now Rs {self.outstanding(r['customer_id']):,.0f}.", rid)

    # ------------------------------------------------------------ expenses
    @staticmethod
    def _expense(r) -> Expense:
        d = dict(r); d["amount"] = to_rupees(int(d["amount"] or 0)); return Expense(**d)

    def record_expense(self, category: str, amount: float, note: str, method: str, paid_by: str, actor: str, approved_by: str | None = None, expense_date: str | None = None) -> Expense:
        amount_p = to_paisa(amount)
        if amount_p <= 0: raise ValueError("expense must be positive")
        category = category if category in EXPENSE_CATEGORIES else "misc"
        e = Expense(new_id("EXP"), category, to_rupees(amount_p), note[:120], method or "cash", paid_by or self._current_user(), expense_date or today_iso())
        with immediate_tx(self) as c:
            c.execute(f"INSERT INTO expenses ({_EXP_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                      (e.expense_id, e.category, amount_p, e.note, e.method, e.paid_by, e.expense_date, e.created_at, None))
            self.audit(actor, "record_expense", "expense", e.expense_id, {"category": category, "amount": e.amount}, approved_by)
        return e

    def get_expense(self, expense_id: str) -> Expense:
        r = self._one(f"SELECT {_EXP_COLS} FROM expenses WHERE expense_id=?", (expense_id,))
        if not r: raise NotFoundError(f"no such expense: {expense_id}")
        return self._expense(r)

    def expenses_between(self, start: str, end: str) -> list[Expense]:
        return [self._expense(r) for r in self._all(f"SELECT {_EXP_COLS} FROM expenses WHERE expense_date BETWEEN ? AND ? ORDER BY expense_date, rowid", (start, end))]

    def expenses_paisa(self, start: str, end: str) -> int:
        return int(self._one("SELECT COALESCE(SUM(amount), 0) s FROM expenses WHERE expense_date BETWEEN ? AND ?", (start, end))["s"])

    def reverse_expense(self, expense_id: str, reason: str, actor: str, approved_by: str) -> Expense:
        """Cancel a mis-keyed expense: a negative expense dated today, same category and method."""
        reason, approved_by = _reason(reason), _approver(approved_by)
        with immediate_tx(self) as c:
            r = self._one(f"SELECT {_EXP_COLS} FROM expenses WHERE expense_id=?", (expense_id,))
            if not r: raise NotFoundError(f"no such expense: {expense_id}")
            if r["reversal_of"]: raise StateError(f"{expense_id} is itself a reversal (of {r['reversal_of']})")
            done = self._one("SELECT expense_id FROM expenses WHERE reversal_of=?", (expense_id,))
            if done: raise StateError(f"{expense_id} was already reversed by {done['expense_id']}")
            e = Expense(new_id("EXP"), r["category"], to_rupees(-int(r["amount"])), f"reversal of {expense_id}: {reason}"[:120], r["method"],
                        self._current_user() or r["paid_by"], today_iso(), reversal_of=expense_id)
            c.execute(f"INSERT INTO expenses ({_EXP_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                      (e.expense_id, e.category, -int(r["amount"]), e.note, e.method, e.paid_by, e.expense_date, e.created_at, expense_id))
            self.audit(actor, "reverse_expense", "expense", expense_id, {"reversal": e.expense_id, "amount": e.amount, "reason": reason}, approved_by)
        return e

    # ------------------------------------------------------------ purchases + suppliers
    @staticmethod
    def _purchase(r) -> Purchase:
        d = dict(r)
        d["items"] = [{"sku": i["sku"], "qty": int(i["qty"]), "unit_cost": to_rupees(int(i["unit_cost_paisa"]))} for i in json.loads(d["items"] or "[]")]
        d["total"] = to_rupees(int(d["total"] or 0)); d["paid_amount"] = to_rupees(int(d["paid_amount"] or 0))
        return Purchase(**d)

    def record_purchase(self, supplier_id: str, warehouse_id: str, items: list[dict], invoice_ref: str, paid_amount: float, actor: str, approved_by: str | None = None) -> Purchase:
        """Goods in from a supplier: stock up (at the bill's unit cost, into the moving average),
        the bill on the supplier's khata, and any payment made there and then. One transaction,
        numbered in the gapless PUR series."""
        if not items: raise ValueError("a purchase needs at least one line")
        paid_p = to_paisa(paid_amount or 0)
        with immediate_tx(self) as c:
            sup = self.get_supplier(supplier_id); self.get_warehouse(warehouse_id)
            lines, total_p = [], 0
            for it in items:
                p = self.get_product(str(it["sku"])); qty = int(it["qty"])
                if qty <= 0: raise ValueError(f"bad quantity for {p.sku}")
                cost_p = to_paisa(it.get("unit_cost") or 0) or self._product_paisa(p.sku)[1]
                if cost_p < 0: raise ValueError(f"bad cost for {p.sku}")
                lines.append({"sku": p.sku, "qty": qty, "unit_cost_paisa": cost_p}); total_p += qty * cost_p
            if paid_p < 0 or paid_p > total_p: raise ValueError("paid amount must be between 0 and the bill total")
            created_at = now_iso()
            pid = next_doc_no(self, c, "purchase", created_at)
            c.execute(f"INSERT INTO purchases ({_PUR_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (pid, supplier_id, warehouse_id, json.dumps(lines), total_p, invoice_ref, paid_p, created_at, pid, None))
            for ln in lines:
                self.move_stock(warehouse_id, ln["sku"], ln["qty"], "purchase", pid, value_paisa=ln["qty"] * ln["unit_cost_paisa"])
                if ln["unit_cost_paisa"] > 0:     # last purchase cost, kept as the product's reference cost
                    c.execute("UPDATE products SET cost_price=? WHERE sku=?", (ln["unit_cost_paisa"], ln["sku"]))
            c.execute(f"INSERT INTO supplier_ledger ({_SUP_COLS}) VALUES (?,?,?,?,?,?,?,?)", (new_id("BIL"), supplier_id, "bill", total_p, pid, "", created_at, None))
            if paid_p > 0:
                c.execute(f"INSERT INTO supplier_ledger ({_SUP_COLS}) VALUES (?,?,?,?,?,?,?,?)", (new_id("SPY"), supplier_id, "payment", -paid_p, pid, "cash", created_at, None))
            self.audit(actor, "record_purchase", "purchase", pid, {"supplier": sup.name, "total": to_rupees(total_p), "paid": to_rupees(paid_p), "warehouse": warehouse_id}, approved_by)
        return self.get_purchase(pid)

    def list_purchases(self, limit: int = 50, supplier_id: str | None = None) -> list[Purchase]:
        q, a = (f"SELECT {_PUR_COLS} FROM purchases WHERE supplier_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?", (supplier_id, limit)) if supplier_id \
            else (f"SELECT {_PUR_COLS} FROM purchases ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
        return [self._purchase(r) for r in self._all(q, a)]

    def get_purchase(self, purchase_id: str) -> Purchase:
        r = self._one(f"SELECT {_PUR_COLS} FROM purchases WHERE purchase_id=?", (purchase_id,))
        if not r: raise NotFoundError(f"no such purchase: {purchase_id}")
        return self._purchase(r)

    def reverse_purchase(self, purchase_id: str, reason: str, actor: str, approved_by: str) -> Purchase:
        """Undo a mis-keyed purchase: the goods leave the godown again (at the cost they came in at),
        the bill and any payment made with it are reversed on the supplier's khata, and a purchase
        return (PRN series) records it. Refused, with nothing changed, if the goods are no longer
        all there (stock may never go below zero)."""
        reason, approved_by = _reason(reason), _approver(approved_by)
        with immediate_tx(self) as c:
            r = self._one(f"SELECT {_PUR_COLS} FROM purchases WHERE purchase_id=?", (purchase_id,))
            if not r: raise NotFoundError(f"no such purchase: {purchase_id}")
            if r["reversal_of"]: raise StateError(f"{purchase_id} is itself a purchase return (of {r['reversal_of']})")
            done = self._one("SELECT purchase_id FROM purchases WHERE reversal_of=?", (purchase_id,))
            if done: raise StateError(f"{purchase_id} was already reversed by {done['purchase_id']}")
            lines = json.loads(r["items"])
            created_at = now_iso()
            rid = next_doc_no(self, c, "purchase_return", created_at)
            back = [{"sku": ln["sku"], "qty": -int(ln["qty"]), "unit_cost_paisa": int(ln["unit_cost_paisa"])} for ln in lines]
            c.execute(f"INSERT INTO purchases ({_PUR_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (rid, r["supplier_id"], r["warehouse_id"], json.dumps(back), -int(r["total"]), r["invoice_ref"], -int(r["paid_amount"]), created_at, rid, purchase_id))
            for ln in lines:
                self.move_stock(r["warehouse_id"], ln["sku"], -int(ln["qty"]), "purchase_reversal", rid, value_paisa=-int(ln["qty"]) * int(ln["unit_cost_paisa"]))
            for e in self._all(f"SELECT {_SUP_COLS} FROM supplier_ledger WHERE ref=? AND reversal_of IS NULL", (purchase_id,)):
                if self._one("SELECT 1 FROM supplier_ledger WHERE reversal_of=?", (e["entry_id"],)): continue
                c.execute(f"INSERT INTO supplier_ledger ({_SUP_COLS}) VALUES (?,?,?,?,?,?,?,?)",
                          (new_id("SRV"), e["supplier_id"], e["kind"], -int(e["amount"]), rid, e["method"] or "", created_at, e["entry_id"]))
            self.audit(actor, "reverse_purchase", "purchase", purchase_id, {"reversal": rid, "total": to_rupees(-int(r["total"])), "reason": reason}, approved_by)
        return self.get_purchase(rid)

    def pay_supplier(self, supplier_id: str, amount: float, method: str, ref: str, actor: str, approved_by: str | None = None) -> SupplierLedgerEntry:
        amount_p = to_paisa(amount)
        if amount_p <= 0: raise ValueError("payment must be positive")
        if method not in PAYMENT_METHODS: raise ValueError(f"method must be one of {', '.join(PAYMENT_METHODS)}")
        e = SupplierLedgerEntry(new_id("SPY"), supplier_id, "payment", to_rupees(-amount_p), ref or method, method)
        with immediate_tx(self) as c:
            self.get_supplier(supplier_id)
            c.execute(f"INSERT INTO supplier_ledger ({_SUP_COLS}) VALUES (?,?,?,?,?,?,?,?)", (e.entry_id, supplier_id, "payment", -amount_p, e.ref, method, e.created_at, None))
            self.audit(actor, "pay_supplier", "supplier", supplier_id, {"amount": to_rupees(amount_p), "method": method, "ref": ref}, approved_by)
        return e

    def supplier_opening_balance(self, supplier_id: str, amount: float, actor: str, approved_by: str | None = None) -> SupplierLedgerEntry:
        """What the business already owed a supplier when it started using Munshi: one bill, dated now."""
        amount_p = to_paisa(amount)
        if amount_p <= 0: raise ValueError("an opening balance must be positive")
        e = SupplierLedgerEntry(new_id("BIL"), supplier_id, "bill", to_rupees(amount_p), "opening balance", "")
        with immediate_tx(self) as c:
            self.get_supplier(supplier_id)
            c.execute(f"INSERT INTO supplier_ledger ({_SUP_COLS}) VALUES (?,?,?,?,?,?,?,?)", (e.entry_id, supplier_id, "bill", amount_p, e.ref, "", e.created_at, None))
            self.audit(actor, "supplier_opening_balance", "supplier", supplier_id, {"amount": e.amount}, approved_by)
        return e

    def reverse_supplier_entry(self, entry_id: str, reason: str, actor: str, approved_by: str) -> SupplierLedgerEntry:
        """Cancel a supplier-khata entry (a payment that bounced or went to the wrong supplier, a wrong
        opening balance). A bill that came with a purchase must be undone with reverse_purchase, so the
        goods go back too."""
        reason, approved_by = _reason(reason), _approver(approved_by)
        with immediate_tx(self) as c:
            r = self._one(f"SELECT {_SUP_COLS} FROM supplier_ledger WHERE entry_id=?", (entry_id,))
            if not r: raise NotFoundError(f"no such supplier entry: {entry_id}")
            if r["reversal_of"]: raise StateError(f"{entry_id} is itself a reversal (of {r['reversal_of']})")
            if r["kind"] == "bill" and self._one("SELECT 1 FROM purchases WHERE purchase_id=?", (r["ref"],)):
                raise StateError(f"bill {entry_id} belongs to purchase {r['ref']}; reverse the purchase so the goods go back too")
            done = self._one("SELECT entry_id FROM supplier_ledger WHERE reversal_of=?", (entry_id,))
            if done: raise StateError(f"{entry_id} was already reversed by {done['entry_id']}")
            e = SupplierLedgerEntry(new_id("SRV"), r["supplier_id"], r["kind"], to_rupees(-int(r["amount"])), r["ref"], r["method"] or "", reversal_of=entry_id)
            c.execute(f"INSERT INTO supplier_ledger ({_SUP_COLS}) VALUES (?,?,?,?,?,?,?,?)",
                      (e.entry_id, e.supplier_id, e.kind, -int(r["amount"]), e.ref, e.method, e.created_at, entry_id))
            self.audit(actor, "reverse_supplier_entry", "supplier", r["supplier_id"], {"reverses": entry_id, "reversal": e.entry_id, "amount": e.amount, "reason": reason}, approved_by)
        return e

    @staticmethod
    def _supplier_entry(r) -> SupplierLedgerEntry:
        d = dict(r); d["amount"] = to_rupees(int(d["amount"] or 0)); return SupplierLedgerEntry(**d)

    def supplier_ledger(self, supplier_id: str) -> list[SupplierLedgerEntry]:
        return [self._supplier_entry(r) for r in self._all(f"SELECT {_SUP_COLS} FROM supplier_ledger WHERE supplier_id=? ORDER BY created_at, rowid", (supplier_id,))]

    def supplier_balance_paisa(self, supplier_id: str) -> int:
        return int(self._one("SELECT COALESCE(SUM(amount),0) s FROM supplier_ledger WHERE supplier_id=?", (supplier_id,))["s"])

    def supplier_balance(self, supplier_id: str) -> float:
        return to_rupees(self.supplier_balance_paisa(supplier_id))

    def payables_paisa(self) -> int:
        """Everything the business owes suppliers, net: the sum of the whole supplier khata."""
        return int(self._one("SELECT COALESCE(SUM(amount), 0) s FROM supplier_ledger")["s"])

    def payables(self) -> list[dict]:
        out = []
        for s in self.list_suppliers():
            bal = self.supplier_balance_paisa(s.supplier_id)
            if bal > 0: out.append({"supplier_id": s.supplier_id, "name": s.name, "phone": s.phone, "balance": to_rupees(bal)})
        return sorted(out, key=lambda r: -r["balance"])

    # ------------------------------------------------------------ cashbook
    def cashbook(self, day: str | None = None) -> dict:
        """Everything that touched physical cash on a Pakistan business day, in and out.
        Reversals appear on the day they were posted, with a negative amount in the section of
        the entry they reverse (a bounced cash receipt is a negative cash-in)."""
        day = day or today_iso()
        ins, outs = [], []
        ins_p = outs_p = handins_p = 0
        for r in self._all(f"SELECT {_LEDGER_COLS} FROM ledger WHERE kind='payment' AND COALESCE(NULLIF(method, ''), 'cash')='cash' AND COALESCE(received_by, '')!='driver' AND "
                           + sql_business_date("created_at") + "=? ORDER BY created_at, rowid", (day,)):     # driver cash arrives as a hand-in
            amt = -int(r["amount"]); ins_p += amt
            ins.append({"kind": "customer payment" + (" reversal" if r["reversal_of"] else ""), "who": self.get_customer(r["customer_id"]).name,
                        "amount": to_rupees(amt), "ref": r["entry_id"], "by": r["received_by"], "at": r["created_at"]})
        for r in self._all(f"SELECT {_EXP_COLS} FROM expenses WHERE method='cash' AND expense_date=? ORDER BY rowid", (day,)):
            amt = int(r["amount"]); outs_p += amt
            outs.append({"kind": f"expense · {r['category']}" + (" reversal" if r["reversal_of"] else ""), "who": r["note"] or r["category"],
                         "amount": to_rupees(amt), "ref": r["expense_id"], "by": r["paid_by"], "at": r["created_at"]})
        for r in self._all(f"SELECT {_SUP_COLS} FROM supplier_ledger WHERE kind='payment' AND method='cash' AND " + sql_business_date("created_at") + "=? ORDER BY created_at, rowid", (day,)):
            amt = -int(r["amount"]); outs_p += amt
            outs.append({"kind": "supplier payment" + (" reversal" if r["reversal_of"] else ""), "who": self.get_supplier(r["supplier_id"]).name,
                         "amount": to_rupees(amt), "ref": r["entry_id"], "by": "", "at": r["created_at"]})
        deposits = []
        for r in self._all("SELECT * FROM deposits WHERE " + sql_business_date("deposited_at") + "=? ORDER BY deposited_at, rowid", (day,)):
            amt = int(r["amount_counted"]); handins_p += amt
            deposits.append({"kind": "driver hand-in", "who": self.get_vehicle(self.get_plan(r["plan_id"]).vehicle_id).plate, "amount": to_rupees(amt),
                             "ref": r["deposit_id"], "by": r["counted_by"], "at": r["deposited_at"]})
        # Memo, not part of `net`: driver cash that should have arrived and didn't (booked as a
        # cash_shortage expense by record_deposit; recoveries and reversals net it down). The drawer never
        # held it, so it is neither a hand-in nor a cash-out -- but the day's book must show it.
        short = [{"kind": "driver shortfall" + (" reversal" if x.reversal_of else " recovered" if x.amount < 0 else ""), "who": x.note,
                  "amount": x.amount, "ref": x.expense_id, "by": x.paid_by, "at": x.created_at} for x in self.shortfalls(day)]
        return {"date": day, "cash_in": ins, "driver_handins": deposits, "cash_out": outs,
                "total_in": to_rupees(ins_p), "total_handins": to_rupees(handins_p), "total_out": to_rupees(outs_p),
                "net": to_rupees(ins_p + handins_p - outs_p),
                "shortfalls": short, "total_shortfall": to_rupees(sum(to_paisa(s["amount"]) for s in short))}

    @staticmethod
    def _today() -> date:
        return business_today()
