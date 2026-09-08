"""Munshi's own system of record. SQLite, standard library only, every
business invariant enforced here so that agents, the web app, evals and
tests all go through one door. Nothing in this module knows about LLMs."""
from __future__ import annotations

import json
import random
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterator

from munshi.domain.models import (
    AuditRow, CashDeposit, Customer, DeliveryStop, DispatchPlan, LedgerEntry, Order,
    OrderItem, Product, Promise, Reminder, Route, StockLevel, Vehicle, Warehouse,
    now_iso, today_iso,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (customer_id TEXT PRIMARY KEY, name TEXT, phone TEXT, tier TEXT, credit_limit REAL, route_id TEXT, language TEXT);
CREATE TABLE IF NOT EXISTS products (sku TEXT PRIMARY KEY, name TEXT, unit_price REAL, aliases TEXT, units_per_load INTEGER);
CREATE TABLE IF NOT EXISTS warehouses (warehouse_id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS stock (warehouse_id TEXT, sku TEXT, on_hand INTEGER, reserved INTEGER, PRIMARY KEY (warehouse_id, sku));
CREATE TABLE IF NOT EXISTS routes (route_id TEXT PRIMARY KEY, name TEXT, warehouse_id TEXT, stop_customer_ids TEXT);
CREATE TABLE IF NOT EXISTS vehicles (vehicle_id TEXT PRIMARY KEY, plate TEXT, capacity_class TEXT, capacity_units INTEGER);
CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, customer_id TEXT, items TEXT, status TEXT, channel TEXT, source_text TEXT, created_at TEXT, warehouse_id TEXT);
CREATE TABLE IF NOT EXISTS dispatch_plans (plan_id TEXT PRIMARY KEY, plan_date TEXT, route_id TEXT, vehicle_id TEXT, warehouse_id TEXT, order_ids TEXT, status TEXT, load_units INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS stops (stop_id TEXT PRIMARY KEY, plan_id TEXT, order_id TEXT, customer_id TEXT, sequence INTEGER, status TEXT, delivered_items TEXT, returned_items TEXT, cash_collected REAL, otp TEXT, otp_verified INTEGER, closed_at TEXT);
CREATE TABLE IF NOT EXISTS deposits (deposit_id TEXT PRIMARY KEY, plan_id TEXT, amount_counted REAL, counted_by TEXT, deposited_at TEXT);
CREATE TABLE IF NOT EXISTS ledger (entry_id TEXT PRIMARY KEY, customer_id TEXT, kind TEXT, amount REAL, ref TEXT, due_date TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS reminders (reminder_id TEXT PRIMARY KEY, customer_id TEXT, tier TEXT, amount_due REAL, days_overdue INTEGER, message TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS promises (promise_id TEXT PRIMARY KEY, customer_id TEXT, amount REAL, promised_date TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit (audit_id TEXT PRIMARY KEY, actor TEXT, action TEXT, entity TEXT, entity_id TEXT, approved_by TEXT, payload TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS chat (msg_id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, text TEXT, meta TEXT, created_at TEXT);
"""


class NotFoundError(Exception): ...
class InsufficientStockError(Exception): ...
class CapacityError(Exception): ...
class CreditHoldError(Exception): ...
class OtpError(Exception): ...
class StateError(Exception): ...


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


class MunshiRepository:
    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------ audit
    def audit(self, actor: str, action: str, entity: str, entity_id: str, payload: dict, approved_by: str | None = None) -> AuditRow:
        row = AuditRow(_id("AUD"), actor, action, entity, entity_id, approved_by, payload)
        with self._tx() as c:
            c.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?,?)",
                      (row.audit_id, actor, action, entity, entity_id, approved_by, json.dumps(payload, default=str), row.created_at))
        return row

    def audit_log(self, limit: int = 50) -> list[dict]:
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in
                self._all("SELECT * FROM audit ORDER BY created_at DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------ master data
    def upsert_customer(self, c: Customer) -> None:
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO customers VALUES (?,?,?,?,?,?,?)",
                        (c.customer_id, c.name, c.phone, c.tier, c.credit_limit, c.route_id, c.language))

    def upsert_product(self, p: Product) -> None:
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO products VALUES (?,?,?,?,?)",
                        (p.sku, p.name, p.unit_price, json.dumps(p.aliases), p.units_per_load))

    def upsert_warehouse(self, w: Warehouse) -> None:
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO warehouses VALUES (?,?)", (w.warehouse_id, w.name))

    def set_stock(self, warehouse_id: str, sku: str, on_hand: int, reserved: int = 0) -> None:
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO stock VALUES (?,?,?,?)", (warehouse_id, sku, on_hand, reserved))

    def upsert_route(self, r: Route) -> None:
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO routes VALUES (?,?,?,?)",
                        (r.route_id, r.name, r.warehouse_id, json.dumps(r.stop_customer_ids)))

    def upsert_vehicle(self, v: Vehicle) -> None:
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO vehicles VALUES (?,?,?,?)", (v.vehicle_id, v.plate, v.capacity_class, v.capacity_units))

    def get_customer(self, customer_id: str) -> Customer:
        r = self._one("SELECT * FROM customers WHERE customer_id=?", (customer_id,))
        if not r: raise NotFoundError(f"no such customer: {customer_id}")
        return Customer(**dict(r))

    def find_customer(self, text: str) -> Customer | None:
        t = text.lower().strip()
        for r in self._all("SELECT * FROM customers"):
            if t == r["customer_id"].lower() or t == r["phone"] or t in r["name"].lower():
                return Customer(**dict(r))
        return None

    def list_customers(self) -> list[Customer]:
        return [Customer(**dict(r)) for r in self._all("SELECT * FROM customers ORDER BY name")]

    def get_product(self, sku: str) -> Product:
        r = self._one("SELECT * FROM products WHERE sku=?", (sku,))
        if not r: raise NotFoundError(f"no such product: {sku}")
        d = dict(r); d["aliases"] = json.loads(d["aliases"])
        return Product(**d)

    def list_products(self) -> list[Product]:
        out = []
        for r in self._all("SELECT * FROM products ORDER BY name"):
            d = dict(r); d["aliases"] = json.loads(d["aliases"]); out.append(Product(**d))
        return out

    def list_warehouses(self) -> list[Warehouse]:
        return [Warehouse(**dict(r)) for r in self._all("SELECT * FROM warehouses")]

    def get_route(self, route_id: str) -> Route:
        r = self._one("SELECT * FROM routes WHERE route_id=?", (route_id,))
        if not r: raise NotFoundError(f"no such route: {route_id}")
        d = dict(r); d["stop_customer_ids"] = json.loads(d["stop_customer_ids"])
        return Route(**d)

    def list_routes(self) -> list[Route]:
        out = []
        for r in self._all("SELECT * FROM routes"):
            d = dict(r); d["stop_customer_ids"] = json.loads(d["stop_customer_ids"]); out.append(Route(**d))
        return out

    def get_vehicle(self, vehicle_id: str) -> Vehicle:
        r = self._one("SELECT * FROM vehicles WHERE vehicle_id=?", (vehicle_id,))
        if not r: raise NotFoundError(f"no such vehicle: {vehicle_id}")
        return Vehicle(**dict(r))

    def list_vehicles(self) -> list[Vehicle]:
        return [Vehicle(**dict(r)) for r in self._all("SELECT * FROM vehicles")]

    # ------------------------------------------------------------ stock
    def get_stock(self, warehouse_id: str, sku: str) -> StockLevel:
        r = self._one("SELECT * FROM stock WHERE warehouse_id=? AND sku=?", (warehouse_id, sku))
        if not r: return StockLevel(warehouse_id, sku, 0, 0)
        return StockLevel(**dict(r))

    def stock_by_sku(self, sku: str) -> list[StockLevel]:
        return [StockLevel(**dict(r)) for r in self._all("SELECT * FROM stock WHERE sku=?", (sku,))]

    def list_stock(self, warehouse_id: str | None = None) -> list[StockLevel]:
        q, a = ("SELECT * FROM stock WHERE warehouse_id=?", (warehouse_id,)) if warehouse_id else ("SELECT * FROM stock", ())
        return [StockLevel(**dict(r)) for r in self._all(q, a)]

    def adjust_stock(self, warehouse_id: str, sku: str, delta: int, reason: str, actor: str, approved_by: str) -> StockLevel:
        self.get_product(sku)
        s = self.get_stock(warehouse_id, sku)
        if s.on_hand + delta < 0:
            raise InsufficientStockError(f"{sku} at {warehouse_id} would go to {s.on_hand + delta}")
        self.set_stock(warehouse_id, sku, s.on_hand + delta, s.reserved)
        self.audit(actor, "adjust_stock", "stock", f"{warehouse_id}/{sku}", {"delta": delta, "reason": reason}, approved_by)
        return self.get_stock(warehouse_id, sku)

    # ------------------------------------------------------------ orders
    def _order_from_row(self, r: sqlite3.Row) -> Order:
        d = dict(r); d["items"] = [OrderItem(**i) for i in json.loads(d["items"])]
        return Order(**d)

    def get_order(self, order_id: str) -> Order:
        r = self._one("SELECT * FROM orders WHERE order_id=?", (order_id,))
        if not r: raise NotFoundError(f"no such order: {order_id}")
        return self._order_from_row(r)

    def list_orders(self, status: str | None = None, customer_id: str | None = None, limit: int = 100) -> list[Order]:
        q, a = "SELECT * FROM orders", []
        conds = []
        if status: conds.append("status=?"); a.append(status)
        if customer_id: conds.append("customer_id=?"); a.append(customer_id)
        if conds: q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at DESC LIMIT ?"; a.append(limit)
        return [self._order_from_row(r) for r in self._all(q, tuple(a))]

    def outstanding(self, customer_id: str) -> float:
        r = self._one("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE customer_id=?", (customer_id,))
        return round(float(r["s"]), 2)

    def create_order(self, customer_id: str, items: list[dict], channel: str, source_text: str, actor: str) -> Order:
        cust = self.get_customer(customer_id)
        if not items: raise ValueError("an order needs at least one line")
        resolved = []
        for it in items:
            p = self.get_product(str(it["sku"])); qty = int(it["qty"])
            if qty <= 0: raise ValueError(f"bad quantity for {p.sku}")
            resolved.append(OrderItem(p.sku, qty, p.unit_price))
        order = Order(_id("ORD"), customer_id, resolved, status="draft", channel=channel, source_text=source_text)
        with self._tx() as c:
            c.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)",
                      (order.order_id, customer_id, json.dumps([i.__dict__ for i in resolved]), order.status, channel, source_text, order.created_at, None))
        self.audit(actor, "create_order", "order", order.order_id, {"customer": customer_id, "total": order.total})
        # Credit check is a hold, not a refusal: the owner decides.
        exposure = self.outstanding(customer_id) + order.total
        if cust.credit_limit and exposure > cust.credit_limit:
            raise CreditHoldError(f"{cust.name}: exposure {exposure:,.0f} exceeds limit {cust.credit_limit:,.0f} (order {order.order_id} saved as draft)")
        return order

    def set_order_status(self, order_id: str, status: str, actor: str, approved_by: str | None = None) -> Order:
        self.get_order(order_id)
        with self._tx() as c:
            c.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
        self.audit(actor, f"order_{status}", "order", order_id, {}, approved_by)
        return self.get_order(order_id)

    def confirm_order(self, order_id: str, actor: str, approved_by: str) -> Order:
        o = self.get_order(order_id)
        if o.status != "draft": raise StateError(f"order {order_id} is {o.status}, not draft")
        return self.set_order_status(order_id, "confirmed", actor, approved_by)

    def allocate_order(self, order_id: str, warehouse_id: str, actor: str, approved_by: str | None = None) -> dict:
        o = self.get_order(order_id)
        if o.status != "confirmed": raise StateError(f"order {order_id} is {o.status}, not confirmed")
        short = []
        for it in o.items:
            s = self.get_stock(warehouse_id, it.sku)
            if s.available < it.qty: short.append({"sku": it.sku, "need": it.qty, "available": s.available})
        if short:
            raise InsufficientStockError(json.dumps(short))
        for it in o.items:
            s = self.get_stock(warehouse_id, it.sku)
            self.set_stock(warehouse_id, it.sku, s.on_hand, s.reserved + it.qty)
        with self._tx() as c:
            c.execute("UPDATE orders SET status='allocated', warehouse_id=? WHERE order_id=?", (warehouse_id, order_id))
        self.audit(actor, "allocate_order", "order", order_id, {"warehouse": warehouse_id}, approved_by)
        return {"order_id": order_id, "warehouse_id": warehouse_id, "status": "allocated"}

    # ------------------------------------------------------------ dispatch
    def _plan_from_row(self, r: sqlite3.Row) -> DispatchPlan:
        d = dict(r); d["order_ids"] = json.loads(d["order_ids"]); return DispatchPlan(**d)

    def get_plan(self, plan_id: str) -> DispatchPlan:
        r = self._one("SELECT * FROM dispatch_plans WHERE plan_id=?", (plan_id,))
        if not r: raise NotFoundError(f"no such plan: {plan_id}")
        return self._plan_from_row(r)

    def list_plans(self, plan_date: str | None = None) -> list[DispatchPlan]:
        if plan_date:
            rows = self._all("SELECT * FROM dispatch_plans WHERE plan_date=? ORDER BY created_at", (plan_date,))
        else:
            rows = self._all("SELECT * FROM dispatch_plans ORDER BY created_at DESC LIMIT 50")
        return [self._plan_from_row(r) for r in rows]

    def create_dispatch_plan(self, plan_date: str, route_id: str, vehicle_id: str, order_ids: list[str], actor: str) -> DispatchPlan:
        route = self.get_route(route_id); veh = self.get_vehicle(vehicle_id)
        orders = [self.get_order(o) for o in order_ids]
        for o in orders:
            if o.status != "allocated": raise StateError(f"order {o.order_id} is {o.status}; only allocated orders can be dispatched")
            if o.warehouse_id != route.warehouse_id: raise StateError(f"order {o.order_id} allocated at {o.warehouse_id}, route leaves from {route.warehouse_id}")
        load = sum(o.load_units for o in orders)
        if load > veh.capacity_units:
            raise CapacityError(f"load {load} units exceeds {veh.plate} capacity {veh.capacity_units}")
        plan = DispatchPlan(_id("DSP"), plan_date, route_id, vehicle_id, route.warehouse_id, order_ids, "planned", load)
        with self._tx() as c:
            c.execute("INSERT INTO dispatch_plans VALUES (?,?,?,?,?,?,?,?,?)",
                      (plan.plan_id, plan_date, route_id, vehicle_id, route.warehouse_id, json.dumps(order_ids), "planned", load, plan.created_at))
            # stops in route order; orders for customers not on the route go last
            order_by_cust = {o.customer_id: o for o in orders}
            seq = 1
            for cid in route.stop_customer_ids + [c for c in order_by_cust if c not in route.stop_customer_ids]:
                if cid in order_by_cust:
                    o = order_by_cust[cid]
                    c.execute("INSERT INTO stops VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              (_id("STP"), plan.plan_id, o.order_id, cid, seq, "pending", "[]", "[]", 0.0, None, 0, None))
                    seq += 1
        self.audit(actor, "create_dispatch_plan", "plan", plan.plan_id, {"route": route_id, "vehicle": vehicle_id, "orders": order_ids, "load": load})
        return plan

    def approve_dispatch_plan(self, plan_id: str, actor: str, approved_by: str) -> DispatchPlan:
        plan = self.get_plan(plan_id)
        if plan.status != "planned": raise StateError(f"plan {plan_id} is {plan.status}")
        with self._tx() as c:
            for oid in plan.order_ids:
                o = self.get_order(oid)
                for it in o.items:   # leave the godown: reserved -> gone
                    s = self.get_stock(plan.warehouse_id, it.sku)
                    c.execute("UPDATE stock SET on_hand=?, reserved=? WHERE warehouse_id=? AND sku=?",
                              (s.on_hand - it.qty, max(0, s.reserved - it.qty), plan.warehouse_id, it.sku))
                c.execute("UPDATE orders SET status='dispatched' WHERE order_id=?", (oid,))
            for st in self._all("SELECT stop_id FROM stops WHERE plan_id=?", (plan_id,)):
                c.execute("UPDATE stops SET otp=? WHERE stop_id=?", (f"{random.randint(0, 9999):04d}", st["stop_id"]))
            c.execute("UPDATE dispatch_plans SET status='approved' WHERE plan_id=?", (plan_id,))
        self.audit(actor, "approve_dispatch_plan", "plan", plan_id, {}, approved_by)
        return self.get_plan(plan_id)

    # ------------------------------------------------------------ stops
    def _stop_from_row(self, r: sqlite3.Row) -> DeliveryStop:
        d = dict(r); d["delivered_items"] = json.loads(d["delivered_items"]); d["returned_items"] = json.loads(d["returned_items"])
        d["otp_verified"] = bool(d["otp_verified"]); return DeliveryStop(**d)

    def list_stops(self, plan_id: str) -> list[DeliveryStop]:
        return [self._stop_from_row(r) for r in self._all("SELECT * FROM stops WHERE plan_id=? ORDER BY sequence", (plan_id,))]

    def get_stop(self, stop_id: str) -> DeliveryStop:
        r = self._one("SELECT * FROM stops WHERE stop_id=?", (stop_id,))
        if not r: raise NotFoundError(f"no such stop: {stop_id}")
        return self._stop_from_row(r)

    def close_stop(self, stop_id: str, delivered_items: list[dict], returned_items: list[dict], cash_collected: float, otp: str, actor: str) -> dict:
        st = self.get_stop(stop_id); plan = self.get_plan(st.plan_id)
        if plan.status not in ("approved", "loaded"): raise StateError(f"plan {plan.plan_id} is {plan.status}; stops can only close on an approved plan")
        if st.status != "pending": raise StateError(f"stop {stop_id} already {st.status}")
        if not otp or otp != st.otp: raise OtpError("OTP does not match the one issued for this stop")
        order = self.get_order(st.order_id)
        ordered = {i.sku: i for i in order.items}
        delivered_value = 0.0; short = False
        for d in delivered_items:
            it = ordered.get(d["sku"])
            if not it: raise ValueError(f"{d['sku']} was not on order {order.order_id}")
            if int(d["qty"]) > it.qty: raise ValueError(f"delivered more {d['sku']} than ordered")
            if int(d["qty"]) < it.qty: short = True
            delivered_value += int(d["qty"]) * it.unit_price
        delivered_skus = {d["sku"] for d in delivered_items}
        if any(s not in delivered_skus for s in ordered): short = True
        status = "short" if short else "delivered"
        with self._tx() as c:
            c.execute("UPDATE stops SET status=?, delivered_items=?, returned_items=?, cash_collected=?, otp_verified=1, closed_at=? WHERE stop_id=?",
                      (status, json.dumps(delivered_items), json.dumps(returned_items), float(cash_collected), now_iso(), stop_id))
            c.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order.order_id))
            for ret in returned_items:   # returns go back on the shelf at the plan's warehouse
                s = self.get_stock(plan.warehouse_id, ret["sku"])
                c.execute("INSERT OR REPLACE INTO stock VALUES (?,?,?,?)", (plan.warehouse_id, ret["sku"], s.on_hand + int(ret["qty"]), s.reserved))
            if delivered_value > 0:
                due = (date.today() + timedelta(days=30)).isoformat()
                c.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?)", (_id("INV"), st.customer_id, "invoice", round(delivered_value, 2), order.order_id, due, now_iso()))
            if cash_collected > 0:
                c.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?)", (_id("PAY"), st.customer_id, "payment", -round(float(cash_collected), 2), stop_id, None, now_iso()))
        self.audit(actor, "close_stop", "stop", stop_id, {"status": status, "value": round(delivered_value, 2), "cash": cash_collected}, approved_by=f"otp:{otp}")
        return {"stop_id": stop_id, "status": status, "invoiced": round(delivered_value, 2), "cash_collected": cash_collected}

    def complete_plan(self, plan_id: str, actor: str) -> DispatchPlan:
        with self._tx() as c:
            c.execute("UPDATE dispatch_plans SET status='completed' WHERE plan_id=?", (plan_id,))
        self.audit(actor, "complete_plan", "plan", plan_id, {})
        return self.get_plan(plan_id)

    # ------------------------------------------------------------ cash
    def record_deposit(self, plan_id: str, amount_counted: float, counted_by: str, actor: str) -> dict:
        plan = self.get_plan(plan_id)
        stops = self.list_stops(plan_id)
        expected = round(sum(s.cash_collected for s in stops), 2)
        variance = round(float(amount_counted) - expected, 2)
        dep = CashDeposit(_id("DEP"), plan_id, float(amount_counted), counted_by)
        with self._tx() as c:
            c.execute("INSERT INTO deposits VALUES (?,?,?,?,?)", (dep.deposit_id, plan_id, dep.amount_counted, counted_by, dep.deposited_at))
        # attribution: the stop whose cash is closest to the shortfall is the first place to look
        suspects = []
        if variance < 0:
            gap = -variance
            suspects = sorted(stops, key=lambda s: abs(s.cash_collected - gap))[:2]
        self.audit(actor, "record_deposit", "plan", plan_id, {"expected": expected, "counted": amount_counted, "variance": variance})
        return {"plan_id": plan_id, "expected": expected, "counted": float(amount_counted), "variance": variance,
                "suspect_stops": [{"stop_id": s.stop_id, "customer_id": s.customer_id, "cash_collected": s.cash_collected} for s in suspects]}

    def deposits(self, plan_id: str) -> list[CashDeposit]:
        return [CashDeposit(**dict(r)) for r in self._all("SELECT * FROM deposits WHERE plan_id=?", (plan_id,))]

    # ------------------------------------------------------------ khata / receivables
    def ledger_for(self, customer_id: str) -> list[LedgerEntry]:
        return [LedgerEntry(**dict(r)) for r in self._all("SELECT * FROM ledger WHERE customer_id=? ORDER BY created_at", (customer_id,))]

    def add_ledger(self, customer_id: str, kind: str, amount: float, ref: str, due_date: str | None, actor: str, approved_by: str | None = None) -> LedgerEntry:
        self.get_customer(customer_id)
        e = LedgerEntry(_id(kind[:3].upper()), customer_id, kind, round(float(amount), 2), ref, due_date)
        with self._tx() as c:
            c.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?)", (e.entry_id, customer_id, kind, e.amount, ref, due_date, e.created_at))
        self.audit(actor, f"ledger_{kind}", "ledger", e.entry_id, {"customer": customer_id, "amount": e.amount}, approved_by)
        return e

    def aging(self, as_of: str | None = None) -> list[dict]:
        """Per customer: outstanding balance and the oldest unpaid invoice age.
        Payments are applied oldest-first (FIFO), the way a munshi does it."""
        as_of_d = date.fromisoformat(as_of or today_iso())
        out = []
        for cust in self.list_customers():
            entries = self.ledger_for(cust.customer_id)
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
            out.append({"customer_id": cust.customer_id, "name": cust.name, "phone": cust.phone, "language": cust.language,
                        "balance": bal, "days_overdue": days_over, "bucket": bucket, "credit_limit": cust.credit_limit})
        return sorted(out, key=lambda r: (-r["days_overdue"], -r["balance"]))

    def draft_reminder(self, customer_id: str, tier: str, amount_due: float, days_overdue: int, message: str, actor: str) -> Reminder:
        r = Reminder(_id("REM"), customer_id, tier, round(float(amount_due), 2), int(days_overdue), message)
        with self._tx() as c:
            c.execute("INSERT INTO reminders VALUES (?,?,?,?,?,?,?,?)", (r.reminder_id, customer_id, tier, r.amount_due, r.days_overdue, message, "drafted", r.created_at))
        self.audit(actor, "draft_reminder", "reminder", r.reminder_id, {"customer": customer_id, "tier": tier})
        return r

    def set_reminder_status(self, reminder_id: str, status: str, actor: str, approved_by: str | None = None) -> Reminder:
        with self._tx() as c:
            c.execute("UPDATE reminders SET status=? WHERE reminder_id=?", (status, reminder_id))
        self.audit(actor, f"reminder_{status}", "reminder", reminder_id, {}, approved_by)
        r = self._one("SELECT * FROM reminders WHERE reminder_id=?", (reminder_id,)); return Reminder(**dict(r))

    def list_reminders(self, status: str | None = None) -> list[Reminder]:
        rows = self._all("SELECT * FROM reminders WHERE status=? ORDER BY created_at DESC", (status,)) if status else self._all("SELECT * FROM reminders ORDER BY created_at DESC LIMIT 100")
        return [Reminder(**dict(r)) for r in rows]

    def log_promise(self, customer_id: str, amount: float, promised_date: str, actor: str) -> Promise:
        p = Promise(_id("PRM"), customer_id, round(float(amount), 2), promised_date)
        with self._tx() as c:
            c.execute("INSERT INTO promises VALUES (?,?,?,?,?)", (p.promise_id, customer_id, p.amount, promised_date, p.created_at))
        self.audit(actor, "log_promise", "promise", p.promise_id, {"customer": customer_id, "amount": p.amount, "date": promised_date})
        return p

    def list_promises(self) -> list[Promise]:
        return [Promise(**dict(r)) for r in self._all("SELECT * FROM promises ORDER BY promised_date")]

    # ------------------------------------------------------------ chat log
    def add_chat(self, thread_id: str, role: str, text: str, meta: dict | None = None) -> dict:
        m = {"msg_id": _id("MSG"), "thread_id": thread_id, "role": role, "text": text, "meta": meta or {}, "created_at": now_iso()}
        with self._tx() as c:
            c.execute("INSERT INTO chat VALUES (?,?,?,?,?,?)", (m["msg_id"], thread_id, role, text, json.dumps(m["meta"], default=str), m["created_at"]))
        return m

    def chat_history(self, thread_id: str, limit: int = 60) -> list[dict]:
        rows = self._all("SELECT * FROM chat WHERE thread_id=? ORDER BY created_at DESC LIMIT ?", (thread_id, limit))
        return [dict(r) | {"meta": json.loads(r["meta"])} for r in reversed(rows)]

    # ------------------------------------------------------------ digest
    def digest(self, day: str | None = None) -> dict:
        day = day or today_iso()
        orders_today = [o for o in self.list_orders(limit=500) if o.created_at.startswith(day)]
        plans = self.list_plans(day)
        stops = [s for p in plans for s in self.list_stops(p.plan_id)]
        cash = round(sum(s.cash_collected for s in stops), 2)
        deps = [d for p in plans for d in self.deposits(p.plan_id)]
        aging = self.aging()
        low = [s for s in self.list_stock() if s.available <= 10]
        return {
            "date": day,
            "orders": {"count": len(orders_today), "value": round(sum(o.total for o in orders_today), 2),
                       "draft": sum(o.status == "draft" for o in orders_today)},
            "dispatch": {"plans": len(plans), "stops": len(stops),
                         "delivered": sum(s.status == "delivered" for s in stops), "short": sum(s.status == "short" for s in stops),
                         "pending": sum(s.status == "pending" for s in stops)},
            "cash": {"collected": cash, "deposited": round(sum(d.amount_counted for d in deps), 2)},
            "receivables": {"customers": len(aging), "total": round(sum(a["balance"] for a in aging), 2),
                            "overdue_60": round(sum(a["balance"] for a in aging if a["bucket"] == "60+"), 2)},
            "low_stock": [{"warehouse_id": s.warehouse_id, "sku": s.sku, "available": s.available} for s in low][:8],
            "pending_reminders": len(self.list_reminders("drafted")),
        }
