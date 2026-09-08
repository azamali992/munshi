"""Demo business: Sultan Traders, an agri-input distributor in Punjab.
Fictional — every name, phone and balance here is invented for the demo.
Seeds enough history (open invoices, overdue accounts, low stock) that every
agent has real work waiting the moment the app opens."""
from __future__ import annotations

from datetime import date, timedelta

from munshi.domain.models import Customer, Product, Route, Vehicle, Warehouse
from munshi.domain.repository import MunshiRepository

PRODUCTS = [
    Product("UREA-50", "Urea 50kg", 3850.0, ["urea", "yuria", "یوریا"]),
    Product("DAP-50", "DAP 50kg", 6250.0, ["dap", "ڈی اے پی"]),
    Product("NPK-25", "NPK 25kg", 4100.0, ["npk", "این پی کے"]),
    Product("SOP-50", "SOP 50kg", 5900.0, ["sop", "potash", "پوٹاش"]),
    Product("ZINC-10", "Zinc Sulphate 10kg", 2600.0, ["zinc", "zinc sulphate", "زنک"]),
    Product("CYPER-1L", "Cypermethrin 1L", 1500.0, ["cypermethrin", "cyper", "spray"]),
    Product("IMIDA-250", "Imidacloprid 250ml", 950.0, ["imida", "imidacloprid"]),
    Product("SEED-MAIZE", "Hybrid Maize Seed 10kg", 7800.0, ["maize seed", "makai", "مکئی"]),
    Product("SEED-WHEAT", "Wheat Seed 50kg", 5200.0, ["wheat seed", "gandum", "گندم"]),
    Product("DRIP-100", "Drip Line 100m", 3200.0, ["drip", "drip line"]),
]

WAREHOUSES = [Warehouse("WH-MULTAN", "Multan Godown"), Warehouse("WH-VEHARI", "Vehari Godown")]

CUSTOMERS = [
    Customer("C-001", "Malik Agro Store", "0300-1111001", "wholesale", 1_200_000, "R-MULTAN-N"),
    Customer("C-002", "Chaudhry Farms", "0300-1111002", "standard", 400_000, "R-MULTAN-N"),
    Customer("C-003", "Green Valley Seeds", "0300-1111003", "vip", 2_000_000, "R-MULTAN-N"),
    Customer("C-004", "Al-Barakah Traders", "0300-1111004", "standard", 300_000, "R-MULTAN-S"),
    Customer("C-005", "Rana Brothers", "0300-1111005", "wholesale", 900_000, "R-MULTAN-S"),
    Customer("C-006", "Shalimar Agri Centre", "0300-1111006", "standard", 350_000, "R-MULTAN-S"),
    Customer("C-007", "Bhatti Kisan Store", "0300-1111007", "standard", 250_000, "R-VEHARI"),
    Customer("C-008", "Punjab Seed Mart", "0300-1111008", "wholesale", 800_000, "R-VEHARI"),
    Customer("C-009", "Haji Sons", "0300-1111009", "standard", 200_000, "R-VEHARI"),
    Customer("C-010", "New Kisan Dost", "0300-1111010", "standard", 150_000, "R-VEHARI"),
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


def seed(repo: MunshiRepository) -> MunshiRepository:
    for p in PRODUCTS: repo.upsert_product(p)
    for w in WAREHOUSES: repo.upsert_warehouse(w)
    for c in CUSTOMERS: repo.upsert_customer(c)
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
        conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?)",
                     (f"INV-SEED{i:02d}", cust, "invoice", float(amt), f"SEED-{i:02d}", due, created))
        if paid:
            pay_day = inv_day + timedelta(days=min(days_ago, 15))
            conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?)",
                         (f"PAY-SEED{i:02d}", cust, "payment", -float(paid), f"SEED-{i:02d}", None, f"{pay_day.isoformat()}T16:00:00+00:00"))
    conn.commit()
    return repo


def seeded_repository(db_path: str = ":memory:") -> MunshiRepository:
    return seed(MunshiRepository(db_path))
