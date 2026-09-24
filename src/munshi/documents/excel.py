"""Excel in, Excel out. A new business brings its customers, products, stock
and paper khata in one workbook; any business can export everything it
owns into one workbook at any time."""
from __future__ import annotations

import io
from dataclasses import asdict

from openpyxl import Workbook, load_workbook

from munshi.domain.models import Customer, Product, Supplier
from munshi.domain.repository import MunshiRepository

SHEETS = {
    "Customers": ["name", "phone", "address", "route_id", "credit_limit", "credit_days", "discount_pct", "tier", "language", "opening_balance"],
    "Products": ["sku", "name", "unit", "category", "unit_price", "cost_price", "aliases", "units_per_load", "min_stock"],
    "Stock": ["warehouse_id", "sku", "on_hand"],
    "Suppliers": ["name", "phone", "address", "opening_balance"],
}
EXAMPLES = {
    "Customers": ["Chaudhry Farms", "0300-1234567", "Chak 5, Multan", "R-MAIN", 400000, 30, 0, "standard", "ur-en", 96000],
    "Products": ["UREA-50", "Urea 50kg", "bag", "fertilizer", 3850, 3600, "urea, yuria", 1, 10],
    "Stock": ["WH-MAIN", "UREA-50", 420],
    "Suppliers": ["Fauji Fertilizer depot", "061-4500001", "Industrial Estate", 540000],
}


def template_xlsx(repo: MunshiRepository | None = None) -> bytes:
    wb = Workbook(); wb.remove(wb.active)
    examples = {k: list(v) for k, v in EXAMPLES.items()}
    if repo:      # example rows use ids that exist in THIS business
        routes, whs = repo.list_routes(), repo.list_warehouses()
        if routes: examples["Customers"][3] = routes[0].route_id
        if whs: examples["Stock"][0] = whs[0].warehouse_id
    for name, cols in SHEETS.items():
        ws = wb.create_sheet(name); ws.append(cols); ws.append(examples[name])
        for i, _ in enumerate(cols, 1): ws.column_dimensions[ws.cell(1, i).column_letter].width = 18
    notes = wb.create_sheet("How to")
    for line in ["Fill each sheet, delete the example row, and upload at Setup → Import.",
                 "Customers: route_id must exist (see Setup → Routes) or be blank. opening_balance is what they owe you today.",
                 "Products: sku is your own code; aliases are comma-separated names people say (Urdu welcome).",
                 "Stock: one row per godown × product. Godown ids are shown in Setup → Godowns.",
                 "Suppliers: opening_balance is what you owe them today.",
                 "Rows with a name that already exists are updated, not duplicated."]:
        notes.append([line])
    if repo:
        ws = wb.create_sheet("Your ids"); ws.append(["type", "id", "name"])
        for w in repo.list_warehouses(): ws.append(["godown", w.warehouse_id, w.name])
        for r in repo.list_routes(): ws.append(["route", r.route_id, r.name])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def _rows(ws) -> list[dict]:
    rows = list(ws.iter_rows(values_only=True))
    if not rows: return []
    header = [str(h).strip().lower() if h is not None else "" for h in rows[0]]
    out = []
    for r in rows[1:]:
        if r is None or all(v in (None, "") for v in r): continue
        out.append({header[i]: r[i] for i in range(min(len(header), len(r))) if header[i]})
    return out


def import_xlsx(repo: MunshiRepository, data: bytes, actor: str) -> dict:
    """Upsert everything in the workbook. Returns counts and per-row errors; never half-applies a row."""
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    result = {"customers": 0, "products": 0, "stock": 0, "suppliers": 0, "opening_balances": 0, "errors": []}
    existing_c = {c.name.lower(): c for c in repo.list_customers(include_inactive=True)}
    existing_s = {s.name.lower(): s for s in repo.list_suppliers()}
    if "Products" in wb.sheetnames:
        for i, r in enumerate(_rows(wb["Products"]), 2):
            try:
                if not r.get("sku") or not r.get("name"): raise ValueError("sku and name are required")
                aliases = [a.strip() for a in str(r.get("aliases") or "").split(",") if a.strip()]
                repo.upsert_product(Product(str(r["sku"]).strip().upper(), str(r["name"]), float(r.get("unit_price") or 0), aliases, int(r.get("units_per_load") or 1),
                                            float(r.get("cost_price") or 0), str(r.get("unit") or "bag"), str(r.get("category") or ""), int(r.get("min_stock") or 10)))
                result["products"] += 1
            except Exception as e: result["errors"].append(f"Products row {i}: {e}")
    if "Customers" in wb.sheetnames:
        routes = {x.route_id for x in repo.list_routes()}
        for i, r in enumerate(_rows(wb["Customers"]), 2):
            try:
                name = str(r.get("name") or "").strip()
                if not name: raise ValueError("name is required")
                prev = existing_c.get(name.lower())
                rid = str(r.get("route_id") or "").strip() or None
                if rid and rid not in routes: raise ValueError(f"route {rid} does not exist")
                c = Customer(prev.customer_id if prev else "", name, str(r.get("phone") or ""), str(r.get("tier") or "standard"), float(r.get("credit_limit") or 0), rid,
                             str(r.get("language") or "ur-en"), str(r.get("address") or ""), float(r.get("discount_pct") or 0), int(r.get("credit_days") or 30))
                repo.upsert_customer(c); result["customers"] += 1
                ob = float(r.get("opening_balance") or 0)
                if ob > 0 and not prev:
                    repo.opening_balance(c.customer_id, ob, actor); result["opening_balances"] += 1
            except Exception as e: result["errors"].append(f"Customers row {i}: {e}")
    if "Suppliers" in wb.sheetnames:
        for i, r in enumerate(_rows(wb["Suppliers"]), 2):
            try:
                name = str(r.get("name") or "").strip()
                if not name: raise ValueError("name is required")
                prev = existing_s.get(name.lower())
                s = repo.upsert_supplier(Supplier(prev.supplier_id if prev else "", name, str(r.get("phone") or ""), str(r.get("address") or "")))
                result["suppliers"] += 1
                ob = float(r.get("opening_balance") or 0)
                if ob > 0 and not prev:
                    repo.supplier_opening_balance(s.supplier_id, ob, actor)
            except Exception as e: result["errors"].append(f"Suppliers row {i}: {e}")
    if "Stock" in wb.sheetnames:
        whs = {w.warehouse_id for w in repo.list_warehouses()}
        for i, r in enumerate(_rows(wb["Stock"]), 2):
            try:
                wh = str(r.get("warehouse_id") or "").strip(); sku = str(r.get("sku") or "").strip().upper()
                if wh not in whs: raise ValueError(f"godown {wh} does not exist")
                repo.get_product(sku)
                cur = repo.get_stock(wh, sku).on_hand
                delta = int(r.get("on_hand") or 0) - cur
                if delta: repo.move_stock(wh, sku, delta, "adjust", "import")     # never below zero
                result["stock"] += 1
            except Exception as e: result["errors"].append(f"Stock row {i}: {e}")
    repo.audit(actor, "import_xlsx", "import", "workbook", {k: v for k, v in result.items() if k != "errors"} | {"errors": len(result["errors"])})
    return result


def export_xlsx(repo: MunshiRepository) -> bytes:
    wb = Workbook(); wb.remove(wb.active)

    def sheet(name, rows, header):
        ws = wb.create_sheet(name); ws.append(header)
        for r in rows: ws.append([r.get(h, "") if isinstance(r, dict) else r for h in header])
        for i, _ in enumerate(header, 1): ws.column_dimensions[ws.cell(1, i).column_letter].width = 16

    names = {c.customer_id: c.name for c in repo.list_customers(include_inactive=True)}
    sheet("Customers", [asdict(c) for c in repo.list_customers(include_inactive=True)], ["customer_id", "name", "phone", "address", "route_id", "tier", "credit_limit", "credit_days", "discount_pct", "language", "active"])
    sheet("Products", [asdict(p) | {"aliases": ", ".join(p.aliases)} for p in repo.list_products(include_inactive=True)], ["sku", "name", "unit", "category", "unit_price", "cost_price", "aliases", "units_per_load", "min_stock", "active"])
    sheet("Suppliers", [asdict(s) | {"balance": repo.supplier_balance(s.supplier_id)} for s in repo.list_suppliers()], ["supplier_id", "name", "phone", "address", "balance"])
    prods = {p.sku: p.name for p in repo.list_products(include_inactive=True)}
    sheet("Stock", [asdict(s) | {"available": s.available, "name": prods.get(s.sku, "")} for s in repo.list_stock()], ["warehouse_id", "sku", "name", "on_hand", "reserved", "available"])
    sheet("Orders", [asdict(o) | {"total": o.total, "customer": names.get(o.customer_id, ""), "items": ", ".join(f"{i.qty}×{i.sku}" for i in o.items)} for o in repo.list_orders(limit=5000)],
          ["order_id", "created_at", "customer", "items", "total", "status", "channel", "created_by"])
    led = []
    for cid, nm in names.items():
        for e in repo.ledger_for(cid): led.append(asdict(e) | {"customer": nm})
    sheet("Khata", sorted(led, key=lambda r: r["created_at"]), ["entry_id", "created_at", "customer", "kind", "amount", "method", "ref", "due_date", "received_by"])
    sheet("Aging", repo.aging(), ["customer_id", "name", "phone", "balance", "days_overdue", "bucket", "credit_limit"])
    sheet("Purchases", [asdict(p) | {"items": ", ".join(f"{i['qty']}×{i['sku']}@{i['unit_cost']}" for i in p.items)} for p in repo.list_purchases(5000)], ["purchase_id", "created_at", "supplier_id", "warehouse_id", "items", "total", "paid_amount", "invoice_ref"])
    sheet("Expenses", [asdict(x) for x in repo.expenses_between("2000-01-01", "2999-12-31")], ["expense_id", "expense_date", "category", "amount", "method", "note", "paid_by"])
    sheet("StockMoves", [m.__dict__ for m in repo.stock_moves(limit=20000)], ["created_at", "warehouse_id", "sku", "delta", "kind", "ref"])
    sheet("Audit", repo.audit_log(5000), ["created_at", "actor", "user", "action", "entity", "entity_id", "approved_by"])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()
