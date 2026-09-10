"""Master data: customers, products, suppliers, godowns, routes, vehicles,
and the stock ledger (levels + every movement)."""
from __future__ import annotations

import json

from munshi.domain.models import Customer, Product, Route, StockLevel, StockMove, Supplier, Vehicle, Warehouse
from munshi.domain.repository.base import InsufficientStockError, NotFoundError, RepositoryBase, new_id


class MasterDataMixin(RepositoryBase):
    # ------------------------------------------------------------ customers
    def upsert_customer(self, c: Customer) -> Customer:
        if not c.customer_id:
            c.customer_id = self._next_code("C", "customers", "customer_id")
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO customers (customer_id, name, phone, tier, credit_limit, route_id, language, address, discount_pct, credit_days, active) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (c.customer_id, c.name.strip(), c.phone.strip(), c.tier, float(c.credit_limit), c.route_id or None, c.language, c.address, float(c.discount_pct), int(c.credit_days), int(c.active)))
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

    @staticmethod
    def _customer(r) -> Customer:
        d = dict(r); d["active"] = bool(d.get("active", 1)); return Customer(**d)

    # ------------------------------------------------------------ products
    def upsert_product(self, p: Product) -> Product:
        if not p.sku:
            p.sku = self._next_code("P", "products", "sku")
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO products (sku, name, unit_price, aliases, units_per_load, cost_price, unit, category, min_stock, active) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (p.sku, p.name.strip(), float(p.unit_price), json.dumps(p.aliases), int(p.units_per_load), float(p.cost_price), p.unit, p.category, int(p.min_stock), int(p.active)))
        return p

    def get_product(self, sku: str) -> Product:
        r = self._one("SELECT * FROM products WHERE sku=?", (sku,))
        if not r: raise NotFoundError(f"no such product: {sku}")
        return self._product(r)

    def list_products(self, include_inactive: bool = False) -> list[Product]:
        q = "SELECT * FROM products" + ("" if include_inactive else " WHERE active=1") + " ORDER BY name"
        return [self._product(r) for r in self._all(q)]

    @staticmethod
    def _product(r) -> Product:
        d = dict(r); d["aliases"] = json.loads(d["aliases"] or "[]"); d["active"] = bool(d.get("active", 1)); return Product(**d)

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
        with self._tx() as cur:
            cur.execute("INSERT OR REPLACE INTO stock VALUES (?,?,?,?)", (warehouse_id, sku, int(on_hand), int(reserved)))

    def stock_by_sku(self, sku: str) -> list[StockLevel]:
        return [StockLevel(**dict(r)) for r in self._all("SELECT * FROM stock WHERE sku=?", (sku,))]

    def list_stock(self, warehouse_id: str | None = None) -> list[StockLevel]:
        q, a = ("SELECT * FROM stock WHERE warehouse_id=?", (warehouse_id,)) if warehouse_id else ("SELECT * FROM stock", ())
        return [StockLevel(**dict(r)) for r in self._all(q, a)]

    def move_stock(self, warehouse_id: str, sku: str, delta: int, kind: str, ref: str, allow_negative: bool = False) -> StockLevel:
        """The only way on_hand changes. Records the movement for the stock ledger."""
        s = self.get_stock(warehouse_id, sku)
        if s.on_hand + delta < 0 and not allow_negative:
            raise InsufficientStockError(f"{sku} at {warehouse_id} would go to {s.on_hand + delta}")
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO stock VALUES (?,?,?,?)", (warehouse_id, sku, s.on_hand + int(delta), s.reserved))
            m = StockMove(new_id("MOV"), warehouse_id, sku, int(delta), kind, ref)
            c.execute("INSERT INTO stock_moves VALUES (?,?,?,?,?,?,?)", (m.move_id, warehouse_id, sku, m.delta, kind, ref, m.created_at))
        return self.get_stock(warehouse_id, sku)

    def adjust_stock(self, warehouse_id: str, sku: str, delta: int, reason: str, actor: str, approved_by: str) -> StockLevel:
        self.get_product(sku); self.get_warehouse(warehouse_id)
        s = self.move_stock(warehouse_id, sku, delta, "adjust", reason[:80])
        self.audit(actor, "adjust_stock", "stock", f"{warehouse_id}/{sku}", {"delta": delta, "reason": reason}, approved_by)
        return s

    def transfer_stock(self, from_wh: str, to_wh: str, sku: str, qty: int, actor: str, approved_by: str) -> dict:
        if qty <= 0: raise ValueError("transfer quantity must be positive")
        if from_wh == to_wh: raise ValueError("transfer needs two different godowns")
        self.get_warehouse(from_wh); self.get_warehouse(to_wh); self.get_product(sku)
        if self.get_stock(from_wh, sku).available < qty:
            raise InsufficientStockError(f"only {self.get_stock(from_wh, sku).available} {sku} available at {from_wh}")
        ref = new_id("TRF")
        with self._tx():
            self.move_stock(from_wh, sku, -qty, "transfer_out", ref)
            self.move_stock(to_wh, sku, qty, "transfer_in", ref)
        self.audit(actor, "transfer_stock", "stock", ref, {"from": from_wh, "to": to_wh, "sku": sku, "qty": qty}, approved_by)
        return {"transfer_id": ref, "from": from_wh, "to": to_wh, "sku": sku, "qty": qty}

    def stock_moves(self, sku: str | None = None, warehouse_id: str | None = None, limit: int = 200) -> list[StockMove]:
        conds, a = [], []
        if sku: conds.append("sku=?"); a.append(sku)
        if warehouse_id: conds.append("warehouse_id=?"); a.append(warehouse_id)
        q = "SELECT * FROM stock_moves" + (" WHERE " + " AND ".join(conds) if conds else "") + " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        a.append(limit)
        return [StockMove(**dict(r)) for r in self._all(q, tuple(a))]

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
