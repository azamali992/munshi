"""Dispatch plans and delivery stops. Stock leaves the godown when a plan is
approved; a stop closes only against the customer's OTP; closing writes the
invoice and any cash to the khata and puts returns back on the shelf."""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import date, timedelta

from munshi.domain.models import DeliveryStop, DispatchPlan, now_iso
from munshi.domain.repository.base import CapacityError, NotFoundError, OtpError, StateError, new_id
from munshi.domain.repository.orders import OrdersMixin


class DispatchMixin(OrdersMixin):
    # ------------------------------------------------------------ plans
    def _plan_from_row(self, r: sqlite3.Row) -> DispatchPlan:
        d = dict(r); d["order_ids"] = json.loads(d["order_ids"]); return DispatchPlan(**d)

    def get_plan(self, plan_id: str) -> DispatchPlan:
        r = self._one("SELECT * FROM dispatch_plans WHERE plan_id=?", (plan_id,))
        if not r: raise NotFoundError(f"no such plan: {plan_id}")
        return self._plan_from_row(r)

    def list_plans(self, plan_date: str | None = None, status: str | None = None, limit: int = 50) -> list[DispatchPlan]:
        conds, a = [], []
        if plan_date: conds.append("plan_date=?"); a.append(plan_date)
        if status: conds.append("status=?"); a.append(status)
        q = "SELECT * FROM dispatch_plans" + (" WHERE " + " AND ".join(conds) if conds else "") + " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        a.append(limit)
        return [self._plan_from_row(r) for r in self._all(q, tuple(a))]

    def create_dispatch_plan(self, plan_date: str, route_id: str, vehicle_id: str, order_ids: list[str], actor: str) -> DispatchPlan:
        route = self.get_route(route_id); veh = self.get_vehicle(vehicle_id)
        if not order_ids: raise ValueError("a plan needs at least one order")
        orders = [self.get_order(o) for o in dict.fromkeys(order_ids)]
        for o in orders:
            if o.status != "allocated": raise StateError(f"order {o.order_id} is {o.status}; only allocated orders can be dispatched")
            if o.warehouse_id != route.warehouse_id: raise StateError(f"order {o.order_id} allocated at {o.warehouse_id}, route leaves from {route.warehouse_id}")
            if self._one("SELECT 1 FROM stops WHERE order_id=? AND status='pending'", (o.order_id,)):
                raise StateError(f"order {o.order_id} is already on a plan")
        load = sum(o.load_units for o in orders)
        if load > veh.capacity_units:
            raise CapacityError(f"load {load} units exceeds {veh.plate} capacity {veh.capacity_units}")
        plan = DispatchPlan(new_id("DSP"), plan_date, route_id, vehicle_id, route.warehouse_id, [o.order_id for o in orders], "planned", load)
        with self._tx() as c:
            c.execute("INSERT INTO dispatch_plans VALUES (?,?,?,?,?,?,?,?,?)",
                      (plan.plan_id, plan_date, route_id, vehicle_id, route.warehouse_id, json.dumps(plan.order_ids), "planned", load, plan.created_at))
            # stops in route order; orders for customers not on the route go last
            order_by_cust: dict[str, list] = {}
            for o in orders: order_by_cust.setdefault(o.customer_id, []).append(o)
            seq = 1
            for cid in route.stop_customer_ids + [c for c in order_by_cust if c not in route.stop_customer_ids]:
                for o in order_by_cust.get(cid, []):
                    c.execute("INSERT INTO stops (stop_id, plan_id, order_id, customer_id, sequence, status, delivered_items, returned_items, cash_collected, otp, otp_verified, closed_at, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (new_id("STP"), plan.plan_id, o.order_id, cid, seq, "pending", "[]", "[]", 0.0, None, 0, None, ""))
                    seq += 1
        self.audit(actor, "create_dispatch_plan", "plan", plan.plan_id, {"route": route_id, "vehicle": vehicle_id, "orders": plan.order_ids, "load": load})
        return plan

    def approve_dispatch_plan(self, plan_id: str, actor: str, approved_by: str) -> DispatchPlan:
        plan = self.get_plan(plan_id)
        if plan.status != "planned": raise StateError(f"plan {plan_id} is {plan.status}")
        with self._tx() as c:
            for oid in plan.order_ids:
                o = self.get_order(oid)
                for it in o.items:   # leave the godown: reserved -> gone
                    s = self.get_stock(plan.warehouse_id, it.sku)
                    c.execute("UPDATE stock SET reserved=? WHERE warehouse_id=? AND sku=?", (max(0, s.reserved - it.qty), plan.warehouse_id, it.sku))
                    self.move_stock(plan.warehouse_id, it.sku, -it.qty, "sale", plan_id, allow_negative=True)
                c.execute("UPDATE orders SET status='dispatched' WHERE order_id=?", (oid,))
            for st in self._all("SELECT stop_id FROM stops WHERE plan_id=?", (plan_id,)):
                c.execute("UPDATE stops SET otp=? WHERE stop_id=?", (f"{secrets.randbelow(10000):04d}", st["stop_id"]))
            c.execute("UPDATE dispatch_plans SET status='approved' WHERE plan_id=?", (plan_id,))
        self.audit(actor, "approve_dispatch_plan", "plan", plan_id, {}, approved_by)
        for st in self.list_stops(plan_id):     # the customer gets the code, never the driver
            cust = self.get_customer(st.customer_id)
            self.queue_message("whatsapp", cust.phone, f"{self.business_name}: aap ki delivery aaj aa rahi hai. Delivery code: {st.otp}. Yeh code sirf driver ko maal milne par dein.", st.stop_id)
        return self.get_plan(plan_id)

    def cancel_plan(self, plan_id: str, actor: str, approved_by: str | None = None) -> DispatchPlan:
        plan = self.get_plan(plan_id)
        if plan.status != "planned": raise StateError(f"only a planned (not yet loaded) plan can be cancelled; {plan_id} is {plan.status}")
        with self._tx() as c:
            c.execute("DELETE FROM stops WHERE plan_id=?", (plan_id,))
            c.execute("UPDATE dispatch_plans SET status='cancelled' WHERE plan_id=?", (plan_id,))
        self.audit(actor, "cancel_plan", "plan", plan_id, {}, approved_by)
        return self.get_plan(plan_id)

    # ------------------------------------------------------------ stops
    def _stop_from_row(self, r: sqlite3.Row) -> DeliveryStop:
        d = dict(r); d["delivered_items"] = json.loads(d["delivered_items"]); d["returned_items"] = json.loads(d["returned_items"])
        d["otp_verified"] = bool(d["otp_verified"]); d["note"] = d.get("note") or ""; return DeliveryStop(**d)

    def list_stops(self, plan_id: str) -> list[DeliveryStop]:
        return [self._stop_from_row(r) for r in self._all("SELECT * FROM stops WHERE plan_id=? ORDER BY sequence", (plan_id,))]

    def get_stop(self, stop_id: str) -> DeliveryStop:
        r = self._one("SELECT * FROM stops WHERE stop_id=?", (stop_id,))
        if not r: raise NotFoundError(f"no such stop: {stop_id}")
        return self._stop_from_row(r)

    def close_stop(self, stop_id: str, delivered_items: list[dict], returned_items: list[dict], cash_collected: float, otp: str, actor: str, note: str = "") -> dict:
        st = self.get_stop(stop_id); plan = self.get_plan(st.plan_id)
        if plan.status not in ("approved", "loaded"): raise StateError(f"plan {plan.plan_id} is {plan.status}; stops can only close on an approved plan")
        if st.status != "pending": raise StateError(f"stop {stop_id} already {st.status}")
        if not otp or not secrets.compare_digest(str(otp), str(st.otp or "")): raise OtpError("OTP does not match the one issued for this stop")
        if float(cash_collected) < 0: raise ValueError("cash collected cannot be negative")
        order = self.get_order(st.order_id); cust = self.get_customer(st.customer_id)
        ordered = {i.sku: i for i in order.items}
        delivered_value = 0.0; short = False
        for d in delivered_items:
            it = ordered.get(d["sku"])
            if not it: raise ValueError(f"{d['sku']} was not on order {order.order_id}")
            if int(d["qty"]) < 0: raise ValueError("delivered quantity cannot be negative")
            if int(d["qty"]) > it.qty: raise ValueError(f"delivered more {d['sku']} than ordered")
            if int(d["qty"]) < it.qty: short = True
            delivered_value += int(d["qty"]) * it.unit_price
        delivered_skus = {d["sku"] for d in delivered_items}
        if any(s not in delivered_skus for s in ordered): short = True
        status = "short" if short else "delivered"
        inv_id = None
        with self._tx() as c:
            c.execute("UPDATE stops SET status=?, delivered_items=?, returned_items=?, cash_collected=?, otp_verified=1, closed_at=?, note=? WHERE stop_id=?",
                      (status, json.dumps(delivered_items), json.dumps(returned_items), float(cash_collected), now_iso(), (note or "")[:200], stop_id))
            c.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order.order_id))
            for ret in returned_items:   # returns go back on the shelf at the plan's warehouse
                if int(ret["qty"]) > 0:
                    self.move_stock(plan.warehouse_id, ret["sku"], int(ret["qty"]), "return", stop_id)
            if delivered_value > 0:
                inv_id = new_id(self.setting("invoice_prefix") or "INV")
                due = (date.today() + timedelta(days=cust.credit_days or int(self.setting("credit_days")))).isoformat()
                c.execute("INSERT INTO ledger (entry_id, customer_id, kind, amount, ref, due_date, created_at, method, received_by) VALUES (?,?,?,?,?,?,?,?,?)",
                          (inv_id, st.customer_id, "invoice", round(delivered_value, 2), order.order_id, due, now_iso(), "", ""))
            if float(cash_collected) > 0:
                c.execute("INSERT INTO ledger (entry_id, customer_id, kind, amount, ref, due_date, created_at, method, received_by) VALUES (?,?,?,?,?,?,?,?,?)",
                          (new_id("PAY"), st.customer_id, "payment", -round(float(cash_collected), 2), stop_id, None, now_iso(), "cash", "driver"))
        self.audit(actor, "close_stop", "stop", stop_id, {"status": status, "value": round(delivered_value, 2), "cash": cash_collected, "invoice": inv_id, "note": (note or "")[:200]}, approved_by=f"otp:{otp}")
        if short:
            self.notify("clerk", "delivery", f"Short delivery at {cust.name} on {plan.plan_id}: invoiced Rs {delivered_value:,.0f} of Rs {order.total:,.0f}" + (f" — {note}" if note else ""), stop_id)
        return {"stop_id": stop_id, "status": status, "invoiced": round(delivered_value, 2), "cash_collected": float(cash_collected), "invoice_id": inv_id,
                "customer_id": st.customer_id}

    def complete_plan(self, plan_id: str, actor: str) -> DispatchPlan:
        with self._tx() as c:
            c.execute("UPDATE dispatch_plans SET status='completed' WHERE plan_id=?", (plan_id,))
        self.audit(actor, "complete_plan", "plan", plan_id, {})
        return self.get_plan(plan_id)
