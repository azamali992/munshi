"""Dispatch plans and delivery stops. Stock leaves the godown when a plan is
approved; a stop closes only against the customer's OTP; closing writes the
invoice and any cash to the khata and puts returns back on the shelf.

Cost of a sale: when a plan is approved, each order line's stock leaves at
the moving-average cost of that moment, and that value is snapshotted on the
sale stock_moves row (value_paisa, order_id) in the same transaction as the
quantity change. When the stop closes, the delivered units are costed from
that snapshot (never from the product's current cost price) and written to
sale_lines; returned units go back on the shelf at the same snapshot cost."""
from __future__ import annotations

import json
import math
import secrets
import sqlite3
from datetime import timedelta

from munshi.domain.models import DeliveryStop, DispatchPlan, OrderItem, business_today, mul_div, now_iso, to_paisa, to_rupees
from munshi.domain.repository.base import CapacityError, NotFoundError, OtpError, StateError, new_id
from munshi.domain.repository.guarded import immediate_tx, request_hash
from munshi.domain.repository.numbering import next_doc_no
from munshi.domain.repository.orders import OrdersMixin

MAX_LINE_QTY = 100_000


def _stop_lines(lines, what: str) -> dict[str, int]:
    """Typed view of a stop's delivered/returned lines: {sku: qty}, one entry per product.

    Each line must be {"sku": non-empty str, "qty": whole number >= 0}. Repeated lines for the same product
    are summed (never multiplied into the invoice); the sum is then capped by the load check in close_stop.
    """
    if lines is None: return {}
    if not isinstance(lines, (list, tuple)): raise ValueError(f"{what} must be a list of {{sku, qty}} lines")
    out: dict[str, int] = {}
    for ln in lines:
        if not isinstance(ln, dict) or "sku" not in ln or "qty" not in ln:
            raise ValueError(f"every line in {what} needs a sku and a qty")
        sku, qty = ln["sku"], ln["qty"]
        if not isinstance(sku, str) or not sku.strip(): raise ValueError(f"{what}: sku must be a non-empty string")
        if isinstance(qty, bool) or not isinstance(qty, (int, float, str)): raise ValueError(f"{what}: qty for {sku} must be a whole number")
        try:
            q = float(qty)
        except ValueError:
            raise ValueError(f"{what}: qty for {sku} must be a whole number") from None
        if not math.isfinite(q) or not q.is_integer(): raise ValueError(f"{what}: qty for {sku} must be a whole number")
        if q < 0: raise ValueError(f"{what}: qty for {sku} cannot be negative")
        sku = sku.strip()
        out[sku] = out.get(sku, 0) + int(q)
        if out[sku] > MAX_LINE_QTY: raise ValueError(f"{what}: qty for {sku} is too large")
    return out


def _value_of(items: list[OrderItem], sku: str, qty: int) -> int:
    """Invoice value in paisa of `qty` units of `sku`, priced off the order's lines for it in order (an order may split a sku over lines)."""
    total, left = 0, qty
    for it in items:
        if it.sku != sku or left <= 0: continue
        take = min(left, it.qty); total += take * it.unit_price_paisa; left -= take
    return total


def _check_otp(otp, issued) -> None:
    if not otp or not secrets.compare_digest(str(otp), str(issued or "")):
        raise OtpError("OTP does not match the one issued for this stop")


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
                              (new_id("STP"), plan.plan_id, o.order_id, cid, seq, "pending", "[]", "[]", 0, None, 0, None, ""))
                    seq += 1
        self.audit(actor, "create_dispatch_plan", "plan", plan.plan_id, {"route": route_id, "vehicle": vehicle_id, "orders": plan.order_ids, "load": load})
        return plan

    def approve_dispatch_plan(self, plan_id: str, actor: str, approved_by: str) -> DispatchPlan:
        """Load the vehicle: every order's stock leaves the godown (reserved -> gone), costed at the
        moving average and snapshotted on the sale move. All or nothing: if any line would take a
        godown below zero, the whole plan is refused and nothing moves."""
        with immediate_tx(self) as c:
            plan = self.get_plan(plan_id)
            if plan.status != "planned": raise StateError(f"plan {plan_id} is {plan.status}")
            for oid in plan.order_ids:
                o = self.get_order(oid)
                for it in o.items:   # leave the godown: reserved -> gone
                    c.execute("UPDATE stock SET reserved=MAX(0, reserved - ?) WHERE warehouse_id=? AND sku=?", (it.qty, plan.warehouse_id, it.sku))
                    self.move_stock(plan.warehouse_id, it.sku, -it.qty, "sale", plan_id, order_id=oid)
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
        d["otp_verified"] = bool(d["otp_verified"]); d["note"] = d.get("note") or ""
        d["cash_collected"] = to_rupees(int(d.get("cash_collected") or 0)); return DeliveryStop(**d)

    def _stop_cash_paisa(self, plan_id: str) -> list[tuple[str, str, int]]:
        """(stop_id, customer_id, cash collected in paisa) for every stop on a plan."""
        return [(r["stop_id"], r["customer_id"], int(r["cash_collected"] or 0))
                for r in self._all("SELECT stop_id, customer_id, cash_collected FROM stops WHERE plan_id=? ORDER BY sequence", (plan_id,))]

    def _load_snapshot(self, plan_id: str, order_id: str, sku: str) -> tuple[int, int]:
        """(units loaded, their cost in paisa) for one product of one order on a plan, from the sale
        moves written when the plan was approved. Plans approved before V5 have no snapshot; they fall
        back to the moving average now (flagged by the caller as legacy)."""
        r = self._one("SELECT COALESCE(-SUM(delta), 0) q, COALESCE(-SUM(value_paisa), 0) v, COUNT(*) n FROM stock_moves "
                      "WHERE kind='sale' AND ref=? AND order_id=? AND sku=?", (plan_id, order_id, sku))
        return (int(r["q"]), int(r["v"])) if r["n"] else (0, -1)

    def list_stops(self, plan_id: str) -> list[DeliveryStop]:
        return [self._stop_from_row(r) for r in self._all("SELECT * FROM stops WHERE plan_id=? ORDER BY sequence", (plan_id,))]

    def list_stops_for_order(self, order_id: str) -> list[DeliveryStop]:
        """Every stop an order was on, most recently closed first."""
        return [self._stop_from_row(r) for r in self._all("SELECT * FROM stops WHERE order_id=? ORDER BY closed_at DESC, rowid DESC", (order_id,))]

    def get_stop(self, stop_id: str) -> DeliveryStop:
        r = self._one("SELECT * FROM stops WHERE stop_id=?", (stop_id,))
        if not r: raise NotFoundError(f"no such stop: {stop_id}")
        return self._stop_from_row(r)

    def close_stop(self, stop_id: str, delivered_items: list[dict], returned_items: list[dict], cash_collected: float, otp: str, actor: str,
                   note: str = "", client_ref: str = "") -> dict:
        """Close a delivery stop against the customer's OTP: invoice what was delivered, record cash, restock returns.

        Lines are reconciled against what was loaded for this stop (the order's lines): repeated lines for a
        product are summed into one, and for every product delivered + returned must not exceed what was loaded;
        returns are only accepted for products that were loaded. Any shortfall (loaded - delivered - returned)
        is reported as `unaccounted`, never silently dropped.

        The whole check-then-write runs in one write-locked transaction, and the stop is claimed with a guarded
        UPDATE (status must still be 'pending'), so parallel closes post exactly once and the rest get a
        StateError "already ...". An exact retry carrying the same `client_ref` replays the recorded result
        instead of posting again.
        """
        client_ref = (client_ref or "").strip() or None
        if client_ref is not None and len(client_ref) > 64: raise ValueError("client_ref is too long (max 64)")
        delivered = _stop_lines(delivered_items, "delivered_items")
        returned = _stop_lines(returned_items, "returned_items")
        try:
            cash = float(cash_collected)
        except (TypeError, ValueError):
            raise ValueError("cash collected must be a number") from None
        if not math.isfinite(cash) or cash < 0: raise ValueError("cash collected cannot be negative")
        cash_p = to_paisa(cash_collected if isinstance(cash_collected, (int, float)) else cash)
        cash = to_rupees(cash_p)
        fingerprint = request_hash(delivered, returned, cash)

        with immediate_tx(self) as c:
            st = self.get_stop(stop_id)
            if st.status != "pending":
                prior = self._one("SELECT client_ref, request_hash, result FROM stop_closes WHERE stop_id=?", (stop_id,))
                if client_ref and prior and prior["client_ref"] == client_ref:
                    _check_otp(otp, st.otp)
                    if prior["request_hash"] != fingerprint:
                        raise StateError(f"client_ref {client_ref} was already used for a different close of stop {stop_id}")
                    return json.loads(prior["result"]) | {"replayed": True}
                raise StateError(f"stop {stop_id} already {st.status}")
            plan = self.get_plan(st.plan_id)
            if plan.status not in ("approved", "loaded"): raise StateError(f"plan {plan.plan_id} is {plan.status}; stops can only close on an approved plan")
            _check_otp(otp, st.otp)
            if client_ref:
                other = self._one("SELECT stop_id FROM stop_closes WHERE client_ref=?", (client_ref,))
                if other: raise StateError(f"client_ref {client_ref} was already used to close stop {other['stop_id']}")
            order = self.get_order(st.order_id); cust = self.get_customer(st.customer_id)

            # ---- reconcile against the load: what went out on the vehicle for this stop is the order's lines
            loaded: dict[str, int] = {}
            for it in order.items: loaded[it.sku] = loaded.get(it.sku, 0) + it.qty
            for sku in delivered:
                if sku not in loaded: raise ValueError(f"{sku} was not loaded for stop {stop_id} (order {order.order_id})")
            for sku in returned:
                if sku not in loaded: raise ValueError(f"cannot return {sku}: it was not loaded for stop {stop_id} (order {order.order_id})")
            for sku, n in loaded.items():
                d, r = delivered.get(sku, 0), returned.get(sku, 0)
                if d + r > n:
                    raise ValueError(f"{sku}: delivered {d} + returned {r} = {d + r} is more than the {n} loaded for stop {stop_id}")
            unaccounted = {sku: n - delivered.get(sku, 0) - returned.get(sku, 0) for sku, n in loaded.items() if n - delivered.get(sku, 0) - returned.get(sku, 0) > 0}
            short = any(delivered.get(sku, 0) < n for sku, n in loaded.items())
            status = "short" if short else "delivered"
            delivered_p = sum(_value_of(order.items, sku, q) for sku, q in delivered.items())
            delivered_value = to_rupees(delivered_p)
            d_lines = [{"sku": s, "qty": q} for s, q in delivered.items() if q > 0]
            r_lines = [{"sku": s, "qty": q} for s, q in returned.items() if q > 0]

            # ---- cost of what left on the vehicle for this stop, split into delivered / returned / unaccounted
            cost_basis = "moving_average"
            costs: dict[str, tuple[int, int]] = {}          # sku -> (cost of delivered, cost of returned)
            for sku, n in loaded.items():
                q_loaded, v_loaded = self._load_snapshot(plan.plan_id, order.order_id, sku)
                if v_loaded < 0 or q_loaded != n:          # plan loaded before V5: no snapshot, cost at today's average
                    cost_basis = "legacy_cost_price"; q_loaded, v_loaded = n, n * self.avg_cost_paisa(sku)
                d, r = delivered.get(sku, 0), returned.get(sku, 0)
                ret_v = mul_div(v_loaded, r, q_loaded) if q_loaded else 0
                del_v = v_loaded - ret_v if d + r == q_loaded else min(mul_div(v_loaded, d, q_loaded) if q_loaded else 0, v_loaded - ret_v)
                costs[sku] = (del_v, ret_v)

            # ---- claim the stop: only one request can move it off 'pending'
            closed_at = now_iso()
            claimed = c.execute("UPDATE stops SET status=?, delivered_items=?, returned_items=?, cash_collected=?, otp_verified=1, closed_at=?, note=? WHERE stop_id=? AND status='pending'",
                                (status, json.dumps(d_lines), json.dumps(r_lines), cash_p, closed_at, (note or "")[:200], stop_id)).rowcount
            if claimed != 1: raise StateError(f"stop {stop_id} already closed")
            c.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order.order_id))
            for ln in r_lines:   # returns go back on the shelf at the plan's warehouse, at the cost they left at
                self.move_stock(plan.warehouse_id, ln["sku"], ln["qty"], "return", stop_id, value_paisa=costs[ln["sku"]][1], order_id=order.order_id)
            inv_id = None
            if delivered_p > 0:
                inv_id = next_doc_no(self, c, "invoice", closed_at)
                due = (business_today() + timedelta(days=cust.credit_days or int(self.setting("credit_days")))).isoformat()
                c.execute("INSERT INTO ledger (entry_id, customer_id, kind, amount, ref, due_date, created_at, method, received_by, doc_no) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (inv_id, st.customer_id, "invoice", delivered_p, order.order_id, due, closed_at, "", "", inv_id))
            for ln in d_lines:   # the sale record: revenue and the cost snapshot, per product
                c.execute("INSERT INTO sale_lines (stop_id, order_id, customer_id, invoice_id, sku, qty, revenue_paisa, cost_paisa, cost_basis, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (stop_id, order.order_id, st.customer_id, inv_id, ln["sku"], ln["qty"], _value_of(order.items, ln["sku"], ln["qty"]),
                           costs[ln["sku"]][0], cost_basis, closed_at))
            rcp_id = None
            if cash_p > 0:
                rcp_id = next_doc_no(self, c, "receipt", closed_at)
                c.execute("INSERT INTO ledger (entry_id, customer_id, kind, amount, ref, due_date, created_at, method, received_by, doc_no) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (rcp_id, st.customer_id, "payment", -cash_p, stop_id, None, closed_at, "cash", "driver", rcp_id))
            result = {"stop_id": stop_id, "status": status, "invoiced": delivered_value, "cash_collected": cash, "invoice_id": inv_id,
                      "receipt_id": rcp_id, "customer_id": st.customer_id, "unaccounted": unaccounted}
            try:
                c.execute("INSERT INTO stop_closes (stop_id, client_ref, request_hash, result, created_at) VALUES (?,?,?,?,?)",
                          (stop_id, client_ref, fingerprint, json.dumps(result), now_iso()))
            except sqlite3.IntegrityError:
                raise StateError(f"stop {stop_id} already closed (or client_ref {client_ref} already used)") from None
            self.audit(actor, "close_stop", "stop", stop_id, {"status": status, "value": delivered_value, "cash": cash, "invoice": inv_id,
                                                              "delivered": d_lines, "returned": r_lines, "unaccounted": unaccounted,
                                                              "client_ref": client_ref, "note": (note or "")[:200]}, approved_by=f"otp:{otp}")
            if short:
                gap = ", ".join(f"{n} x {s}" for s, n in unaccounted.items())
                self.notify("clerk", "delivery", f"Short delivery at {cust.name} on {plan.plan_id}: invoiced Rs {delivered_value:,.0f} of Rs {order.total:,.0f}"
                            + (f"; {gap} unaccounted (neither delivered nor returned)" if gap else "") + (f" — {note}" if note else ""), stop_id)
        return result | {"replayed": False}

    def complete_plan(self, plan_id: str, actor: str) -> DispatchPlan:
        with self._tx() as c:
            c.execute("UPDATE dispatch_plans SET status='completed' WHERE plan_id=?", (plan_id,))
        self.audit(actor, "complete_plan", "plan", plan_id, {})
        return self.get_plan(plan_id)
