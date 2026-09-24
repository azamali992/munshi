"""Orders: draft → confirmed → allocated → dispatched → delivered | short | cancelled."""
from __future__ import annotations

import json
import sqlite3

from munshi.domain.models import Order, OrderItem, sql_business_date
from munshi.domain.repository.base import CreditHoldError, InsufficientStockError, NotFoundError, StateError, new_id
from munshi.domain.repository.master import MasterDataMixin

_ORDER_COLS = "order_id, customer_id, items, status, channel, source_text, created_at, warehouse_id, discount_pct, notes, created_by"


class OrdersMixin(MasterDataMixin):
    def _order_from_row(self, r: sqlite3.Row) -> Order:
        d = dict(r); d["items"] = [OrderItem(**i) for i in json.loads(d["items"])]
        d.setdefault("discount_pct", 0.0); d.setdefault("notes", ""); d.setdefault("created_by", "")
        return Order(**d)

    def get_order(self, order_id: str) -> Order:
        r = self._one("SELECT * FROM orders WHERE order_id=?", (order_id,))
        if not r: raise NotFoundError(f"no such order: {order_id}")
        return self._order_from_row(r)

    def list_orders(self, status: str | None = None, customer_id: str | None = None, limit: int = 100, day: str | None = None) -> list[Order]:
        q, a = "SELECT * FROM orders", []
        conds = []
        if status: conds.append("status=?"); a.append(status)
        if customer_id: conds.append("customer_id=?"); a.append(customer_id)
        if day: conds.append(f"{sql_business_date('created_at')}=?"); a.append(day)
        if conds: q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at DESC, rowid DESC LIMIT ?"; a.append(limit)
        return [self._order_from_row(r) for r in self._all(q, tuple(a))]

    def outstanding(self, customer_id: str) -> float:
        r = self._one("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE customer_id=?", (customer_id,))
        return round(float(r["s"]), 2)

    def _resolve_items(self, cust, items: list[dict]) -> tuple[list[OrderItem], list[dict]]:
        """Lines with the customer's standing discount applied, or an explicit negotiated unit_price. Returns (lines, overrides)."""
        if not items: raise ValueError("an order needs at least one line")
        resolved, overrides = [], []
        for it in items:
            p = self.get_product(str(it["sku"])); qty = int(it["qty"])
            if qty <= 0: raise ValueError(f"bad quantity for {p.sku}")
            if not p.active: raise StateError(f"{p.name} is no longer sold")
            list_price = round(p.unit_price * (1 - cust.discount_pct / 100), 2)
            price = float(it["unit_price"]) if it.get("unit_price") not in (None, "", 0) else list_price
            if price < 0: raise ValueError(f"bad price for {p.sku}")
            if price != list_price: overrides.append({"sku": p.sku, "list": list_price, "price": price})
            resolved.append(OrderItem(p.sku, qty, round(price, 2)))
        return resolved, overrides

    def exposure_if(self, customer_id: str, extra: float) -> tuple[float, float]:
        """(exposure after adding `extra`, credit limit)."""
        cust = self.get_customer(customer_id)
        return round(self.outstanding(customer_id) + extra, 2), cust.credit_limit

    def create_order(self, customer_id: str, items: list[dict], channel: str, source_text: str, actor: str, notes: str = "") -> Order:
        cust = self.get_customer(customer_id)
        if not cust.active: raise StateError(f"{cust.name} is inactive")
        resolved, overrides = self._resolve_items(cust, items)
        order = Order(new_id("ORD"), customer_id, resolved, status="draft", channel=channel, source_text=source_text,
                      discount_pct=cust.discount_pct, notes=notes, created_by=self._current_user())
        with self._tx() as c:
            c.execute(f"INSERT INTO orders ({_ORDER_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (order.order_id, customer_id, json.dumps([i.__dict__ for i in resolved]), order.status, channel, source_text,
                       order.created_at, None, order.discount_pct, notes, order.created_by))
        self.audit(actor, "create_order", "order", order.order_id, {"customer": customer_id, "total": order.total, "price_overrides": overrides})
        # Credit check is a hold, not a refusal: the owner decides.
        exposure = self.outstanding(customer_id) + order.total
        if cust.credit_limit and exposure > cust.credit_limit:
            self.notify("owner", "credit_hold", f"{cust.name}: order {order.order_id} would take exposure to Rs {exposure:,.0f} (limit Rs {cust.credit_limit:,.0f})", order.order_id)
            raise CreditHoldError(f"{cust.name}: exposure {exposure:,.0f} exceeds limit {cust.credit_limit:,.0f} (order {order.order_id} saved as draft)")
        return order

    def set_order_status(self, order_id: str, status: str, actor: str, approved_by: str | None = None) -> Order:
        self.get_order(order_id)
        with self._tx() as c:
            c.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
        self.audit(actor, f"order_{status}", "order", order_id, {}, approved_by)
        return self.get_order(order_id)

    def over_credit(self, order: Order) -> str | None:
        """A sentence if confirming this order would take the customer over their limit, else None."""
        exposure, limit = self.exposure_if(order.customer_id, order.total)
        if limit and exposure > limit:
            return f"{self.get_customer(order.customer_id).name}: exposure would be Rs {exposure:,.0f} against a limit of Rs {limit:,.0f}"
        return None

    def confirm_order(self, order_id: str, actor: str, approved_by: str, override_credit: bool = False) -> Order:
        o = self.get_order(order_id)
        if o.status != "draft": raise StateError(f"order {order_id} is {o.status}, not draft")
        hold = self.over_credit(o)
        if hold and not override_credit:
            raise CreditHoldError(hold + " — the owner must confirm")
        o = self.set_order_status(order_id, "confirmed", actor, approved_by)
        if hold:
            self.audit(actor, "credit_override", "order", order_id, {"reason": hold}, approved_by)
        return o

    def update_order(self, order_id: str, items: list[dict] | None, notes: str | None, actor: str, approved_by: str | None = None) -> Order:
        """Edit a draft's lines or note. Anything past draft is a cancel + new order."""
        o = self.get_order(order_id)
        if o.status != "draft": raise StateError(f"order {order_id} is {o.status}; only drafts can be edited")
        cust = self.get_customer(o.customer_id)
        overrides: list[dict] = []
        if items is not None:
            resolved, overrides = self._resolve_items(cust, items)
            o.items = resolved
        if notes is not None: o.notes = notes
        with self._tx() as c:
            c.execute("UPDATE orders SET items=?, notes=? WHERE order_id=?", (json.dumps([i.__dict__ for i in o.items]), o.notes, order_id))
        self.audit(actor, "order_edited", "order", order_id, {"total": o.total, "lines": len(o.items), "price_overrides": overrides}, approved_by)
        return self.get_order(order_id)

    def cancel_order(self, order_id: str, reason: str, actor: str, approved_by: str | None = None) -> Order:
        o = self.get_order(order_id)
        if o.status not in ("draft", "confirmed", "allocated"):
            raise StateError(f"order {order_id} is {o.status}; only draft, confirmed or allocated orders can be cancelled")
        with self._tx() as c:
            if o.status == "allocated" and o.warehouse_id:
                for it in o.items:      # release the reservation
                    s = self.get_stock(o.warehouse_id, it.sku)
                    c.execute("UPDATE stock SET reserved=? WHERE warehouse_id=? AND sku=?", (max(0, s.reserved - it.qty), o.warehouse_id, it.sku))
            c.execute("UPDATE orders SET status='cancelled', notes=? WHERE order_id=?", ((o.notes + " | " if o.notes else "") + f"cancelled: {reason}"[:200], order_id))
        self.audit(actor, "order_cancelled", "order", order_id, {"reason": reason}, approved_by)
        return self.get_order(order_id)

    def allocate_order(self, order_id: str, warehouse_id: str, actor: str, approved_by: str | None = None) -> dict:
        o = self.get_order(order_id)
        if o.status != "confirmed": raise StateError(f"order {order_id} is {o.status}, not confirmed")
        self.get_warehouse(warehouse_id)
        short = []
        for it in o.items:
            s = self.get_stock(warehouse_id, it.sku)
            if s.available < it.qty: short.append({"sku": it.sku, "need": it.qty, "available": s.available})
        if short:
            raise InsufficientStockError(json.dumps(short))
        with self._tx() as c:
            for it in o.items:
                s = self.get_stock(warehouse_id, it.sku)
                c.execute("INSERT OR REPLACE INTO stock VALUES (?,?,?,?)", (warehouse_id, it.sku, s.on_hand, s.reserved + it.qty))
            c.execute("UPDATE orders SET status='allocated', warehouse_id=? WHERE order_id=?", (warehouse_id, order_id))
        self.audit(actor, "allocate_order", "order", order_id, {"warehouse": warehouse_id}, approved_by)
        return {"order_id": order_id, "warehouse_id": warehouse_id, "status": "allocated"}
