"""Demo business: Sultan Traders, an agri-input distributor in Punjab.
Fictional — every name, phone and balance here is invented for the demo.
Seeds enough history (open invoices, overdue accounts, low stock, a
supplier bill, a few expenses) that every agent has real work waiting the
moment the app opens. `seed_new_business` is the empty starting point a
real business gets at signup."""
from __future__ import annotations

import json
from datetime import timedelta

from munshi.domain.models import Customer, Product, Route, Supplier, Vehicle, Warehouse, business_today, to_paisa
from munshi.domain.repository import MunshiRepository

PRODUCTS = [
    Product("UREA-50", "Urea 50kg", 3850.0, ["urea", "yuria", "یوریا"], cost_price=3600, category="fertilizer"),
    Product("DAP-50", "DAP 50kg", 6250.0, ["dap", "ڈی اے پی"], cost_price=5900, category="fertilizer"),
    Product("NPK-25", "NPK 25kg", 4100.0, ["npk", "این پی کے"], cost_price=3800, category="fertilizer"),
    Product("SOP-50", "SOP 50kg", 5900.0, ["sop", "potash", "پوٹاش"], cost_price=5500, category="fertilizer"),
    Product("ZINC-10", "Zinc Sulphate 10kg", 2600.0, ["zinc", "zinc sulphate", "زنک"], cost_price=2350, category="micronutrient"),
    Product("CYPER-1L", "Cypermethrin 1L", 1500.0, ["cypermethrin", "cyper", "spray"], cost_price=1250, unit="ltr", category="pesticide"),
    Product("IMIDA-250", "Imidacloprid 250ml", 950.0, ["imida", "imidacloprid"], cost_price=780, unit="pc", category="pesticide"),
    Product("SEED-MAIZE", "Hybrid Maize Seed 10kg", 7800.0, ["maize seed", "makai", "مکئی"], cost_price=7000, category="seed"),
    Product("SEED-WHEAT", "Wheat Seed 50kg", 5200.0, ["wheat seed", "gandum", "گندم"], cost_price=4700, category="seed"),
    Product("DRIP-100", "Drip Line 100m", 3200.0, ["drip", "drip line"], cost_price=2700, unit="pc", category="irrigation", min_stock=5),
]

WAREHOUSES = [Warehouse("WH-MULTAN", "Multan Godown"), Warehouse("WH-VEHARI", "Vehari Godown")]

CUSTOMERS = [
    Customer("C-001", "Malik Agro Store", "0300-1111001", "wholesale", 1_200_000, "R-MULTAN-N", address="Vehari Road, Multan", discount_pct=2),
    Customer("C-002", "Chaudhry Farms", "0300-1111002", "standard", 400_000, "R-MULTAN-N", address="Chak 5-Faiz, Multan"),
    Customer("C-003", "Green Valley Seeds", "0300-1111003", "vip", 2_000_000, "R-MULTAN-N", address="Bosan Road, Multan", discount_pct=3),
    Customer("C-004", "Al-Barakah Traders", "0300-1111004", "standard", 300_000, "R-MULTAN-S", address="Shujabad Road"),
    Customer("C-005", "Rana Brothers", "0300-1111005", "wholesale", 900_000, "R-MULTAN-S", address="Jalalpur Road", discount_pct=2),
    Customer("C-006", "Shalimar Agri Centre", "0300-1111006", "standard", 350_000, "R-MULTAN-S", address="Shershah"),
    Customer("C-007", "Bhatti Kisan Store", "0300-1111007", "standard", 250_000, "R-VEHARI", address="Vehari city"),
    Customer("C-008", "Punjab Seed Mart", "0300-1111008", "wholesale", 800_000, "R-VEHARI", address="Burewala Road, Vehari"),
    Customer("C-009", "Haji Sons", "0300-1111009", "standard", 200_000, "R-VEHARI", address="Mailsi"),
    Customer("C-010", "New Kisan Dost", "0300-1111010", "standard", 150_000, "R-VEHARI", address="Tibba Sultanpur", language="en"),
]

SUPPLIERS = [
    Supplier("S-001", "Fauji Fertilizer (Multan depot)", "061-4500001", "Industrial Estate, Multan"),
    Supplier("S-002", "Engro Fertilizers (Vehari)", "067-3300002", "Vehari"),
    Supplier("S-003", "Ali Akbar Group (pesticides)", "042-3500003", "Lahore"),
]

ROUTES = [
    Route("R-MULTAN-N", "Multan North", "WH-MULTAN", ["C-001", "C-002", "C-003"]),
    Route("R-MULTAN-S", "Multan South", "WH-MULTAN", ["C-004", "C-005", "C-006"]),
    Route("R-VEHARI", "Vehari Road", "WH-VEHARI", ["C-007", "C-008", "C-009", "C-010"]),
]

VEHICLES = [
    Vehicle("V-01", "MNK-4521", "small", 120),
    Vehicle("V-02", "MNK-7788", "medium", 260),
    Vehicle("V-03", "LEB-1130", "large", 480),
]

STOCK = {
    "WH-MULTAN": {"UREA-50": 420, "DAP-50": 180, "NPK-25": 95, "SOP-50": 60, "ZINC-10": 140,
                  "CYPER-1L": 8, "IMIDA-250": 210, "SEED-MAIZE": 45, "SEED-WHEAT": 0, "DRIP-100": 30},
    "WH-VEHARI": {"UREA-50": 150, "DAP-50": 40, "NPK-25": 30, "SOP-50": 12, "ZINC-10": 25,
                  "CYPER-1L": 60, "IMIDA-250": 70, "SEED-MAIZE": 10, "SEED-WHEAT": 120, "DRIP-100": 5},
}

# (customer, amount, days_ago_invoiced, paid) -- receivables history for Wasooli
HISTORY = [
    ("C-001", 385_000, 12, 0),
    ("C-002", 96_000, 48, 0),
    ("C-003", 620_000, 20, 620_000),
    ("C-004", 145_000, 75, 20_000),
    ("C-005", 410_000, 33, 150_000),
    ("C-007", 58_000, 9, 0),
    ("C-008", 312_000, 41, 0),
    ("C-009", 84_000, 95, 0),
    ("C-010", 39_000, 5, 39_000),
]

# What each HISTORY invoice sold, at list price (sku, qty); each adds up to its invoice exactly. Fertilizer-heavy,
# the way an agri distributor's book looks, so the demo's gross margin is the thin one such a business runs on
# (about 6%: urea 6.5%, DAP 5.6%, SOP 6.8% at the seed cost prices), not the 100% an uncosted invoice shows.
# Drip line, maize, wheat seed, zinc, NPK (outside the 41-day-old Punjab Seed Mart bill) and the pesticides sold
# nothing in the last 30 days, so slow stock still has something true to say.
HISTORY_LINES = [
    [("UREA-50", 100)],                         # 385,000
    [("UREA-50", 20), ("IMIDA-250", 20)],       # 77,000 + 19,000 = 96,000
    [("UREA-50", 75), ("DAP-50", 53)],          # 288,750 + 331,250 = 620,000
    [("UREA-50", 30), ("SOP-50", 5)],           # 115,500 + 29,500 = 145,000
    [("UREA-50", 100), ("DAP-50", 4)],          # 385,000 + 25,000 = 410,000
    [("UREA-50", 12), ("SOP-50", 2)],           # 46,200 + 11,800 = 58,000
    [("UREA-50", 64), ("NPK-25", 16)],          # 246,400 + 65,600 = 312,000
    [("DAP-50", 4), ("SOP-50", 10)],            # 25,000 + 59,000 = 84,000
    [("UREA-50", 4), ("SOP-50", 4)],            # 15,400 + 23,600 = 39,000
]
OPENING_DAYS_AGO = 100      # the opening stock count, before the oldest HISTORY invoice

DEMO_SETTINGS = {"business_name": "Sultan Traders", "city": "Multan", "phone": "061-4567890", "owner_phone": "0300-0000001",
                 "default_warehouse": "WH-MULTAN", "language": "en"}


def seed(repo: MunshiRepository) -> MunshiRepository:
    for k, v in DEMO_SETTINGS.items(): repo.set_setting(k, v)
    for p in PRODUCTS: repo.upsert_product(p)
    for w in WAREHOUSES: repo.upsert_warehouse(w)
    for c in CUSTOMERS: repo.upsert_customer(c)
    for s in SUPPLIERS: repo.upsert_supplier(s)
    for r in ROUTES: repo.upsert_route(r)
    for v in VEHICLES: repo.upsert_vehicle(v)
    # backdated history (pre-Munshi paper records, so no gapless numbers) so aging has teeth, margins are real and
    # best sellers / slow stock mean something. Written directly because it is backdated (the repository stamps
    # "now"), but by the repository's own rules: the opening count and every sale go through the stock ledger
    # (stock_moves) and the moving-average pool (inventory_value), and each sale leaves a sale_lines cost snapshot
    # at the average of the moment -- which is the cost price, since nothing else has moved the pool. What is left
    # on the shelf is exactly STOCK. Amounts are integer paisa like every stored amount.
    price = {p.sku: to_paisa(p.unit_price) for p in PRODUCTS}
    cost = {p.sku: to_paisa(p.cost_price) for p in PRODUCTS}
    godown = {c.customer_id: next(r.warehouse_id for r in ROUTES if r.route_id == c.route_id) for c in CUSTOMERS}
    sold: dict[tuple[str, str], int] = {}
    for (cust, amt, _, _), lines in zip(HISTORY, HISTORY_LINES, strict=True):
        if sum(q * price[sku] for sku, q in lines) != to_paisa(amt): raise ValueError(f"seed lines for {cust} don't add up to {amt}")
        for sku, q in lines: sold[(godown[cust], sku)] = sold.get((godown[cust], sku), 0) + q
    # "N days ago" counts Pakistan business days (never the host OS date, which is a day behind on a UTC server
    # from 00:00 to 05:00 PKT); the fixed UTC times below all fall on that same business day (10:00-21:00 PKT)
    today = business_today()
    move = ("INSERT INTO stock_moves (move_id, warehouse_id, sku, delta, kind, ref, created_at, value_paisa, order_id) VALUES (?,?,?,?,?,?,?,?,?)")
    with repo._tx() as conn:
        opened = f"{(today - timedelta(days=OPENING_DAYS_AGO)).isoformat()}T05:00:00+00:00"
        for wh, levels in STOCK.items():
            for sku, qty in levels.items():
                units = qty + sold.get((wh, sku), 0)
                conn.execute("INSERT INTO stock (warehouse_id, sku, on_hand, reserved) VALUES (?,?,?,0)", (wh, sku, units))
                if units:
                    conn.execute(move, (f"MOV-SEEDO-{wh[3:]}-{sku}", wh, sku, units, "opening", "opening stock count", opened, units * cost[sku], None))
                    conn.execute("INSERT INTO inventory_value (sku, value_paisa) VALUES (?, ?) ON CONFLICT(sku) DO UPDATE SET value_paisa = value_paisa + excluded.value_paisa",
                                 (sku, units * cost[sku]))
        for i, (cust, amt, days_ago, paid) in enumerate(HISTORY):
            inv_day = today - timedelta(days=days_ago)
            created = f"{inv_day.isoformat()}T10:00:00+00:00"
            due = (inv_day + timedelta(days=30)).isoformat()
            ref, wh = f"SEED-{i:02d}", godown[cust]
            conn.execute("INSERT INTO ledger (entry_id, customer_id, kind, amount, ref, due_date, created_at, method, received_by) VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"INV-SEED{i:02d}", cust, "invoice", to_paisa(amt), ref, due, created, "", ""))
            # the paper delivery behind the bill: a closed stop on no Munshi plan (sale_lines needs a stop to point at)
            lines = HISTORY_LINES[i]
            conn.execute("INSERT INTO stops (stop_id, plan_id, order_id, customer_id, sequence, status, delivered_items, returned_items, cash_collected, otp, otp_verified, closed_at, note) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (f"STP-SEED{i:02d}", None, ref, cust, 1, "delivered", json.dumps([{"sku": s, "qty": q} for s, q in lines]), "[]", 0, None, 0, created,
                          "pre-Munshi paper delivery"))
            for sku, q in lines:
                conn.execute(move, (f"MOV-SEED{i:02d}-{sku}", wh, sku, -q, "sale", ref, created, -q * cost[sku], ref))
                conn.execute("UPDATE stock SET on_hand = on_hand - ? WHERE warehouse_id=? AND sku=?", (q, wh, sku))
                conn.execute("UPDATE inventory_value SET value_paisa = value_paisa - ? WHERE sku=?", (q * cost[sku], sku))
                conn.execute("INSERT INTO sale_lines (stop_id, order_id, customer_id, invoice_id, sku, qty, revenue_paisa, cost_paisa, cost_basis, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (f"STP-SEED{i:02d}", ref, cust, f"INV-SEED{i:02d}", sku, q, q * price[sku], q * cost[sku], "moving_average", created))
            if paid:
                pay_day = inv_day + timedelta(days=min(max(days_ago - 1, 0), 15))
                conn.execute("INSERT INTO ledger (entry_id, customer_id, kind, amount, ref, due_date, created_at, method, received_by) VALUES (?,?,?,?,?,?,?,?,?)",
                             (f"PAY-SEED{i:02d}", cust, "payment", -to_paisa(paid), f"SEED-{i:02d}", None, f"{pay_day.isoformat()}T16:00:00+00:00", "bank", "office"))
        # a supplier bill partly paid, and a few expenses, so payables and the cashbook aren't empty
        sup = "INSERT INTO supplier_ledger (entry_id, supplier_id, kind, amount, ref, method, created_at) VALUES (?,?,?,?,?,?,?)"
        conn.execute(sup, ("BIL-SEED00", "S-001", "bill", to_paisa(1_440_000), "PUR-SEED00", "", f"{(today - timedelta(days=6)).isoformat()}T11:00:00+00:00"))
        conn.execute(sup, ("SPY-SEED00", "S-001", "payment", -to_paisa(900_000), "PUR-SEED00", "bank", f"{(today - timedelta(days=3)).isoformat()}T11:00:00+00:00"))
        for j, (cat, amt, note) in enumerate([("fuel", 8500, "V-01 diesel"), ("loading", 1200, "godown labour"), ("utilities", 6400, "godown electricity")]):
            conn.execute("INSERT INTO expenses (expense_id, category, amount, note, method, paid_by, expense_date, created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (f"EXP-SEED{j:02d}", cat, to_paisa(amt), note, "cash", "Bilal", (today - timedelta(days=j)).isoformat(), f"{(today - timedelta(days=j)).isoformat()}T09:00:00+00:00"))
    return repo


def seed_new_business(repo: MunshiRepository, name: str, city: str = "", phone: str = "", owner_phone: str = "", language: str = "en") -> MunshiRepository:
    """What a real business starts with: its name, one godown, one route. Everything else comes from Setup or an Excel import."""
    repo.set_setting("business_name", name); repo.set_setting("city", city); repo.set_setting("phone", phone)
    repo.set_setting("owner_phone", owner_phone); repo.set_setting("language", language)
    wh = repo.upsert_warehouse(Warehouse("WH-MAIN", f"{city or 'Main'} Godown"))
    repo.set_setting("default_warehouse", wh.warehouse_id)
    repo.upsert_route(Route("R-MAIN", "Main Route", wh.warehouse_id, []))
    return repo


def seeded_repository(db_path: str = ":memory:") -> MunshiRepository:
    return seed(MunshiRepository(db_path))
