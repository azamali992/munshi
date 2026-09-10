"""Versioned schema migrations for a business's SQLite file. Every open
applies whatever is missing, in order, inside one transaction per step.
Never edit an applied step — add a new one."""
from __future__ import annotations

import sqlite3

V1 = """
CREATE TABLE IF NOT EXISTS customers (customer_id TEXT PRIMARY KEY, name TEXT, phone TEXT, tier TEXT, credit_limit REAL, route_id TEXT, language TEXT);
CREATE TABLE IF NOT EXISTS products (sku TEXT PRIMARY KEY, name TEXT, unit_price REAL, aliases TEXT, units_per_load INTEGER);
CREATE TABLE IF NOT EXISTS warehouses (warehouse_id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS stock (warehouse_id TEXT, sku TEXT, on_hand INTEGER, reserved INTEGER, PRIMARY KEY (warehouse_id, sku));
CREATE TABLE IF NOT EXISTS routes (route_id TEXT PRIMARY KEY, name TEXT, warehouse_id TEXT, stop_customer_ids TEXT);
CREATE TABLE IF NOT EXISTS vehicles (vehicle_id TEXT PRIMARY KEY, plate TEXT, capacity_class TEXT, capacity_units INTEGER);
CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, customer_id TEXT, items TEXT, status TEXT, channel TEXT, source_text TEXT, created_at TEXT, warehouse_id TEXT);
CREATE TABLE IF NOT EXISTS dispatch_plans (plan_id TEXT PRIMARY KEY, plan_date TEXT, route_id TEXT, vehicle_id TEXT, warehouse_id TEXT, order_ids TEXT, status TEXT, load_units INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS stops (stop_id TEXT PRIMARY KEY, plan_id TEXT, order_id TEXT, customer_id TEXT, sequence INTEGER, status TEXT, delivered_items TEXT, returned_items TEXT, cash_collected REAL, otp TEXT, otp_verified INTEGER, closed_at TEXT);
CREATE TABLE IF NOT EXISTS deposits (deposit_id TEXT PRIMARY KEY, plan_id TEXT, amount_counted REAL, counted_by TEXT, deposited_at TEXT);
CREATE TABLE IF NOT EXISTS ledger (entry_id TEXT PRIMARY KEY, customer_id TEXT, kind TEXT, amount REAL, ref TEXT, due_date TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS reminders (reminder_id TEXT PRIMARY KEY, customer_id TEXT, tier TEXT, amount_due REAL, days_overdue INTEGER, message TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS promises (promise_id TEXT PRIMARY KEY, customer_id TEXT, amount REAL, promised_date TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit (audit_id TEXT PRIMARY KEY, actor TEXT, action TEXT, entity TEXT, entity_id TEXT, approved_by TEXT, payload TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS chat (msg_id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, text TEXT, meta TEXT, created_at TEXT);
"""

V2 = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
ALTER TABLE customers ADD COLUMN address TEXT DEFAULT '';
ALTER TABLE customers ADD COLUMN discount_pct REAL DEFAULT 0;
ALTER TABLE customers ADD COLUMN credit_days INTEGER DEFAULT 30;
ALTER TABLE customers ADD COLUMN active INTEGER DEFAULT 1;
ALTER TABLE products ADD COLUMN cost_price REAL DEFAULT 0;
ALTER TABLE products ADD COLUMN unit TEXT DEFAULT 'bag';
ALTER TABLE products ADD COLUMN category TEXT DEFAULT '';
ALTER TABLE products ADD COLUMN min_stock INTEGER DEFAULT 10;
ALTER TABLE products ADD COLUMN active INTEGER DEFAULT 1;
ALTER TABLE orders ADD COLUMN discount_pct REAL DEFAULT 0;
ALTER TABLE orders ADD COLUMN notes TEXT DEFAULT '';
ALTER TABLE orders ADD COLUMN created_by TEXT DEFAULT '';
ALTER TABLE ledger ADD COLUMN method TEXT DEFAULT '';
ALTER TABLE ledger ADD COLUMN received_by TEXT DEFAULT '';
ALTER TABLE audit ADD COLUMN user TEXT DEFAULT '';
CREATE TABLE IF NOT EXISTS suppliers (supplier_id TEXT PRIMARY KEY, name TEXT, phone TEXT, address TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS purchases (purchase_id TEXT PRIMARY KEY, supplier_id TEXT, warehouse_id TEXT, items TEXT, total REAL, invoice_ref TEXT, paid_amount REAL, created_at TEXT);
CREATE TABLE IF NOT EXISTS supplier_ledger (entry_id TEXT PRIMARY KEY, supplier_id TEXT, kind TEXT, amount REAL, ref TEXT, method TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS expenses (expense_id TEXT PRIMARY KEY, category TEXT, amount REAL, note TEXT, method TEXT, paid_by TEXT, expense_date TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS stock_moves (move_id TEXT PRIMARY KEY, warehouse_id TEXT, sku TEXT, delta INTEGER, kind TEXT, ref TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS approvals (approval_id TEXT PRIMARY KEY, thread_id TEXT, specialist TEXT, tool TEXT, args TEXT, tier TEXT, needs_role TEXT, requested_by_role TEXT, requested_by TEXT, status TEXT, resolved_by TEXT, note TEXT, created_at TEXT, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS notifications (notif_id TEXT PRIMARY KEY, for_role TEXT, kind TEXT, text TEXT, ref TEXT, read INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS outbox (msg_id TEXT PRIMARY KEY, channel TEXT, to_phone TEXT, text TEXT, status TEXT, ref TEXT, created_at TEXT, sent_at TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS ix_ledger_customer ON ledger(customer_id, created_at);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status, created_at);
CREATE INDEX IF NOT EXISTS ix_stops_plan ON stops(plan_id, sequence);
CREATE INDEX IF NOT EXISTS ix_audit_created ON audit(created_at);
CREATE INDEX IF NOT EXISTS ix_chat_thread ON chat(thread_id, created_at);
CREATE INDEX IF NOT EXISTS ix_moves_sku ON stock_moves(warehouse_id, sku, created_at);
"""

V3 = """
ALTER TABLE stops ADD COLUMN note TEXT DEFAULT '';
"""

MIGRATIONS: list[tuple[int, str]] = [(1, V1), (2, V2), (3, V3)]


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
    row = conn.execute("SELECT MAX(version) v FROM schema_version").fetchone()
    return int(row[0] or 0)


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply every migration above the file's version. Returns the versions applied."""
    applied = []
    have = current_version(conn)
    for version, sql in MIGRATIONS:
        if version <= have:
            continue
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                # a re-run against a file that already has the column (e.g. created by a dev build)
                if "duplicate column" not in str(e).lower():
                    raise
        conn.execute("INSERT INTO schema_version VALUES (?, datetime('now'))", (version,))
        conn.commit()
        applied.append(version)
    return applied
