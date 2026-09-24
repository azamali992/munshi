"""Versioned schema migrations for a business's SQLite file. Every open
applies whatever is missing, in order, each step inside ONE transaction
(BEGIN IMMEDIATE ... COMMIT, rolled back whole on any error, so a step can
never half-apply). Never edit an applied step — add a new one.

A step is either a SQL script (statements separated by ';') or a Python
callable taking the connection, for steps that must transform data (V5)."""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Callable

from munshi.domain.models import now_iso, sql_business_date, to_paisa

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

# Durable idempotency record for delivery-stop closes: one row per closed stop
# (PRIMARY KEY stop_id — a second close of the same stop cannot be recorded even
# if the status guard were bypassed) plus the client's idempotency key (UNIQUE
# client_ref — a key belongs to one close).
V4 = """
CREATE TABLE IF NOT EXISTS stop_closes (
 stop_id TEXT PRIMARY KEY REFERENCES stops(stop_id),
 client_ref TEXT UNIQUE CHECK (client_ref IS NULL OR length(client_ref) BETWEEN 1 AND 64),
 request_hash TEXT NOT NULL,
 result TEXT NOT NULL,
 created_at TEXT NOT NULL);
"""


# ============================================================================ V5
# Money you can trust.
#
#  1. Every money column becomes INTEGER paisa (1 rupee = 100 paisa), converted
#     with the one rounding rule (models.to_paisa, ROUND_HALF_UP). SQLite cannot
#     change a column's type in place, so each table is rebuilt: create
#     <t>__v5 with the new schema, copy + convert, DROP <t>, RENAME <t>__v5 -> <t>.
#     (That order, not "rename old first", so the stop_closes -> stops foreign
#     key keeps pointing at the right table.) Money columns carry
#     CHECK(typeof(col)='integer'): a fractional (rupee-float) write is refused.
#     Money inside JSON (orders.items unit_price, purchases.items unit_cost) is
#     rewritten to *_paisa keys.
#  2. Reversals: ledger / supplier_ledger / expenses / purchases get a
#     reversal_of column (+ a partial UNIQUE index: an entry is reversed at most once).
#  3. Cost snapshots: stock_moves.value_paisa (signed inventory value each move
#     added or removed, moving average) and stock_moves.order_id; sale_lines
#     (one row per delivered product per stop: qty, revenue, cost snapshot);
#     inventory_value (the moving-average pool per sku).
#  4. Gapless numbering: document_counters(kind, period) + doc_no columns.
#  5. Stock ledger reconciliation: one 'opening' stock move per (godown, sku)
#     whose on_hand disagreed with the sum of its recorded moves (set_stock used
#     to bypass the ledger), so replaying stock_moves reproduces stock exactly.
#  6. Guards in the database itself: on_hand may never be written below zero
#     (legacy negative rows may only move up), and ledger, supplier_ledger,
#     audit, stock_moves, sale_lines, expenses and purchases are append-only.
#
# Legacy data notes (documented, deliberate):
#  * pre-V5 documents keep their random ids and have doc_no NULL; numbering
#    starts at 000001 for the year V5 is applied in.
#  * pre-V5 sales have no cost snapshot; their sale_lines are backfilled at the
#    product's cost_price as it stands at migration (cost_basis='legacy_cost_price').
#  * opening inventory value = on_hand x cost_price at migration.

class MigrationError(Exception): ...


def _paisa(value, where: str) -> int:
    if value is None or value == "":
        return 0
    try:
        return to_paisa(value)
    except (ValueError, TypeError):
        raise MigrationError(f"{where}: {value!r} is not an amount; fix it by hand before upgrading") from None


_MONEY = "INTEGER NOT NULL DEFAULT 0 CHECK (typeof({c}) = 'integer')"

# table -> (columns in their existing order, money columns, DDL of the new table)
_REBUILD: dict[str, tuple[list[str], set[str], str]] = {
    "customers": (
        ["customer_id", "name", "phone", "tier", "credit_limit", "route_id", "language", "address", "discount_pct", "credit_days", "active"],
        {"credit_limit"},
        f"""(customer_id TEXT PRIMARY KEY, name TEXT, phone TEXT, tier TEXT, credit_limit {_MONEY.format(c='credit_limit')},
             route_id TEXT, language TEXT, address TEXT DEFAULT '', discount_pct REAL DEFAULT 0, credit_days INTEGER DEFAULT 30, active INTEGER DEFAULT 1)"""),
    "products": (
        ["sku", "name", "unit_price", "aliases", "units_per_load", "cost_price", "unit", "category", "min_stock", "active"],
        {"unit_price", "cost_price"},
        f"""(sku TEXT PRIMARY KEY, name TEXT, unit_price {_MONEY.format(c='unit_price')}, aliases TEXT, units_per_load INTEGER,
             cost_price {_MONEY.format(c='cost_price')}, unit TEXT DEFAULT 'bag', category TEXT DEFAULT '', min_stock INTEGER DEFAULT 10, active INTEGER DEFAULT 1)"""),
    "stops": (
        ["stop_id", "plan_id", "order_id", "customer_id", "sequence", "status", "delivered_items", "returned_items", "cash_collected", "otp", "otp_verified", "closed_at", "note"],
        {"cash_collected"},
        f"""(stop_id TEXT PRIMARY KEY, plan_id TEXT, order_id TEXT, customer_id TEXT, sequence INTEGER, status TEXT, delivered_items TEXT,
             returned_items TEXT, cash_collected {_MONEY.format(c='cash_collected')}, otp TEXT, otp_verified INTEGER, closed_at TEXT, note TEXT DEFAULT '')"""),
    "deposits": (
        ["deposit_id", "plan_id", "amount_counted", "counted_by", "deposited_at"],
        {"amount_counted"},
        f"(deposit_id TEXT PRIMARY KEY, plan_id TEXT, amount_counted {_MONEY.format(c='amount_counted')}, counted_by TEXT, deposited_at TEXT)"),
    "ledger": (
        ["entry_id", "customer_id", "kind", "amount", "ref", "due_date", "created_at", "method", "received_by"],
        {"amount"},
        f"""(entry_id TEXT PRIMARY KEY, customer_id TEXT, kind TEXT, amount {_MONEY.format(c='amount')}, ref TEXT, due_date TEXT, created_at TEXT,
             method TEXT DEFAULT '', received_by TEXT DEFAULT '',
             doc_no TEXT UNIQUE,
             reversal_of TEXT REFERENCES ledger(entry_id) CHECK (reversal_of IS NULL OR reversal_of <> entry_id))"""),
    "reminders": (
        ["reminder_id", "customer_id", "tier", "amount_due", "days_overdue", "message", "status", "created_at"],
        {"amount_due"},
        f"(reminder_id TEXT PRIMARY KEY, customer_id TEXT, tier TEXT, amount_due {_MONEY.format(c='amount_due')}, days_overdue INTEGER, message TEXT, status TEXT, created_at TEXT)"),
    "promises": (
        ["promise_id", "customer_id", "amount", "promised_date", "created_at"],
        {"amount"},
        f"(promise_id TEXT PRIMARY KEY, customer_id TEXT, amount {_MONEY.format(c='amount')}, promised_date TEXT, created_at TEXT)"),
    "purchases": (
        ["purchase_id", "supplier_id", "warehouse_id", "items", "total", "invoice_ref", "paid_amount", "created_at"],
        {"total", "paid_amount"},
        f"""(purchase_id TEXT PRIMARY KEY, supplier_id TEXT, warehouse_id TEXT, items TEXT, total {_MONEY.format(c='total')}, invoice_ref TEXT,
             paid_amount {_MONEY.format(c='paid_amount')}, created_at TEXT,
             doc_no TEXT UNIQUE,
             reversal_of TEXT REFERENCES purchases(purchase_id) CHECK (reversal_of IS NULL OR reversal_of <> purchase_id))"""),
    "supplier_ledger": (
        ["entry_id", "supplier_id", "kind", "amount", "ref", "method", "created_at"],
        {"amount"},
        f"""(entry_id TEXT PRIMARY KEY, supplier_id TEXT, kind TEXT, amount {_MONEY.format(c='amount')}, ref TEXT, method TEXT, created_at TEXT,
             reversal_of TEXT REFERENCES supplier_ledger(entry_id) CHECK (reversal_of IS NULL OR reversal_of <> entry_id))"""),
    "expenses": (
        ["expense_id", "category", "amount", "note", "method", "paid_by", "expense_date", "created_at"],
        {"amount"},
        f"""(expense_id TEXT PRIMARY KEY, category TEXT, amount {_MONEY.format(c='amount')}, note TEXT, method TEXT, paid_by TEXT, expense_date TEXT, created_at TEXT,
             reversal_of TEXT REFERENCES expenses(expense_id) CHECK (reversal_of IS NULL OR reversal_of <> expense_id))"""),
}


def _purchase_items_to_paisa(items: str | None, where: str) -> str:
    out = []
    for ln in json.loads(items or "[]"):
        out.append({"sku": ln["sku"], "qty": int(ln["qty"]), "unit_cost_paisa": _paisa(ln.get("unit_cost", ln.get("unit_cost_paisa")), where)})
    return json.dumps(out)


def _order_items_to_paisa(items: str | None, where: str) -> str:
    out = []
    for ln in json.loads(items or "[]"):
        out.append({"sku": ln["sku"], "qty": int(ln["qty"]), "unit_price_paisa": _paisa(ln.get("unit_price"), where)})
    return json.dumps(out)


def _rebuild(conn: sqlite3.Connection, table: str) -> None:
    cols, money, body = _REBUILD[table]
    have = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    extra = [c for c in have if c not in cols]
    missing = [c for c in cols if c not in have]
    if extra or missing:
        raise MigrationError(f"{table}: unexpected schema (extra columns {extra}, missing {missing}); refusing to rebuild it")
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
    new = f"{table}__v5"
    conn.execute(f"DROP TABLE IF EXISTS {new}")
    conn.execute(f"CREATE TABLE {new} {body}")
    converted = []
    for r in rows:
        vals = list(r)
        for i, c in enumerate(cols):
            if c in money:
                vals[i] = _paisa(vals[i], f"{table}.{c} of {vals[0]}")
        if table == "purchases":
            vals[cols.index("items")] = _purchase_items_to_paisa(vals[cols.index("items")], f"purchases.items of {vals[0]}")
        converted.append(vals)
    conn.executemany(f"INSERT INTO {new} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", converted)
    n_old = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    n_new = conn.execute(f"SELECT COUNT(*) FROM {new}").fetchone()[0]
    if n_old != n_new:
        raise MigrationError(f"{table}: copied {n_new} of {n_old} rows")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {new} RENAME TO {table}")


_APPEND_ONLY = ("ledger", "supplier_ledger", "audit", "stock_moves", "sale_lines", "expenses", "purchases")

V5_SQL = """
CREATE INDEX IF NOT EXISTS ix_ledger_customer ON ledger(customer_id, created_at);
CREATE INDEX IF NOT EXISTS ix_ledger_ref ON ledger(ref);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_reversal ON ledger(reversal_of) WHERE reversal_of IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_stops_plan ON stops(plan_id, sequence);
CREATE INDEX IF NOT EXISTS ix_stops_order ON stops(order_id);
CREATE INDEX IF NOT EXISTS ix_supplier_ledger_supplier ON supplier_ledger(supplier_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_supplier_ledger_reversal ON supplier_ledger(reversal_of) WHERE reversal_of IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_expenses_date ON expenses(expense_date);
CREATE UNIQUE INDEX IF NOT EXISTS ux_expenses_reversal ON expenses(reversal_of) WHERE reversal_of IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_purchases_supplier ON purchases(supplier_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_purchases_reversal ON purchases(reversal_of) WHERE reversal_of IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_deposits_plan ON deposits(plan_id);
CREATE INDEX IF NOT EXISTS ix_promises_customer ON promises(customer_id);

ALTER TABLE stock_moves ADD COLUMN value_paisa INTEGER NOT NULL DEFAULT 0 CHECK (typeof(value_paisa) = 'integer');
ALTER TABLE stock_moves ADD COLUMN order_id TEXT;
CREATE INDEX IF NOT EXISTS ix_moves_ref ON stock_moves(ref, order_id, sku);

CREATE TABLE IF NOT EXISTS document_counters (
 kind TEXT NOT NULL,
 period TEXT NOT NULL,
 last_value INTEGER NOT NULL CHECK (typeof(last_value) = 'integer' AND last_value >= 1),
 PRIMARY KEY (kind, period));

CREATE TABLE IF NOT EXISTS inventory_value (
 sku TEXT PRIMARY KEY,
 value_paisa INTEGER NOT NULL DEFAULT 0 CHECK (typeof(value_paisa) = 'integer' AND value_paisa >= 0));

CREATE TABLE IF NOT EXISTS sale_lines (
 line_id INTEGER PRIMARY KEY,
 stop_id TEXT NOT NULL REFERENCES stops(stop_id),
 order_id TEXT NOT NULL,
 customer_id TEXT NOT NULL,
 invoice_id TEXT REFERENCES ledger(entry_id),
 sku TEXT NOT NULL,
 qty INTEGER NOT NULL CHECK (typeof(qty) = 'integer' AND qty > 0),
 revenue_paisa INTEGER NOT NULL CHECK (typeof(revenue_paisa) = 'integer' AND revenue_paisa >= 0),
 cost_paisa INTEGER NOT NULL CHECK (typeof(cost_paisa) = 'integer' AND cost_paisa >= 0),
 cost_basis TEXT NOT NULL CHECK (cost_basis IN ('moving_average', 'legacy_cost_price')),
 created_at TEXT NOT NULL,
 UNIQUE (stop_id, sku));
CREATE INDEX IF NOT EXISTS ix_sale_lines_invoice ON sale_lines(invoice_id);
CREATE INDEX IF NOT EXISTS ix_stock_sku ON stock(sku)
""" + f""";
-- reports filter on the Pakistan business day of a UTC stamp: index that exact expression
CREATE INDEX IF NOT EXISTS ix_ledger_business_day ON ledger({sql_business_date('created_at')}, kind);
CREATE INDEX IF NOT EXISTS ix_sale_lines_business_day ON sale_lines({sql_business_date('created_at')})
"""

# (trigger bodies contain ';', so these are executed one by one, not split)
V5_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS stock_on_hand_insert_guard BEFORE INSERT ON stock
       WHEN NEW.on_hand < 0
       BEGIN SELECT RAISE(ABORT, 'stock on_hand cannot go below zero'); END""",
    # a legacy negative row (allowed before V5) may only move up towards zero
    """CREATE TRIGGER IF NOT EXISTS stock_on_hand_update_guard BEFORE UPDATE OF on_hand ON stock
       WHEN NEW.on_hand < 0 AND NEW.on_hand < OLD.on_hand
       BEGIN SELECT RAISE(ABORT, 'stock on_hand cannot go below zero'); END""",
]


def _v5(conn: sqlite3.Connection) -> None:
    for table in _REBUILD:
        _rebuild(conn, table)
    for oid, items in conn.execute("SELECT order_id, items FROM orders").fetchall():
        conn.execute("UPDATE orders SET items=? WHERE order_id=?", (_order_items_to_paisa(items, f"orders.items of {oid}"), oid))
    for stmt in [s.strip() for s in V5_SQL.split(";") if s.strip()]:
        conn.execute(stmt)
    for stmt in V5_TRIGGERS:
        conn.execute(stmt)
    stamp = now_iso()

    # ---- stock ledger reconciliation: stock_moves must replay to stock.on_hand
    on_hand = {(w, s): int(q or 0) for w, s, q in conn.execute("SELECT warehouse_id, sku, on_hand FROM stock")}
    moved = {(w, s): int(q or 0) for w, s, q in conn.execute("SELECT warehouse_id, sku, SUM(delta) FROM stock_moves GROUP BY warehouse_id, sku")}
    for key in sorted(set(on_hand) | set(moved)):
        gap = on_hand.get(key, 0) - moved.get(key, 0)
        if gap:
            conn.execute("INSERT INTO stock_moves (move_id, warehouse_id, sku, delta, kind, ref, created_at, value_paisa, order_id) VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"MOV-V5-{uuid.uuid4().hex[:12].upper()}", key[0], key[1], gap, "opening", "V5 ledger reconciliation", stamp, 0, None))

    # ---- opening inventory value (moving-average pool) at the product's cost price
    cost = {sku: int(c) for sku, c in conn.execute("SELECT sku, cost_price FROM products")}
    for sku, qty in conn.execute("SELECT sku, SUM(on_hand) FROM stock GROUP BY sku").fetchall():
        qty = int(qty or 0)
        conn.execute("INSERT OR REPLACE INTO inventory_value (sku, value_paisa) VALUES (?,?)", (sku, max(0, qty) * max(0, cost.get(sku, 0))))

    # ---- sale_lines for deliveries closed before V5 (no cost snapshot existed: legacy basis)
    orders = {oid: json.loads(items or "[]") for oid, items in conn.execute("SELECT order_id, items FROM orders")}
    for stop_id, order_id, customer_id, delivered, closed_at in conn.execute(
            "SELECT stop_id, order_id, customer_id, delivered_items, closed_at FROM stops WHERE status IN ('delivered','short')").fetchall():
        qty_by_sku: dict[str, int] = {}
        for ln in json.loads(delivered or "[]"):
            q = int(ln.get("qty") or 0)
            if q > 0: qty_by_sku[ln["sku"]] = qty_by_sku.get(ln["sku"], 0) + q
        inv = conn.execute("SELECT entry_id FROM ledger WHERE kind='invoice' AND ref=? ORDER BY created_at DESC LIMIT 1", (order_id,)).fetchone()
        for sku, q in qty_by_sku.items():
            revenue, left = 0, q
            for it in orders.get(order_id, []):
                if it["sku"] != sku or left <= 0: continue
                take = min(left, int(it["qty"])); revenue += take * int(it["unit_price_paisa"]); left -= take
            conn.execute("INSERT INTO sale_lines (stop_id, order_id, customer_id, invoice_id, sku, qty, revenue_paisa, cost_paisa, cost_basis, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (stop_id, order_id, customer_id, inv[0] if inv else None, sku, q, max(0, revenue), q * max(0, cost.get(sku, 0)),
                          "legacy_cost_price", closed_at or stamp))

    # ---- history is append-only from here on: corrections are reversing entries
    for t in _APPEND_ONLY:
        for op in ("UPDATE", "DELETE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_append_only_{op.lower()} BEFORE {op} ON {t} "
                         f"BEGIN SELECT RAISE(ABORT, '{t} is append-only: post a reversal instead of editing or deleting'); END")

    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        raise MigrationError(f"foreign key violations after V5: {[tuple(r) for r in bad[:5]]}")


Step = str | Callable[[sqlite3.Connection], None]
MIGRATIONS: list[tuple[int, Step]] = [(1, V1), (2, V2), (3, V3), (4, V4), (5, _v5)]


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
    row = conn.execute("SELECT MAX(version) v FROM schema_version").fetchone()
    return int(row[0] or 0)


def _run_sql(conn: sqlite3.Connection, sql: str) -> None:
    for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            # a re-run against a file that already has the column (e.g. created by a dev build)
            if "duplicate column" not in str(e).lower():
                raise


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply every migration above the file's version, each in its own all-or-nothing
    transaction. Returns the versions applied. Safe against a second process migrating
    the same file at the same time: the version is re-read under the write lock."""
    applied = []
    current_version(conn)
    if conn.in_transaction:
        conn.commit()
    for version, step in MIGRATIONS:
        if version <= current_version(conn):
            continue
        fk_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        if fk_on:
            conn.execute("PRAGMA foreign_keys=OFF")     # table rebuilds; checked explicitly before commit
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if version > current_version(conn):
                    if callable(step):
                        step(conn)
                    else:
                        _run_sql(conn, step)
                    conn.execute("INSERT INTO schema_version VALUES (?, datetime('now'))", (version,))
                    applied.append(version)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            if fk_on:
                conn.execute("PRAGMA foreign_keys=ON")
    return applied
