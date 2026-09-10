"""Master data and settings: customers, products, suppliers, godowns, routes,
vehicles, business settings, Excel import/template."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from munshi.documents.excel import import_xlsx, template_xlsx
from munshi.domain.models import Customer, Product, Route, Supplier, Vehicle, Warehouse
from munshi.web.deps import Ctx, context

router = APIRouter(prefix="/api", tags=["setup"])
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class CustomerIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(default="", max_length=20)
    address: str = Field(default="", max_length=160)
    route_id: str | None = None
    tier: str = Field(default="standard", pattern="^(standard|wholesale|vip)$")
    credit_limit: float = Field(default=0, ge=0)
    credit_days: int = Field(default=30, ge=0, le=365)
    discount_pct: float = Field(default=0, ge=0, le=50)
    language: str = Field(default="ur-en", pattern="^(ur-en|en)$")
    active: bool = True
    opening_balance: float = Field(default=0, ge=0)


class ProductIn(BaseModel):
    sku: str = Field(default="", max_length=24)
    name: str = Field(min_length=2, max_length=80)
    unit_price: float = Field(ge=0)
    cost_price: float = Field(default=0, ge=0)
    unit: str = Field(default="bag", max_length=12)
    category: str = Field(default="", max_length=40)
    aliases: list[str] = []
    units_per_load: int = Field(default=1, ge=1)
    min_stock: int = Field(default=10, ge=0)
    active: bool = True


class SupplierIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(default="", max_length=20)
    address: str = Field(default="", max_length=160)
    opening_balance: float = Field(default=0, ge=0)


class WarehouseIn(BaseModel):
    warehouse_id: str = Field(default="", max_length=20)
    name: str = Field(min_length=2, max_length=60)


class RouteIn(BaseModel):
    route_id: str = Field(default="", max_length=24)
    name: str = Field(min_length=2, max_length=60)
    warehouse_id: str
    stop_customer_ids: list[str] = []


class VehicleIn(BaseModel):
    vehicle_id: str = Field(default="", max_length=12)
    plate: str = Field(min_length=2, max_length=16)
    capacity_class: str = Field(default="small", pattern="^(small|medium|large)$")
    capacity_units: int = Field(ge=1, le=100000)


class SettingsIn(BaseModel):
    business_name: str | None = Field(default=None, min_length=2, max_length=80)
    city: str | None = Field(default=None, max_length=60)
    phone: str | None = Field(default=None, max_length=20)
    owner_phone: str | None = Field(default=None, max_length=20)
    credit_days: int | None = Field(default=None, ge=0, le=365)
    invoice_prefix: str | None = Field(default=None, min_length=1, max_length=6, pattern="^[A-Za-z]+$")
    language: str | None = Field(default=None, pattern="^(en|ur)$")
    digest_time: str | None = Field(default=None, pattern="^([01]\\d|2[0-3]):[0-5]\\d$")
    default_warehouse: str | None = None
    big_order_limit: float | None = Field(default=None, ge=0)


def _cust(c: Ctx, x: Customer) -> dict:
    return asdict(x) | {"outstanding": c.repo.outstanding(x.customer_id)}


# ---------------------------------------------------------------- customers
@router.get("/customers")
def customers(q: str = "", include_inactive: bool = False, c: Ctx = Depends(context("customers:read"))):
    rows = c.repo.search_customers(q) if q else c.repo.list_customers(include_inactive)
    if c.role == "driver":       # a driver sees names and addresses, never balances
        return [{"customer_id": x.customer_id, "name": x.name, "phone": x.phone, "address": x.address, "route_id": x.route_id} for x in rows]
    return [_cust(c, x) for x in rows]


@router.post("/customers", status_code=201)
def add_customer(body: CustomerIn, c: Ctx = Depends(context("customers:write"))):
    if body.route_id:
        c.repo.get_route(body.route_id)
    x = c.repo.upsert_customer(Customer("", body.name, body.phone, body.tier, body.credit_limit, body.route_id, body.language, body.address, body.discount_pct, body.credit_days, body.active))
    c.repo.audit(c.role, "customer_added", "customer", x.customer_id, {"name": x.name}, approved_by=c.signature)
    if body.opening_balance > 0:
        c.repo.opening_balance(x.customer_id, body.opening_balance, c.role, c.signature)
    return _cust(c, x)


@router.patch("/customers/{customer_id}")
def edit_customer(customer_id: str, body: CustomerIn, c: Ctx = Depends(context("customers:write"))):
    old = c.repo.get_customer(customer_id)
    if body.route_id: c.repo.get_route(body.route_id)
    x = c.repo.upsert_customer(Customer(old.customer_id, body.name, body.phone, body.tier, body.credit_limit, body.route_id, body.language, body.address, body.discount_pct, body.credit_days, body.active))
    changed = {k: v for k, v in asdict(x).items() if asdict(old).get(k) != v}
    c.repo.audit(c.role, "customer_updated", "customer", customer_id, changed, approved_by=c.signature)
    return _cust(c, x)


# ---------------------------------------------------------------- products
@router.get("/products")
def products(include_inactive: bool = False, c: Ctx = Depends(context("customers:read"))):
    rows = [asdict(p) for p in c.repo.list_products(include_inactive)]
    if c.role in ("driver", "salesman"):
        for r in rows: r.pop("cost_price", None)
    return rows


@router.post("/products", status_code=201)
def add_product(body: ProductIn, c: Ctx = Depends(context("setup:write"))):
    p = c.repo.upsert_product(Product(body.sku.strip().upper(), body.name, body.unit_price, body.aliases, body.units_per_load, body.cost_price, body.unit, body.category, body.min_stock, body.active))
    c.repo.audit(c.role, "product_added", "product", p.sku, {"name": p.name, "unit_price": p.unit_price}, approved_by=c.signature)
    return asdict(p)


@router.patch("/products/{sku}")
def edit_product(sku: str, body: ProductIn, c: Ctx = Depends(context("setup:write"))):
    old = c.repo.get_product(sku)
    p = c.repo.upsert_product(Product(old.sku, body.name, body.unit_price, body.aliases, body.units_per_load, body.cost_price, body.unit, body.category, body.min_stock, body.active))
    changed = {k: v for k, v in asdict(p).items() if asdict(old).get(k) != v}
    c.repo.audit(c.role, "product_updated", "product", sku, changed, approved_by=c.signature)
    return asdict(p)


# ---------------------------------------------------------------- suppliers
@router.get("/suppliers")
def suppliers(c: Ctx = Depends(context("purchases:read"))):
    return [asdict(s) | {"balance": c.repo.supplier_balance(s.supplier_id)} for s in c.repo.list_suppliers()]


@router.post("/suppliers", status_code=201)
def add_supplier(body: SupplierIn, c: Ctx = Depends(context("purchases:write"))):
    s = c.repo.upsert_supplier(Supplier("", body.name, body.phone, body.address))
    c.repo.audit(c.role, "supplier_added", "supplier", s.supplier_id, {"name": s.name}, approved_by=c.signature)
    if body.opening_balance > 0:
        from munshi.domain.models import now_iso
        from munshi.domain.repository import new_id
        with c.repo._tx() as cur:
            cur.execute("INSERT INTO supplier_ledger VALUES (?,?,?,?,?,?,?)", (new_id("BIL"), s.supplier_id, "bill", body.opening_balance, "opening balance", "", now_iso()))
    return asdict(s) | {"balance": c.repo.supplier_balance(s.supplier_id)}


@router.patch("/suppliers/{supplier_id}")
def edit_supplier(supplier_id: str, body: SupplierIn, c: Ctx = Depends(context("purchases:write"))):
    old = c.repo.get_supplier(supplier_id)
    s = c.repo.upsert_supplier(Supplier(old.supplier_id, body.name, body.phone, body.address))
    c.repo.audit(c.role, "supplier_updated", "supplier", supplier_id, {"name": s.name}, approved_by=c.signature)
    return asdict(s) | {"balance": c.repo.supplier_balance(s.supplier_id)}


# ---------------------------------------------------------------- godowns, routes, vehicles
@router.get("/warehouses")
def warehouses(c: Ctx = Depends(context("customers:read"))):
    return [asdict(w) | {"default": w.warehouse_id == c.repo.setting("default_warehouse")} for w in c.repo.list_warehouses()]


@router.post("/warehouses", status_code=201)
def add_warehouse(body: WarehouseIn, c: Ctx = Depends(context("setup:write"))):
    w = c.repo.upsert_warehouse(Warehouse(body.warehouse_id.strip().upper(), body.name))
    if not c.repo.setting("default_warehouse"): c.repo.set_setting("default_warehouse", w.warehouse_id)
    c.repo.audit(c.role, "warehouse_added", "warehouse", w.warehouse_id, {"name": w.name}, approved_by=c.signature)
    return asdict(w)


@router.get("/routes")
def routes(c: Ctx = Depends(context("customers:read"))):
    return [asdict(r) for r in c.repo.list_routes()]


@router.post("/routes", status_code=201)
def add_route(body: RouteIn, c: Ctx = Depends(context("setup:write"))):
    c.repo.get_warehouse(body.warehouse_id)
    for cid in body.stop_customer_ids: c.repo.get_customer(cid)
    r = c.repo.upsert_route(Route(body.route_id.strip().upper(), body.name, body.warehouse_id, body.stop_customer_ids))
    for cid in body.stop_customer_ids:      # putting a customer on a route assigns them to it
        cust = c.repo.get_customer(cid)
        if cust.route_id != r.route_id:
            cust.route_id = r.route_id; c.repo.upsert_customer(cust)
    c.repo.audit(c.role, "route_saved", "route", r.route_id, {"name": r.name, "stops": len(r.stop_customer_ids)}, approved_by=c.signature)
    return asdict(r)


@router.delete("/routes/{route_id}")
def del_route(route_id: str, c: Ctx = Depends(context("setup:write"))):
    c.repo.get_route(route_id); c.repo.delete_route(route_id)
    c.repo.audit(c.role, "route_deleted", "route", route_id, {}, approved_by=c.signature)
    return {"ok": True}


@router.get("/vehicles")
def vehicles(c: Ctx = Depends(context("dispatch:read"))):
    return [asdict(v) for v in c.repo.list_vehicles()]


@router.post("/vehicles", status_code=201)
def add_vehicle(body: VehicleIn, c: Ctx = Depends(context("setup:write"))):
    v = c.repo.upsert_vehicle(Vehicle(body.vehicle_id.strip().upper(), body.plate, body.capacity_class, body.capacity_units))
    c.repo.audit(c.role, "vehicle_saved", "vehicle", v.vehicle_id, {"plate": v.plate, "capacity": v.capacity_units}, approved_by=c.signature)
    return asdict(v)


@router.delete("/vehicles/{vehicle_id}")
def del_vehicle(vehicle_id: str, c: Ctx = Depends(context("setup:write"))):
    c.repo.get_vehicle(vehicle_id); c.repo.delete_vehicle(vehicle_id)
    c.repo.audit(c.role, "vehicle_deleted", "vehicle", vehicle_id, {}, approved_by=c.signature)
    return {"ok": True}


# ---------------------------------------------------------------- settings
@router.get("/settings")
def get_settings(c: Ctx = Depends(context("settings:write"))):
    return c.repo.settings()


@router.patch("/settings")
def patch_settings(body: SettingsIn, c: Ctx = Depends(context("settings:write"))):
    changes = body.model_dump(exclude_none=True)
    if "default_warehouse" in changes and changes["default_warehouse"]:
        c.repo.get_warehouse(changes["default_warehouse"])
    for k, v in changes.items():
        c.repo.set_setting(k, str(v))
    c.repo.audit(c.role, "settings_updated", "settings", "business", changes, approved_by=c.signature)
    return c.repo.settings()


# ---------------------------------------------------------------- import
@router.get("/import/template.xlsx")
def import_template(c: Ctx = Depends(context("setup:write"))):
    return Response(template_xlsx(c.repo), media_type=XLSX, headers={"Content-Disposition": "attachment; filename=munshi-import-template.xlsx"})


@router.post("/import")
async def do_import(file: UploadFile = File(...), c: Ctx = Depends(context("setup:write"))):
    data = await file.read()
    if len(data) > 8 * 1024 * 1024: raise HTTPException(413, "workbook is too large (8 MB max)")
    try:
        return import_xlsx(c.repo, data, c.role)
    except Exception as e:
        raise HTTPException(400, f"couldn't read that workbook: {e}")
