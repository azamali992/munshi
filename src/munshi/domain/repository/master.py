"""Master data: customers, products, suppliers, godowns, routes, vehicles,
and the stock ledger (levels + every movement).

Stock rules (Phase 2):
  * move_stock is the ONLY way stock.on_hand changes, and it always writes the
    matching stock_moves row in the same transaction, so replaying stock_moves
    reproduces stock exactly.
  * on_hand can never go below zero: every path is checked inside a
    write-locked transaction (immediate_tx) and refused with
    InsufficientStockError; a database trigger (migration V5) backs this up.
  * Costing is the perpetual moving (weighted) average per product, pooled
    across godowns. inventory_value holds the pool's total value in paisa;
    each move records the value it added or removed (stock_moves.value_paisa),
    computed atomically with the quantity change. The average is pool / units.
    Issues take a proportional share of the pool (rounded to the paisa), and
    the last unit out takes whatever is left, so value is conserved exactly:
    opening + receipts = issues + closing, to the paisa."""
from __future__ import annotations

import json

from munshi.domain.models import Customer, Product, Route, StockLevel, StockMove, Supplier, Vehicle, Warehouse, mul_div, to_paisa, to_rupees
from munshi.domain.repository.base import InsufficientStockError, NotFoundError, RepositoryBase, new_id
from munshi.domain.repository.guarded import immediate_tx

_MOVE_COLS = "move_id, warehouse_id, sku, delta, kind, ref, created_at"


def _whole(n, what: str) -> int:
    if isinstance(n, bool): raise ValueError(f"{what} must be a whole number")
    try:
        f = float(n)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a whole number") from None
    if not f.is_integer(): raise ValueError(f"{what} must be a whole number")
    return int(f)


class MasterDataMixin(RepositoryBase):
    # ------------------------------------------------------------ customers
    def upsert_customer(self, c: Customer) -> Customer:
        if not c.customer_id:
            c.customer_id = self._next_code("C", "customers", "customer_id")
        limit_p = to_paisa(c.credit_limit or 0)
        if limit_p < 0: raise ValueError("credit limit cannot be negative")
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO customers (customer_id, name, phone, tier, credit_limit, route_id, language, address, discount_pct, credit_days, active) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (c.customer_id, c.name.strip(), c.phone.strip(), c.tier, limit_p, c.route_id or None, c.language, c.address, float(c.discount_pct), int(c.credit_days), int(c.active)))
        c.credit_limit = to_rupees(limit_p)
        return c

    def get_customer(self, customer_id: str) -> Customer:
        r = self._one("SELECT * FROM customers WHERE customer_id=?", (customer_id,))
        if not r: raise NotFoundError(f"no such customer: {customer_id}")
        return self._customer(r)

    def find_customer(self, text: str) -> Customer | None:
        t = text.lower().strip()
        if not t: return None
        rows = self._all("SELECT * FROM customers WHERE active=1")
        for r in rows:
            if t == r["customer_id"].lower() or (r["phone"] and t == r["phone"].lower()):
                return self._customer(r)
        for r in rows:
            if t in r["name"].lower():
                return self._customer(r)
        return None

    def list_customers(self, include_inactive: bool = False, route_id: str | None = None) -> list[Customer]:
        q, a = "SELECT * FROM customers", []
        conds = [] if include_inactive else ["active=1"]
        if route_id: conds.append("route_id=?"); a.append(route_id)
        if conds: q += " WHERE " + " AND ".join(conds)
        return [self._customer(r) for r in self._all(q + " ORDER BY name", tuple(a))]

    def search_customers(self, text: str, limit: int = 20) -> list[Customer]:
        t = f"%{text.lower().strip()}%"
        return [self._customer(r) for r in self._all("SELECT * FROM customers WHERE active=1 AND (lower(name) LIKE ? OR phone LIKE ? OR lower(customer_id) LIKE ?) ORDER BY name LIMIT ?", (t, t, t, limit))]

    def customer_credit_limit_paisa(self, customer_id: str) -> int:
        r = self._one("SELECT credit_limit FROM customers WHERE customer_id=?", (customer_id,))
        if not r: raise NotFoundError(f"no such customer: {customer_id}")
        return int(r["credit_limit"] or 0)

    @staticmethod
    def _customer(r) -> Customer:
        d = dict(r); d["active"] = bool(d.get("active", 1)); d["credit_limit"] = to_rupees(int(d.get("credit_limit") or 0)); return Customer(**d)

    # ------------------------------------------------------------ products
    def upsert_product(self, p: Product) -> Product:
        if not p.sku:
            p.sku = self._next_code("P", "products", "sku")
        price_p, cost_p = to_paisa(p.unit_price or 0), to_paisa(p.cost_price or 0)
        if price_p < 0 or cost_p < 0: raise ValueError("prices cannot be negative")
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO products (sku, name, unit_price, aliases, units_per_load, cost_price, unit, category, min_stock, active) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (p.sku, p.name.strip(), price_p, json.dumps(p.aliases), int(p.units_per_load), cost_p, p.unit, p.category, int(p.min_stock), int(p.active)))
        p.unit_price, p.cost_price = to_rupees(price_p), to_rupees(cost_p)
        return p

    def _product_paisa(self, sku: str) -> tuple[int, int]:
        """(unit_price, cost_price) of a product in paisa; (0, 0) if unknown."""
        r = self._one("SELECT unit_price, cost_price FROM products WHERE sku=?", (sku,))
        return (int(r["unit_price"] or 0), int(r["cost_price"] or 0)) if r else (0, 0)

    def get_product(self, sku: str) -> Product:
        r = self._one("SELECT * FROM products WHERE sku=?", (sku,))
        if not r: raise NotFoundError(f"no such product: {sku}")
        return self._product(r)

    def list_products(self, include_inactive: bool = False) -> list[Product]:
        q = "SELECT * FROM products" + ("" if include_inactive else " WHERE active=1") + " ORDER BY name"
        return [self._product(r) for r in self._all(q)]

    @staticmethod
    def _product(r) -> Product:
        d = dict(r); d["aliases"] = json.loads(d["aliases"] or "[]"); d["active"] = bool(d.get("active", 1))
        d["unit_price"] = to_rupees(int(d.get("unit_price") or 0)); d["cost_price"] = to_rupees(int(d.get("cost_price") or 0))
        return Product(**d)

    # ------------------------------------------------------------ suppliers
    def upsert_supplier(self, s: Supplier) -> Supplier:
        if not s.supplier_id:
            s.supplier_id = self._next_code("S", "suppliers", "supplier_id")
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO suppliers VALUES (?,?,?,?,?)", (s.supplier_id, s.name.strip(), s.phone, s.address, int(s.active)))
        return s

    def get_supplier(self, supplier_id: str) -> Supplier:
        r = self._one("SELECT * FROM suppliers WHERE supplier_id=?", (supplier_id,))
        if not r: raise NotFoundError(f"no such supplier: {supplier_id}")
        d = dict(r); d["active"] = bool(d["active"]); return Supplier(**d)

    def find_supplier(self, text: str) -> Supplier | None:
        t = text.lower().strip()
        for r in self._all("SELECT * FROM suppliers WHERE active=1"):
            if t == r["supplier_id"].lower() or t in r["name"].lower() or (r["phone"] and t == r["phone"]):
                d = dict(r); d["active"] = True; return Supplier(**d)
        return None

    def list_suppliers(self) -> list[Supplier]:
        out = []
        for r in self._all("SELECT * FROM suppliers WHERE active=1 ORDER BY name"):
            d = dict(r); d["active"] = True; out.append(Supplier(**d))
        return out

    # ------------------------------------------------------------ godowns, routes, vehicles
    def upsert_warehouse(self, w: Warehouse) -> Warehouse:
        if not w.warehouse_id:
            w.warehouse_id = "WH-" + self._slug(w.name)
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO warehouses VALUES (?,?)", (w.warehouse_id, w.name.strip()))
        return w

    def get_warehouse(self, warehouse_id: str) -> Warehouse:
        r = self._one("SELECT * FROM warehouses WHERE warehouse_id=?", (warehouse_id,))
        if not r: raise NotFoundError(f"no such godown: {warehouse_id}")
        return Warehouse(**dict(r))

    def list_warehouses(self) -> list[Warehouse]:
        return [Warehouse(**dict(r)) for r in self._all("SELECT * FROM warehouses ORDER BY name")]

    def default_warehouse_id(self) -> str:
        s = self.setting("default_warehouse")
        if s: return s
        whs = self.list_warehouses()
        if not whs: raise NotFoundError("no godown set up yet")
        return whs[0].warehouse_id

    def upsert_route(self, r: Route) -> Route:
        if not r.route_id:
            r.route_id = "R-" + self._slug(r.name)
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO routes VALUES (?,?,?,?)", (r.route_id, r.name.strip(), r.warehouse_id, json.dumps(r.stop_customer_ids)))
        return r

    def get_route(self, route_id: str) -> Route:
        r = self._one("SELECT * FROM routes WHERE route_id=?", (route_id,))
        if not r: raise NotFoundError(f"no such route: {route_id}")
        d = dict(r); d["stop_customer_ids"] = json.loads(d["stop_customer_ids"] or "[]"); return Route(**d)

    def list_routes(self) -> list[Route]:
        out = []
        for r in self._all("SELECT * FROM routes ORDER BY name"):
            d = dict(r); d["stop_customer_ids"] = json.loads(d["stop_customer_ids"] or "[]"); out.append(Route(**d))
        return out

    def delete_route(self, route_id: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE customers SET route_id=NULL WHERE route_id=?", (route_id,))
            c.execute("DELETE FROM routes WHERE route_id=?", (route_id,))

    def upsert_vehicle(self, v: Vehicle) -> Vehicle:
        if not v.vehicle_id:
            v.vehicle_id = self._next_code("V", "vehicles", "vehicle_id", width=2)
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO vehicles VALUES (?,?,?,?)", (v.vehicle_id, v.plate.strip(), v.capacity_class, int(v.capacity_units)))
        return v

    def get_vehicle(self, vehicle_id: str) -> Vehicle:
        r = self._one("SELECT * FROM vehicles WHERE vehicle_id=?", (vehicle_id,))
        if not r: raise NotFoundError(f"no such vehicle: {vehicle_id}")
        return Vehicle(**dict(r))

    def list_vehicles(self) -> list[Vehicle]:
        return [Vehicle(**dict(r)) for r in self._all("SELECT * FROM vehicles ORDER BY capacity_units")]

    def delete_vehicle(self, vehicle_id: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM vehicles WHERE vehicle_id=?", (vehicle_id,))

    # ------------------------------------------------------------ stock
    def get_stock(self, warehouse_id: str, sku: str) -> StockLevel:
        r = self._one("SELECT * FROM stock WHERE warehouse_id=? AND sku=?", (warehouse_id, sku))
        if not r: return StockLevel(warehouse_id, sku, 0, 0)
        return StockLevel(**dict(r))

    def set_stock(self, warehouse_id: str, sku: str, on_hand: int, reserved: int = 0) -> None:
        """Set a godown's count outright (seeding, stock-take). The difference is posted
        through move_stock as an 'adjust' move, so the stock ledger stays complete."""
        on_hand, reserved = _whole(on_hand, "on hand"), _whole(reserved, "reserved")
        if on_hand < 0: raise InsufficientStockError(f"{sku} at {warehouse_id}: stock cannot be set below zero ({on_hand})")
        if reserved < 0: raise ValueError("reserved cannot be negative")
        with immediate_tx(self) as c:
            delta = on_hand - self.get_stock(warehouse_id, sku).on_hand
            if delta:
                self.move_stock(warehouse_id, sku, delta, "adjust", "set_stock")
            c.execute("INSERT INTO stock (warehouse_id, sku, on_hand, reserved) VALUES (?,?,?,?) "
                      "ON CONFLICT(warehouse_id, sku) DO UPDATE SET reserved=excluded.reserved", (warehouse_id, sku, on_hand, reserved))

    def stock_by_sku(self, sku: str) -> list[StockLevel]:
        return [StockLevel(**dict(r)) for r in self._all("SELECT * FROM stock WHERE sku=?", (sku,))]

    def list_stock(self, warehouse_id: str | None = None) -> list[StockLevel]:
        q, a = ("SELECT * FROM stock WHERE warehouse_id=?", (warehouse_id,)) if warehouse_id else ("SELECT * FROM stock", ())
        return [StockLevel(**dict(r)) for r in self._all(q, a)]

    # ------------------------------------------------------------ moving-average pool
    def _pool(self, sku: str) -> tuple[int, int]:
        """(units on hand across all godowns, pool value in paisa) for a product."""
        q = self._one("SELECT COALESCE(SUM(on_hand), 0) q FROM stock WHERE sku=?", (sku,))["q"]
        v = self._one("SELECT value_paisa FROM inventory_value WHERE sku=?", (sku,))
        return int(q), int(v["value_paisa"]) if v else 0

    def avg_cost_paisa(self, sku: str) -> int:
        """Current moving-average unit cost, rounded to the paisa (display only; never used to post)."""
        qty, pool = self._pool(sku)
        return mul_div(pool, 1, qty) if qty > 0 else self._product_paisa(sku)[1]

    def _move_value(self, sku: str, delta: int, kind: str, value_paisa: int | None) -> int:
        """The signed inventory value a move of `delta` units adds to (or takes from) the pool."""
        qty, pool = self._pool(sku)
        after = qty + delta
        if value_paisa is not None:
            v = int(value_paisa)                                  # an explicit cost (purchase, return at its load cost)
        elif delta > 0:
            v = mul_div(pool, delta, qty) if qty > 0 else delta * self._product_paisa(sku)[1]   # at average; empty pool: cost price
        else:
            v = -mul_div(pool, -delta, qty) if qty > 0 else 0      # issue at average
        if after <= 0 and not kind.startswith("transfer"):
            v = -pool                                             # the last unit out takes what is left: the pool ends at exactly 0
        return max(v, -pool)                                      # the pool never goes negative

    def move_stock(self, warehouse_id: str, sku: str, delta: int, kind: str, ref: str, *,
                   value_paisa: int | None = None, order_id: str | None = None) -> StockLevel:
        """The only way on_hand changes. In one write-locked transaction: refuse anything that
        would take on_hand below zero, change on_hand, write the stock_moves row (with the value
        moved, i.e. the cost snapshot), and update the product's moving-average pool."""
        delta = _whole(delta, "quantity")
        if delta == 0: raise ValueError("a stock move needs a non-zero quantity")
        with immediate_tx(self) as c:
            s = self.get_stock(warehouse_id, sku)
            if delta < 0 and s.on_hand + delta < 0:     # (an inbound move may lift a legacy negative row towards zero)
                raise InsufficientStockError(f"{sku} at {warehouse_id}: only {s.on_hand} on hand, cannot take out {-delta} — stock may never go below zero")
            value = self._move_value(sku, delta, kind, value_paisa)
            # (not an UPSERT: the V5 BEFORE INSERT guard would see a negative delta as a negative row)
            if not c.execute("UPDATE stock SET on_hand = on_hand + ? WHERE warehouse_id=? AND sku=?", (delta, warehouse_id, sku)).rowcount:
                c.execute("INSERT INTO stock (warehouse_id, sku, on_hand, reserved) VALUES (?,?,?,0)", (warehouse_id, sku, delta))
            m = StockMove(new_id("MOV"), warehouse_id, sku, delta, kind, ref)
            c.execute(f"INSERT INTO stock_moves ({_MOVE_COLS}, value_paisa, order_id) VALUES (?,?,?,?,?,?,?,?,?)",
                      (m.move_id, warehouse_id, sku, delta, kind, ref, m.created_at, value, order_id))
            if not c.execute("UPDATE inventory_value SET value_paisa = value_paisa + ? WHERE sku=?", (value, sku)).rowcount:
                c.execute("INSERT INTO inventory_value (sku, value_paisa) VALUES (?, ?)", (sku, value))
        return self.get_stock(warehouse_id, sku)

    def adjust_stock(self, warehouse_id: str, sku: str, delta: int, reason: str, actor: str, approved_by: str) -> StockLevel:
        """A manual correction (damage, count difference). Priced at the moving average; never below zero."""
        delta = _whole(delta, "adjustment")
        if delta == 0: raise ValueError("an adjustment needs a non-zero quantity")
        with immediate_tx(self):
            self.get_product(sku); self.get_warehouse(warehouse_id)
            s = self.move_stock(warehouse_id, sku, delta, "adjust", reason[:80])
            self.audit(actor, "adjust_stock", "stock", f"{warehouse_id}/{sku}", {"delta": delta, "reason": reason}, approved_by)
        return s

    def transfer_stock(self, from_wh: str, to_wh: str, sku: str, qty: int, actor: str, approved_by: str) -> dict:
        qty = _whole(qty, "transfer quantity")
        if qty <= 0: raise ValueError("transfer quantity must be positive")
        if from_wh == to_wh: raise ValueError("transfer needs two different godowns")
        with immediate_tx(self):
            self.get_warehouse(from_wh); self.get_warehouse(to_wh); self.get_product(sku)
            avail = self.get_stock(from_wh, sku).available
            if avail < qty:
                raise InsufficientStockError(f"only {avail} {sku} available at {from_wh}")
            ref = new_id("TRF")
            # the cost pool is per product across godowns, so a transfer moves units, not value
            self.move_stock(from_wh, sku, -qty, "transfer_out", ref, value_paisa=0)
            self.move_stock(to_wh, sku, qty, "transfer_in", ref, value_paisa=0)
            self.audit(actor, "transfer_stock", "stock", ref, {"from": from_wh, "to": to_wh, "sku": sku, "qty": qty}, approved_by)
        return {"transfer_id": ref, "from": from_wh, "to": to_wh, "sku": sku, "qty": qty}

    def stock_moves(self, sku: str | None = None, warehouse_id: str | None = None, limit: int = 200) -> list[StockMove]:
        conds, a = [], []
        if sku: conds.append("sku=?"); a.append(sku)
        if warehouse_id: conds.append("warehouse_id=?"); a.append(warehouse_id)
        q = f"SELECT {_MOVE_COLS} FROM stock_moves" + (" WHERE " + " AND ".join(conds) if conds else "") + " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        a.append(limit)
        return [StockMove(**dict(r)) for r in self._all(q, tuple(a))]

    def replay_stock_ledger(self) -> dict[tuple[str, str], int]:
        """On-hand per (godown, sku) as the stock ledger says it should be: the sum of every move."""
        return {(r["warehouse_id"], r["sku"]): int(r["q"]) for r in self._all("SELECT warehouse_id, sku, SUM(delta) q FROM stock_moves GROUP BY warehouse_id, sku")}

    def low_stock(self) -> list[dict]:
        thresholds = {p.sku: p.min_stock for p in self.list_products()}
        return [{"warehouse_id": s.warehouse_id, "sku": s.sku, "available": s.available, "min_stock": thresholds.get(s.sku, 10)}
                for s in self.list_stock() if s.sku in thresholds and s.available <= thresholds[s.sku]]

    # ------------------------------------------------------------ id helpers
    def _next_code(self, prefix: str, table: str, col: str, width: int = 3) -> str:
        rows = self._all(f"SELECT {col} FROM {table}")
        nums = []
        for r in rows:
            tail = str(r[0]).rsplit("-", 1)[-1]
            if tail.isdigit(): nums.append(int(tail))
        return f"{prefix}-{(max(nums) + 1 if nums else 1):0{width}d}"

    @staticmethod
    def _slug(name: str) -> str:
        import re
        s = re.sub(r"[^A-Za-z0-9]+", "-", name.strip().upper()).strip("-")
        return s[:16] or "X"
