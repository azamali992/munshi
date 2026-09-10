"""Demo business: Sultan Traders, an agri-input distributor in Punjab.
Fictional — every name, phone and balance here is invented for the demo.
Seeds enough history (open invoices, overdue accounts, low stock, a
supplier bill, a few expenses) that every agent has real work waiting the
moment the app opens. `seed_new_business` is the empty starting point a
real business gets at signup."""
from __future__ import annotations

from datetime import date, timedelta

from munshi.domain.models import Customer, Product, Route, Supplier, Vehicle, Warehouse
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
    for wh, levels in STOCK.items():
        for sku, qty in levels.items():
            repo.set_stock(wh, sku, qty, 0)
    # backdated ledger so aging has teeth
    conn = repo._conn
    for i, (cust, amt, days_ago, paid) in enumerate(HISTORY):
        inv_day = date.today() - timedelta(days=days_ago)
        created = f"{inv_day.isoformat()}T10:00:00+00:00"
        due = (inv_day + timedelta(days=30)).isoformat()
        conn.execute("INSERT INTO ledger (entry_id, customer_id, kind, amount, ref, due_date, created_at, method, received_by) VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"INV-SEED{i:02d}", cust, "invoice", float(amt), f"SEED-{i:02d}", due, created, "", ""))
        if paid:
            pay_day = inv_day + timedelta(days=min(max(days_ago - 1, 0), 15))
            conn.execute("INSERT INTO ledger (entry_id, customer_id, kind, amount, ref, due_date, created_at, method, received_by) VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"PAY-SEED{i:02d}", cust, "payment", -float(paid), f"SEED-{i:02d}", None, f"{pay_day.isoformat()}T16:00:00+00:00", "bank", "office"))
    # a supplier bill partly paid, and a few expenses, so payables and the cashbook aren't empty
    conn.execute("INSERT INTO supplier_ledger VALUES (?,?,?,?,?,?,?)", ("BIL-SEED00", "S-001", "bill", 1_440_000.0, "PUR-SEED00", "", f"{(date.today() - timedelta(days=6)).isoformat()}T11:00:00+00:00"))
    conn.execute("INSERT INTO supplier_ledger VALUES (?,?,?,?,?,?,?)", ("SPY-SEED00", "S-001", "payment", -900_000.0, "PUR-SEED00", "bank", f"{(date.today() - timedelta(days=3)).isoformat()}T11:00:00+00:00"))
    for j, (cat, amt, note) in enumerate([("fuel", 8500, "V-01 diesel"), ("loading", 1200, "godown labour"), ("utilities", 6400, "godown electricity")]):
        conn.execute("INSERT INTO expenses VALUES (?,?,?,?,?,?,?,?)", (f"EXP-SEED{j:02d}", cat, float(amt), note, "cash", "Bilal", (date.today() - timedelta(days=j)).isoformat(), f"{(date.today() - timedelta(days=j)).isoformat()}T09:00:00+00:00"))
    conn.commit()
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
