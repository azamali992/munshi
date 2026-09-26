"""Every tool as a plain method on MunshiTools. Framework-free: the LangChain
wrappers, the HTTP API and the tests all call these same methods. Each
method knows which agent it belongs to (the `actor` it writes to the audit)."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import date, timedelta

from munshi.domain.models import business_today, to_business_date, today_iso
from munshi.domain.repository import MunshiRepository, StateError
from munshi.llm.guardrails import check_scope, viewer  # the chatting role's read scope (a no-op outside a chat turn)

# The signed-in user's id for the current chat turn (set by the platform from the session, never from the message or the
# model). my_payslips reads it: whose slips a person sees is decided by who is signed in, not by anything typed.
_USER_ID: ContextVar[str] = ContextVar("munshi_user_id", default="")

TANKHWA, ACCOUNTS = "tankhwa_munshi", "accounts_munshi"      # domain/accounts.MONEY_AGENT_ACTORS
# pay fields a non-owner never receives from an employee record (owner decision 2); Stream A omits them already
# (include_pay=False) -- this is the belt to that brace
PAY_FIELDS = frozenset({"basic", "daily_rate", "pay_basis", "components", "gross", "net", "net_pay", "pay_method", "payee_ref", "salary",
                        "commission", "advance", "advances", "ytd_taxable", "ytd_tax", "cnic"})


# The proofs of the card whose approved action is running now (set by platform.resolve around the resumed call): the money
# write links them to the entry it creates INSIDE its own transaction (Stream E's contract), so the entry and its proof
# commit -- or roll back -- together. (approval id, attachment ids); None outside an approved resume.
_APPROVED_PROOFS: ContextVar[tuple[str, tuple[str, ...]] | None] = ContextVar("munshi_approved_proofs", default=None)


@contextmanager
def approved_proofs(approval_id: str, att_ids):
    token = _APPROVED_PROOFS.set((str(approval_id), tuple(str(a) for a in att_ids or ())) if att_ids else None)
    try:
        yield
    finally:
        _APPROVED_PROOFS.reset(token)


def _ids_in(out, keys: tuple[str, ...]) -> list[str]:
    """The ids (under these keys) a write's result names -- one entry, or each payment of a batch."""
    found, stack = [], [asdict(out) if hasattr(out, "__dataclass_fields__") else out]
    while stack:
        x = stack.pop(0)
        if isinstance(x, dict):
            found += [str(x[k]) for k in keys if x.get(k)]
            stack += [v for v in x.values() if isinstance(v, (dict, list))]
        elif isinstance(x, list):
            stack += x
    return list(dict.fromkeys(found))


@contextmanager
def acting_user_id(user_id: str):
    token = _USER_ID.set(str(user_id or ""))
    try:
        yield
    finally:
        _USER_ID.reset(token)


def current_user_id() -> str:
    return _USER_ID.get()


def this_period() -> str:
    return business_today().strftime("%Y-%m")


def _owner_view() -> bool:
    """Is the chatting role the owner? (Outside a chat turn -- tests, scripts -- nobody is chatting: the owner's view.)"""
    v = viewer()
    return v is None or v == "owner"


def _no_pay(x):
    """An employee record (or a list / result holding them) without its pay fields."""
    if isinstance(x, dict):
        out = {k: _no_pay(v) for k, v in x.items() if k not in PAY_FIELDS}
        if isinstance(out.get("table"), dict):
            t = out["table"]
            cols = [c for c in t.get("columns") or [] if c.get("key") not in PAY_FIELDS]
            keep = {c["key"] for c in cols} | {"_em"}
            out["table"] = t | {"columns": cols, "rows": [{k: v for k, v in r.items() if k in keep} for r in t.get("rows") or []],
                                "totals": None}
        return out
    if isinstance(x, list):
        return [_no_pay(v) for v in x]
    return x

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

    def _proved(self, entity: str, actor: str, write, *keys: str):
        """Run a money write; when it is an approved card's and the card carries proofs, link each proof to the entry the write
        created, in the SAME transaction (a proof that can't be linked -- Stream E's AttachmentError, a ValueError -- rolls the
        write back and is reported like any refusal)."""
        p = _APPROVED_PROOFS.get()
        if not p:
            return write()
        approval_id, att_ids = p
        with self.repo._tx():
            out = write()
            for eid in _ids_in(out, keys):
                for att in att_ids:
                    self.repo.link_attachment(att, entity, eid, actor, approval_id=approval_id)
            return out

    # ---------- lookups ----------
    # A lookup never picks one of several matches: 'Malik' with Malik Agro and Malik Seeds on the books comes back
    # as ambiguous with both candidates, so the caller asks which. Resolution is llm/resolve.py's confidence rule
    # (the same code that checks a model's arguments), then an exact ID / phone, then a name substring that fits
    # exactly one record.
    @staticmethod
    def _lookup(text: str, resolution, records: list, rid, name) -> tuple[str, object | None, list]:
        if resolution.ok:
            return "ok", next((r for r in records if rid(r) == resolution.id), None), []
        if resolution.status == "ambiguous":
            return "ambiguous", None, [{"id": c.id, "name": c.name} for c in resolution.candidates]
        t = text.lower().strip()
        if not t:
            return "none", None, []
        exact = [r for r in records if t in (rid(r).lower(), str(getattr(r, "phone", "") or "").lower())]
        if len(exact) == 1:
            return "ok", exact[0], []
        subs = [r for r in records if t in name(r).lower()]
        if len(subs) == 1:
            return "ok", subs[0], []
        if len(subs) > 1:
            return "ambiguous", None, [{"id": rid(r), "name": name(r)} for r in subs[:3]]
        return "none", None, []

    def find_customer(self, text: str) -> dict:
        from munshi.llm.parse import customer_resolution
        records = self.repo.list_customers()
        status, c, cands = self._lookup(text, customer_resolution(text, self.repo), records, lambda r: r.customer_id, lambda r: r.name)
        if status == "ambiguous":
            return {"found": False, "ambiguous": True, "query": text, "candidates": [{"customer_id": x["id"], "name": x["name"]} for x in cands],
                    "note": "More than one customer matches: ask the user which one, naming these. Do not pick one."}
        if c is None: return {"found": False, "query": text}
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

    def get_stock(self, sku: str = "") -> list[dict]:
        """One product's stock at every godown; with no SKU, every active product's (read-only)."""
        if sku:
            self.repo.get_product(sku)
            return [asdict(s) | {"available": s.available} for s in self.repo.stock_by_sku(sku)]
        active = {p.sku for p in self.repo.list_products()}
        return [asdict(s) | {"available": s.available} for s in self.repo.list_stock() if s.sku in active]

    def list_orders(self, status: str = "", sku: str = "", customer_id: str = "", days: int = 0) -> list[dict]:
        """Recent orders, newest first; optionally only one status, one product, one customer, or the last N days
        (days=1: today). Read-only."""
        days = max(0, int(days or 0))
        # "today" / "last N days" are Pakistan business days, filtered in SQL on the business date of created_at
        # (never on the UTC date prefix, which is a day behind from 00:00 to 05:00 PKT)
        since = (date.fromisoformat(today_iso()) - timedelta(days=days - 1)).isoformat() if days else None
        rows = self.repo.list_orders(status or None, customer_id=customer_id or None, limit=200 if (sku or days) else 40, since=since)
        out = []
        for o in rows:
            if sku and not any(i.sku == sku for i in o.items):
                continue
            out.append(self._order(o))
        return out[:40]

    def get_order(self, order_id: str) -> dict:
        check_scope(self.repo, "get_order", order_id=order_id)          # a driver: only orders on today's runs
        return self._order(self.repo.get_order(order_id))

    def list_routes(self) -> list[dict]:
        return [asdict(r) for r in self.repo.list_routes()]

    def list_vehicles(self) -> list[dict]:
        return [asdict(v) for v in self.repo.list_vehicles()]

    @staticmethod
    def _stop_view(s) -> dict:
        """A stop as an agent may see it. The customer's delivery code is the customer's alone: an agent
        tool result is shown to the model and echoed to whoever is chatting, and the driver has these
        tools, so the code must never appear in it (the HTTP routes blank it for drivers the same way)."""
        return asdict(s) | {"otp": None}

    def get_plan(self, plan_id: str) -> dict:
        check_scope(self.repo, "get_plan", plan_id=plan_id)             # a driver: only today's active runs
        p = self.repo.get_plan(plan_id)
        return asdict(p) | {"stops": [self._stop_view(s) for s in self.repo.list_stops(plan_id)]}

    def list_stops(self, plan_id: str) -> list[dict]:
        """The stops with what a driver needs at the door: the customer's name and address, what was loaded for them and the
        bill for it (the amount to collect, at most). Read-only; the delivery code is never included."""
        check_scope(self.repo, "list_stops", plan_id=plan_id)
        out = []
        for s in self.repo.list_stops(plan_id):
            c = self.repo.get_customer(s.customer_id)
            try:
                o = self.repo.get_order(s.order_id)
                extra = {"order_total": o.total, "items": [{"sku": i.sku, "qty": i.qty} for i in o.items]}
            except Exception:
                extra = {}
            out.append(self._stop_view(s) | {"customer_name": c.name, "address": c.address, "phone": c.phone} | extra)
        return out

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

    @staticmethod
    def merged_lines(order, changes: list[dict]) -> list[dict]:
        """A draft's lines with `changes` applied: each {sku, qty} sets that product's quantity (a new product is added
        at the end, qty 0 removes it); lines not mentioned stay as they are. Existing lines keep the price they were
        drafted at; a new line takes the customer's price (the repository prices it). Pure: reads nothing."""
        want: dict[str, int] = {}
        for c in changes or []:
            want[str(c["sku"]).strip().upper()] = int(c["qty"])
        out, seen = [], set()
        for it in order.items:
            if it.sku in seen:
                continue
            seen.add(it.sku)
            qty = want.get(it.sku, sum(i.qty for i in order.items if i.sku == it.sku))
            if qty > 0:
                out.append({"sku": it.sku, "qty": qty, "unit_price": it.unit_price})
        for sku, qty in want.items():
            if sku not in seen and qty > 0:
                out.append({"sku": sku, "qty": qty})
        return out

    def update_order(self, order_id: str, items: list[dict], approved_by: str = "clerk") -> dict:
        """Edit a DRAFT order: `items` are only the lines that change (see merged_lines). Anything past draft is
        refused by the repository ("only drafts can be edited") -- that is a cancel and a new order."""
        o = self.repo.get_order(order_id)
        if o.status != "draft":
            raise StateError(f"order {order_id} is {o.status}; only drafts can be edited -- cancel it and book a new order instead")
        lines = self.merged_lines(o, items)
        if not lines:
            raise ValueError("that would leave the order with no lines -- cancel it instead")
        return self._order(self.repo.update_order(order_id, lines, None, "order_munshi", approved_by))

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
        e = self._proved("ledger", "hisaab_munshi", lambda: self.repo.record_payment(customer_id, amount, method or "cash", ref, "hisaab_munshi", approved_by),
                         "entry_id")
        c = self.repo.get_customer(customer_id)
        self.repo.queue_message("whatsapp", c.phone, f"{self.repo.business_name}: Rs {abs(e.amount):,.0f} received ({e.method}). Receipt {e.entry_id}. Balance now Rs {self.repo.outstanding(customer_id):,.0f}. Shukriya.", e.entry_id)
        return asdict(e) | {"customer_name": c.name, "outstanding": self.repo.outstanding(customer_id)}

    def record_expense(self, category: str, amount: float, note: str = "", method: str = "cash", approved_by: str = "clerk") -> dict:
        return asdict(self._proved("expense", "hisaab_munshi", lambda: self.repo.record_expense(category, amount, note, method, "", "hisaab_munshi", approved_by),
                                   "expense_id"))

    def credit_note(self, customer_id: str, amount: float, reason: str, approved_by: str = "owner") -> dict:
        e = self.repo.add_ledger(customer_id, "credit_note", -abs(float(amount)), reason, None, "hisaab_munshi", approved_by, "adjustment")
        return asdict(e)

    def reverse_ledger_entry(self, entry_id: str, reason: str, approved_by: str = "owner") -> dict:
        # a reversal changes money that already moved: HIGH_RISK, owner-approved, like credit_note
        # the customer's correction message (if they were ever told) is queued by the repository, exactly once
        e = self.repo.reverse_ledger_entry(entry_id, reason, "hisaab_munshi", approved_by)
        c = self.repo.get_customer(e.customer_id)
        return asdict(e) | {"customer_name": c.name, "outstanding": self.repo.outstanding(e.customer_id)}

    def reverse_expense(self, expense_id: str, reason: str, approved_by: str = "owner") -> dict:
        return asdict(self.repo.reverse_expense(expense_id, reason, "hisaab_munshi", approved_by))

    def cashbook(self, day: str = "") -> dict:
        return self.repo.cashbook(day or None, redact_payroll=not _owner_view())

    # ---------- khareed (purchases) ----------
    def find_supplier(self, text: str) -> dict:
        from munshi.llm.parse import supplier_resolution
        records = self.repo.list_suppliers()
        status, s, cands = self._lookup(text, supplier_resolution(text, self.repo), records, lambda r: r.supplier_id, lambda r: r.name)
        if status == "ambiguous":
            return {"found": False, "ambiguous": True, "query": text, "candidates": [{"supplier_id": x["id"], "name": x["name"]} for x in cands],
                    "note": "More than one supplier matches: ask the user which one, naming these. Do not pick one."}
        if s is None: return {"found": False, "query": text}
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
        p = self._proved("purchase", "khareed_munshi", lambda: self.repo.record_purchase(supplier_id, warehouse_id or self.repo.default_warehouse_id(), items, invoice_ref,
                                                                                          paid_amount, "khareed_munshi", approved_by), "purchase_id")
        return asdict(p) | {"supplier_name": self.repo.get_supplier(supplier_id).name, "balance": self.repo.supplier_balance(supplier_id)}

    def pay_supplier(self, supplier_id: str, amount: float, method: str = "cash", ref: str = "", approved_by: str = "owner") -> dict:
        e = self._proved("supplier_ledger", "khareed_munshi", lambda: self.repo.pay_supplier(supplier_id, amount, method or "cash", ref, "khareed_munshi", approved_by),
                         "entry_id")
        return asdict(e) | {"supplier_name": self.repo.get_supplier(supplier_id).name, "balance": self.repo.supplier_balance(supplier_id)}

    def reverse_purchase(self, purchase_id: str, reason: str, approved_by: str = "owner") -> dict:
        p = self.repo.reverse_purchase(purchase_id, reason, "khareed_munshi", approved_by)
        return asdict(p) | {"supplier_name": self.repo.get_supplier(p.supplier_id).name, "balance": self.repo.supplier_balance(p.supplier_id)}

    def reverse_supplier_entry(self, entry_id: str, reason: str, approved_by: str = "owner") -> dict:
        e = self.repo.reverse_supplier_entry(entry_id, reason, "khareed_munshi", approved_by)
        return asdict(e) | {"supplier_name": self.repo.get_supplier(e.supplier_id).name, "balance": self.repo.supplier_balance(e.supplier_id)}

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
        return self.repo.profit_summary(start, end, redact_payroll=not _owner_view())

    def collection_report(self, start: str = "", end: str = "") -> dict:
        """The period's collection figures plus the payments themselves (who paid, how much, how), newest last."""
        start, end = self._range(start, end)
        names = {c.customer_id: c.name for c in self.repo.list_customers(include_inactive=True)}
        pays = [{"entry_id": e.entry_id, "customer_id": e.customer_id, "name": names.get(e.customer_id, e.customer_id), "amount": -e.amount,
                 "method": e.method or "cash", "at": e.created_at, "day": to_business_date(e.created_at).isoformat(),   # `at` is UTC; `day` is the business day
                 "reversal_of": e.reversal_of} for e in self.repo.ledger_between(start, end, "payment")]
        return self.repo.collection_report(start, end) | {"payments": pays}

    def stock_ledger(self, sku: str, warehouse_id: str = "") -> dict:
        return self.repo.stock_ledger(sku, warehouse_id or None)

    def stock_valuation(self) -> dict:
        return self.repo.stock_valuation()

    def slow_stock(self, days: int = 30) -> list[dict]:
        return self.repo.slow_stock(days)

    def top_customers(self, days: int = 30) -> list[dict]:
        return self.repo.top_customers(days)

    # ---------- tankhwa (payroll; Stream A's repository) ----------
    # Reads that show pay are bound only for roles holding payroll:read (the owner) -- domain/accounts.TOOL_PERMISSION.
    def list_employees(self, status: str = "active") -> dict:
        own = _owner_view()
        out = self.repo.list_employees(status or "active", include_pay=own)
        return out if own else _no_pay(out)

    def find_employee(self, text: str) -> dict:
        out = self.repo.find_employee(text)
        return out if _owner_view() else _no_pay(out)

    def payroll_preview(self, period: str = "", employee_ids: list[str] | None = None) -> dict:
        return self.repo.preview_payroll(period or this_period(), list(employee_ids or []) or None)

    def latest_run_id(self) -> str:
        """The latest approved (not reversed) regular payroll run, or ''. A read."""
        r = self.repo._one("SELECT run_id FROM payroll_runs r WHERE kind='regular' AND NOT EXISTS (SELECT 1 FROM payroll_runs x WHERE x.reversal_of=r.run_id) "
                           "ORDER BY period DESC, generation DESC LIMIT 1")
        return r["run_id"] if r else ""

    def payroll_register(self, period: str = "", run_id: str = "") -> dict:
        if not (run_id or period):
            run_id = self.latest_run_id()
            if not run_id:
                raise StateError("no payroll has been approved yet -- say 'is mahine ki tankhwa bana do' to preview this month's")
        return self.repo.payroll_register(run_id or None, period or None)

    def payslip(self, employee_id: str, period: str = "") -> dict:
        if not period:                                  # the latest approved month's slip
            rid = self.latest_run_id()
            period = self.repo.payroll_register(rid)["run"]["period"] if rid else this_period()
        return self.repo.payslip(None, employee_id, period)

    def my_payslips(self) -> dict:
        """The signed-in person's OWN slips: the employee is found from the session's user id (never from the message)."""
        uid = current_user_id()
        if not uid:
            return {"slips": [], "employee": None, "signed_out": True}
        return self.repo.my_payslips(uid)

    def staff_advances_report(self, employee_id: str = "", status: str = "open") -> dict:
        return self.repo.staff_advances_report(employee_id or None, status or "open")

    def statutory_summary(self, kind: str = "eobi", period: str = "") -> dict:
        if not period:
            rid = self.latest_run_id()
            period = self.repo.payroll_register(rid)["run"]["period"] if rid else this_period()
        return self.repo.statutory_summary(period, kind or "eobi")

    def record_attendance(self, period: str, rows: list[dict], approved_by: str = "clerk") -> dict:
        return self.repo.set_attendance(period or this_period(), rows, TANKHWA, approved_by)

    def add_employee(self, name: str, designation: str = "", phone: str = "", basic: float = 0.0, pay_basis: str = "monthly",
                     approved_by: str = "owner") -> dict:
        # `basic` is the pay asked for: it is SET by the set_pay_structure card the platform chains after this one (its own
        # approval and its own audit row -- one gated write per approval); the employee record carries no pay field
        from munshi.domain.repository.payroll import ROLE_HINTS
        data = {"name": name, "designation": designation, "phone": phone} | ({"role_hint": designation} if designation in ROLE_HINTS else {})
        return self.repo.add_employee({k: v for k, v in data.items() if v}, TANKHWA, approved_by)

    def update_employee(self, employee_id: str, changes: dict, approved_by: str = "owner") -> dict:
        return self.repo.update_employee(employee_id, dict(changes or {}), TANKHWA, approved_by)

    def rehire_employee(self, employee_id: str, rejoined_on: str = "", approved_by: str = "owner") -> dict:
        return self.repo.rehire_employee(employee_id, rejoined_on or today_iso(), TANKHWA, approved_by)

    def set_pay_structure(self, employee_id: str, pay_basis: str = "monthly", basic: float = 0.0, daily_rate: float = 0.0, effective_from: str = "",
                          approved_by: str = "owner") -> dict:
        return self.repo.set_pay_structure(employee_id, effective_from or today_iso(), pay_basis or "monthly", float(basic or 0), float(daily_rate or 0),
                                           actor=TANKHWA, approved_by=approved_by)

    def set_commission_rule(self, employee_id: str, basis: str, rate_pct: float = 0.0, per_unit: float = 0.0, sku: str = "", effective_from: str = "",
                            approved_by: str = "owner") -> dict:
        return self.repo.set_commission_rule(employee_id, basis, float(rate_pct or 0), float(per_unit or 0), sku or None, effective_from=effective_from or None,
                                             actor=TANKHWA, approved_by=approved_by)

    def end_employment(self, employee_id: str, reason: str = "", left_on: str = "", approved_by: str = "owner") -> dict:
        return self.repo.end_employment(employee_id, left_on or today_iso(), reason or "left", TANKHWA, approved_by)

    def add_payroll_adjustment(self, employee_id: str, code: str, amount: float, note: str = "", period: str = "", ref: str = "",
                               approved_by: str = "owner") -> dict:
        code = {"other": "other_deduction", "deduction": "other_deduction", "loss": "loss_recovery"}.get(code, code)
        return self.repo.add_payroll_adjustment(employee_id, period or this_period(), code, abs(float(amount)), (note or code)[:200], ref or None,
                                                actor=TANKHWA, approved_by=approved_by)

    def void_payroll_adjustment(self, adj_id: str, approved_by: str = "owner") -> dict:
        return self.repo.void_payroll_adjustment(adj_id, TANKHWA, approved_by)

    def approve_payroll_run(self, period: str, fingerprint: str, approved_by: str = "owner") -> dict:
        # the fingerprint is the preview the card showed: a payroll that changed since is refused by the repository
        return self.repo.approve_payroll(period, fingerprint, TANKHWA, approved_by)

    def reverse_payroll_run(self, run_id: str, reason: str, approved_by: str = "owner") -> dict:
        return self.repo.reverse_payroll_run(run_id, reason, TANKHWA, approved_by)

    def pay_salaries(self, run_id: str, payments: list[dict], approved_by: str = "owner") -> dict:
        return self._proved("salary_payment", TANKHWA, lambda: self.repo.pay_salaries(run_id, [dict(p) for p in payments or []], TANKHWA, approved_by),
                            "payment_id")

    def reverse_salary_payment(self, payment_id: str, reason: str, approved_by: str = "owner") -> dict:
        return self.repo.reverse_salary_payment(payment_id, reason, TANKHWA, approved_by)

    def give_staff_advance(self, employee_id: str, amount: float, method: str, installment: float = 0.0, note: str = "", account_id: str = "",
                           approved_by: str = "owner") -> dict:
        return self._proved("staff_advance", TANKHWA, lambda: self.repo.give_staff_advance(
            employee_id, float(amount), method, account_id or None, "advance", float(installment or 0), None, note, actor=TANKHWA, approved_by=approved_by),
            "advance_id")

    def repay_staff_advance(self, employee_id: str, amount: float, method: str, account_id: str = "", approved_by: str = "owner") -> dict:
        return self._proved("staff_advance", TANKHWA, lambda: self.repo.repay_staff_advance(employee_id, float(amount), method, account_id or None,
                                                                                            actor=TANKHWA, approved_by=approved_by), "advance_id")

    def reverse_staff_advance(self, advance_id: str, reason: str, approved_by: str = "owner") -> dict:
        return self.repo.reverse_staff_advance(advance_id, reason, TANKHWA, approved_by)

    def record_statutory_payment(self, kind: str, period: str, amount: float, method: str, challan_ref: str, paid_on: str = "", account_id: str = "",
                                 approved_by: str = "owner") -> dict:
        return self._proved("statutory_payment", TANKHWA, lambda: self.repo.record_statutory_payment(
            kind, period, float(amount), method, account_id or None, challan_ref, paid_on or today_iso(), TANKHWA, approved_by), "payment_id", "statutory_id")

    def add_statutory_rate(self, key: str, value: str, effective_from: str, source: str, verified_on: str, source_url: str = "", grade: str = "C",
                           note: str = "", jurisdiction: str = "pk", approved_by: str = "owner") -> dict:
        return self.repo.add_statutory_rate(key, jurisdiction or "pk", str(value), effective_from, source, source_url, verified_on, grade, note, TANKHWA, approved_by)

    def set_payroll_settings(self, changes: dict, approved_by: str = "owner") -> dict:
        return self.repo.set_payroll_settings(dict(changes or {}), TANKHWA, approved_by)

    # ---------- accounts (company finance; Stream B's repository) ----------
    def money_accounts(self) -> dict:
        return self.repo.list_money_accounts()

    def account_book(self, account_id: str, start: str = "", end: str = "") -> dict:
        start, end = self._range(start, end)
        # salary / advance / statutory lines are aggregated for anyone but the owner (decision 2)
        return self.repo.account_book(account_id, start, end, redact_payroll=not _owner_view())

    def reconciliation_status(self, account_id: str, statement_date: str = "") -> dict:
        return self.repo.reconciliation(account_id, statement_date or today_iso(), redact_payroll=not _owner_view())

    def trial_balance(self, as_of: str = "") -> dict:
        return self.repo.trial_balance(as_of or None)

    def income_statement(self, start: str = "", end: str = "") -> dict:
        start, end = self._month(start, end)
        return self.repo.income_statement(start, end)

    def balance_sheet(self, as_of: str = "") -> dict:
        return self.repo.balance_sheet(as_of or None)

    def cash_flow(self, start: str = "", end: str = "") -> dict:
        start, end = self._month(start, end)
        return self.repo.cash_flow(start, end)

    def owner_kpis(self, as_of: str = "") -> dict:
        return self.repo.owner_kpis(as_of or None)

    def margins_report(self, by: str = "product", start: str = "", end: str = "") -> dict:
        start, end = self._range(start, end)
        return self.repo.margins(by or "product", start, end)

    def fixed_assets_register(self, as_of: str = "") -> dict:
        return self.repo.fixed_assets_register(as_of or None)

    def loans_report(self, as_of: str = "") -> dict:
        return self.repo.loans_report(as_of or None)

    def period_status(self) -> dict:
        return self.repo.period_status()

    def list_attachments(self, entity: str, entity_id: str) -> list[dict]:
        return self.repo.attachments_for(entity, entity_id)

    def transfer_between_accounts(self, from_account: str, to_account: str, amount: float, ref: str = "", note: str = "", approved_by: str = "clerk") -> dict:
        return self._proved("account_transfer", ACCOUNTS, lambda: self.repo.transfer(from_account, to_account, float(amount), None, ref, note, actor=ACCOUNTS,
                                                                                     approved_by=approved_by), "transfer_id")

    def count_cash(self, account_id: str, counted: float, note: str = "") -> dict:
        return self.repo.count_cash(account_id or "CASH", float(counted), note, ACCOUNTS)

    def mark_cleared(self, account_id: str, items: list[dict], cleared_on: str = "", cleared: bool = True) -> dict:
        return self.repo.mark_cleared(account_id, [dict(i) for i in items or []], cleared_on or today_iso(), bool(cleared), ACCOUNTS)

    def save_reconciliation(self, account_id: str, statement_date: str, statement_balance: float) -> dict:
        return self.repo.save_reconciliation(account_id, statement_date, float(statement_balance), ACCOUNTS, redact_payroll=not _owner_view())

    def add_money_account(self, kind: str, name: str, provider: str = "", number_last4: str = "", opening_balance: float = 0.0,
                          approved_by: str = "owner") -> dict:
        return self.repo.add_money_account(kind, name, provider, number_last4, float(opening_balance or 0), None, actor=ACCOUNTS, approved_by=approved_by)

    def set_method_route(self, method: str, account_id: str, effective_from: str = "", approved_by: str = "owner") -> dict:
        return self.repo.set_method_route(method, account_id, effective_from or today_iso(), ACCOUNTS, approved_by)

    def record_capital(self, amount: float, method: str = "cash", account_id: str = "", note: str = "", approved_by: str = "owner") -> dict:
        return self.repo.record_capital(float(amount), method or "cash", account_id or None, None, note, actor=ACCOUNTS, approved_by=approved_by)

    def record_drawing(self, amount: float, method: str = "cash", account_id: str = "", note: str = "", approved_by: str = "owner") -> dict:
        return self.repo.record_drawing(float(amount), method or "cash", account_id or None, None, note, actor=ACCOUNTS, approved_by=approved_by)

    def record_loan(self, lender: str, amount: float, method: str = "bank", kind: str = "informal", account_id: str = "", terms: str = "",
                    approved_by: str = "owner") -> dict:
        return self.repo.add_loan(lender, kind or "informal", float(amount), method or "bank", account_id or None, None, terms, actor=ACCOUNTS, approved_by=approved_by)

    def repay_loan(self, loan_id: str, principal: float, interest: float = 0.0, method: str = "bank", account_id: str = "", approved_by: str = "owner") -> dict:
        return self.repo.repay_loan(loan_id, float(principal), float(interest or 0), method or "bank", account_id or None, None, actor=ACCOUNTS, approved_by=approved_by)

    def add_fixed_asset(self, name: str, cost: float, category: str = "vehicle", life_months: int = 60, acquired_on: str = "", funded_by: str = "paid",
                        method: str = "", account_id: str = "", approved_by: str = "owner") -> dict:
        return self.repo.add_fixed_asset(name, category or "vehicle", float(cost), acquired_on or today_iso(), int(life_months or 60), 0.0, funded_by or "paid",
                                         method or None, account_id or None, None, actor=ACCOUNTS, approved_by=approved_by)

    def dispose_fixed_asset(self, asset_id: str, proceeds: float, method: str = "cash", on_date: str = "", approved_by: str = "owner") -> dict:
        return self.repo.dispose_fixed_asset(asset_id, on_date or today_iso(), float(proceeds or 0), method or "cash", None, actor=ACCOUNTS, approved_by=approved_by)

    def run_depreciation(self, through_period: str, approved_by: str = "owner") -> dict:
        return self.repo.run_depreciation(through_period or this_period(), ACCOUNTS, approved_by)

    def post_journal_entry(self, entry_date: str, memo: str, lines: list[dict], kind: str = "general", approved_by: str = "owner") -> dict:
        return self.repo.post_journal(entry_date or today_iso(), kind or "general", memo, [dict(x) for x in lines or []], actor=ACCOUNTS, approved_by=approved_by)

    def reverse_journal_entry(self, je_id: str, reason: str, approved_by: str = "owner") -> dict:
        return self.repo.reverse_journal(je_id, reason, ACCOUNTS, approved_by)

    def reverse_account_transfer(self, transfer_id: str, reason: str, approved_by: str = "owner") -> dict:
        return self.repo.reverse_transfer(transfer_id, reason, ACCOUNTS, approved_by)

    def post_cash_difference(self, count_id: str, approved_by: str = "owner") -> dict:
        return self.repo.post_cash_difference(count_id, ACCOUNTS, approved_by)

    def record_opening_balances(self, as_of: str, money: list[dict], assets: list[dict] | None = None, loans: list[dict] | None = None,
                                approved_by: str = "owner") -> dict:
        return self.repo.record_opening_balances(as_of, [dict(x) for x in money or []], [dict(x) for x in assets or []], [dict(x) for x in loans or []],
                                                 actor=ACCOUNTS, approved_by=approved_by)

    def close_period(self, through_date: str, note: str = "", approved_by: str = "owner") -> dict:
        return self.repo.close_period(through_date, note, False, actor=ACCOUNTS, approved_by=approved_by)

    def reopen_period(self, close_id: int, reason: str, approved_by: str = "owner") -> dict:
        return self.repo.reopen_period(int(close_id), reason, ACCOUNTS, approved_by)

    # ---------- read-only lookups for approval cards (never write; never exposed as agent tools) ----------
    # Which record already reverses this one, per reversible table. The table and column names are fixed
    # here, never taken from input, so the ids are the only bound parameters.
    _REVERSIBLE = {"expense": ("expenses", "expense_id"), "purchase": ("purchases", "purchase_id"), "supplier_entry": ("supplier_ledger", "entry_id")}

    def reversed_by(self, kind: str, record_id: str) -> str | None:
        """The id of the record that already reverses `record_id`, or None."""
        if kind == "ledger":
            return self.repo.reversal_of_ledger(record_id)
        table, col = self._REVERSIBLE[kind]
        r = self.repo._one(f"SELECT {col} AS rid FROM {table} WHERE reversal_of=? LIMIT 1", (record_id,))
        return r["rid"] if r else None

    def supplier_entry(self, entry_id: str):
        """One supplier-khata entry (SupplierLedgerEntry), or None if there is no such entry."""
        r = self.repo._one("SELECT entry_id, supplier_id, kind, amount, ref, method, created_at, reversal_of FROM supplier_ledger WHERE entry_id=?", (entry_id,))
        return self.repo._supplier_entry(r) if r else None

    def purchase_exists(self, purchase_id: str) -> bool:
        return self.repo._one("SELECT 1 FROM purchases WHERE purchase_id=?", (purchase_id,)) is not None

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

    @staticmethod
    def _month(start: str, end: str) -> tuple[str, str]:
        """A statement's period: default this month to date (a P&L or cash flow is read by the month)."""
        end = end or today_iso()
        return start or date.fromisoformat(end).replace(day=1).isoformat(), end
