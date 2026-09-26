"""Migration V8: company finance -- money accounts, method routes, account transfers, the journal, loans, fixed assets,
cash counts, bank clearings and reconciliations, period closes + the books lock. OWNED BY STREAM B (plan §4.1).

HOW THIS STEP IS REGISTERED (read before editing; the pattern is shared by V8, V9, V10):
  * `STEP` is None until the schema below is really written. migrations.py registers only steps that are not None,
    and only CONTIGUOUSLY (V9 waits for V8, V10 for V9). An unwritten stub is therefore never applied, so no database
    is ever stamped "version 8" without V8's tables -- the failure a registered no-op stub would cause, because
    migrate() skips every version at or below the file's highest applied one, forever.
  * V8 IS WRITTEN: `STEP = v8`. From the moment this lands on main and any real file applies it, V8 is frozen like
    V1-V7 (a later fix is a new step).
  * Do NOT import munshi.domain.migrations at module level here (migrations.py imports this module while it is being
    imported). Its helpers are imported inside v8().
  * The _v7 pattern: plain SQL through _run_sql, then triggers one by one (their bodies contain ';').
    The whole step runs inside migrate()'s single BEGIN IMMEDIATE transaction; it never COMMITs.

What it adds (additive only: nothing existing is rewritten; every new money column is INTEGER paisa with a typeof CHECK):
  * money_accounts (+ the seeded 'CASH' "Cash in hand (galla)", the default cash account) and method_routes (+ the
    seeded route cash -> CASH from 0001-01-01). bank / cheque / wallets stay unrouted until the owner adds an account;
    until then such money resolves to the virtual UNASSIGNED account (chart 1900) and every report flags it.
  * account_id (nullable, FK) on ledger, supplier_ledger, expenses, deposits: the explicit money account of a NEW row.
    NULL on history = resolved by the method route in force on the row's business date, so a later remap never
    rewrites history (routes are append-only and dated).
  * account_transfers (XFR series), the journal (journal_lines first, the header last: the header's BEFORE INSERT
    trigger proves the entry has >= 2 lines and balances to the paisa; lines cannot be added to a posted entry),
    loans, fixed_assets (+ dep_start_period / opening_acc_dep_paisa for assets brought in at opening),
    cash_counts, bank_clearings (ticks: mutable, audited), reconciliations, period_closes + the books_lock view.
  * Guards in the database itself: append-only on transfers, journal, loans, fixed assets, cash counts,
    reconciliations and method routes; period_closes may only be REOPENED (once); money accounts are never deleted and
    never change id or kind; the period lock refuses an expense, journal entry, transfer or method route dated on or
    before the books lock (operational rows stamped "now" -- ledger, purchases, stops, deposits -- cannot fall in a
    closed period because a period can only be closed through a date before today).
"""
from __future__ import annotations

import sqlite3
from typing import Callable

VERSION = 8

V8 = """
CREATE TABLE IF NOT EXISTS money_accounts (
 account_id TEXT PRIMARY KEY CHECK (length(account_id) BETWEEN 2 AND 40),
 kind TEXT NOT NULL CHECK (kind IN ('cash','bank','wallet')),
 name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
 provider TEXT NOT NULL DEFAULT '',
 number_last4 TEXT NOT NULL DEFAULT '' CHECK (length(number_last4) <= 4),
 opening_date TEXT CHECK (opening_date IS NULL OR length(opening_date) = 10),
 is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1)),
 active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
 created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_money_accounts_default ON money_accounts(kind) WHERE is_default = 1;

CREATE TABLE IF NOT EXISTS method_routes (
 route_id INTEGER PRIMARY KEY,
 method TEXT NOT NULL CHECK (method IN ('cash','bank','jazzcash','easypaisa','cheque')),
 account_id TEXT NOT NULL REFERENCES money_accounts(account_id),
 effective_from TEXT NOT NULL CHECK (length(effective_from) = 10),
 set_by TEXT NOT NULL DEFAULT '', set_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_method_routes ON method_routes(method, effective_from, route_id);

ALTER TABLE ledger ADD COLUMN account_id TEXT REFERENCES money_accounts(account_id);
ALTER TABLE supplier_ledger ADD COLUMN account_id TEXT REFERENCES money_accounts(account_id);
ALTER TABLE expenses ADD COLUMN account_id TEXT REFERENCES money_accounts(account_id);
ALTER TABLE deposits ADD COLUMN account_id TEXT REFERENCES money_accounts(account_id);

CREATE TABLE IF NOT EXISTS account_transfers (
 transfer_id TEXT PRIMARY KEY,
 from_account TEXT NOT NULL REFERENCES money_accounts(account_id),
 to_account TEXT NOT NULL REFERENCES money_accounts(account_id),
 amount_paisa INTEGER NOT NULL CHECK (typeof(amount_paisa) = 'integer' AND amount_paisa <> 0),
 transfer_date TEXT NOT NULL CHECK (length(transfer_date) = 10),
 ref TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
 created_by TEXT NOT NULL DEFAULT '', approved_by TEXT, created_at TEXT NOT NULL,
 reversal_of TEXT REFERENCES account_transfers(transfer_id),
 CHECK (from_account <> to_account),
 CHECK ((reversal_of IS NULL AND amount_paisa > 0) OR (reversal_of IS NOT NULL AND amount_paisa < 0)));
CREATE UNIQUE INDEX IF NOT EXISTS ux_transfers_reversal ON account_transfers(reversal_of) WHERE reversal_of IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_transfers_date ON account_transfers(transfer_date);

CREATE TABLE IF NOT EXISTS journal_lines (
 line_id INTEGER PRIMARY KEY,
 je_id TEXT NOT NULL,
 account_code TEXT NOT NULL CHECK (length(account_code) BETWEEN 4 AND 40),
 money_account_id TEXT REFERENCES money_accounts(account_id),
 party_kind TEXT CHECK (party_kind IS NULL OR party_kind IN ('customer','supplier','employee','loan','asset','owner')),
 party_id TEXT,
 debit_paisa INTEGER NOT NULL DEFAULT 0 CHECK (typeof(debit_paisa) = 'integer' AND debit_paisa >= 0),
 credit_paisa INTEGER NOT NULL DEFAULT 0 CHECK (typeof(credit_paisa) = 'integer' AND credit_paisa >= 0),
 memo TEXT NOT NULL DEFAULT '',
 CHECK ((debit_paisa = 0) <> (credit_paisa = 0)),
 CHECK ((account_code = '1000') = (money_account_id IS NOT NULL)));
CREATE INDEX IF NOT EXISTS ix_journal_lines_je ON journal_lines(je_id);
CREATE INDEX IF NOT EXISTS ix_journal_lines_party ON journal_lines(party_kind, party_id);
CREATE INDEX IF NOT EXISTS ix_journal_lines_money ON journal_lines(money_account_id) WHERE money_account_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS journal_entries (
 je_id TEXT PRIMARY KEY,
 entry_date TEXT NOT NULL CHECK (length(entry_date) = 10),
 kind TEXT NOT NULL CHECK (kind IN ('opening','capital','drawing','loan','loan_repayment','asset_purchase','asset_disposal',
                                    'depreciation','cash_count','bank_charge','adjustment')),
 memo TEXT NOT NULL CHECK (length(memo) BETWEEN 3 AND 200),
 source TEXT, source_id TEXT,
 period TEXT CHECK (period IS NULL OR length(period) = 7),
 created_by TEXT NOT NULL DEFAULT '', approved_by TEXT, created_at TEXT NOT NULL,
 reversal_of TEXT REFERENCES journal_entries(je_id),
 CHECK (reversal_of IS NULL OR reversal_of <> je_id));
CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_reversal ON journal_entries(reversal_of) WHERE reversal_of IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_journal_date ON journal_entries(entry_date);
CREATE INDEX IF NOT EXISTS ix_journal_source ON journal_entries(source, source_id, kind);

CREATE TABLE IF NOT EXISTS loans (
 loan_id TEXT PRIMARY KEY,
 lender TEXT NOT NULL CHECK (length(lender) BETWEEN 2 AND 80),
 kind TEXT NOT NULL CHECK (kind IN ('bank','informal','family','other')),
 principal_paisa INTEGER NOT NULL CHECK (typeof(principal_paisa) = 'integer' AND principal_paisa > 0),
 received_on TEXT NOT NULL CHECK (length(received_on) = 10),
 terms TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS fixed_assets (
 asset_id TEXT PRIMARY KEY,
 name TEXT NOT NULL CHECK (length(name) BETWEEN 2 AND 80),
 category TEXT NOT NULL CHECK (category IN ('vehicle','building','land','furniture','equipment','computer','other')),
 vehicle_id TEXT,
 cost_paisa INTEGER NOT NULL CHECK (typeof(cost_paisa) = 'integer' AND cost_paisa > 0),
 salvage_paisa INTEGER NOT NULL DEFAULT 0 CHECK (typeof(salvage_paisa) = 'integer' AND salvage_paisa >= 0),
 acquired_on TEXT NOT NULL CHECK (length(acquired_on) = 10),
 life_months INTEGER NOT NULL CHECK (typeof(life_months) = 'integer' AND life_months >= 0),
 method TEXT NOT NULL DEFAULT 'straight_line' CHECK (method IN ('straight_line')),
 dep_start_period TEXT NOT NULL CHECK (length(dep_start_period) = 7),
 opening_acc_dep_paisa INTEGER NOT NULL DEFAULT 0 CHECK (typeof(opening_acc_dep_paisa) = 'integer' AND opening_acc_dep_paisa >= 0),
 funded_by TEXT NOT NULL DEFAULT 'paid' CHECK (funded_by IN ('paid','payable','opening')),
 created_at TEXT NOT NULL,
 CHECK (salvage_paisa <= cost_paisa),
 CHECK (opening_acc_dep_paisa <= cost_paisa - salvage_paisa));

CREATE TABLE IF NOT EXISTS cash_counts (
 count_id TEXT PRIMARY KEY,
 account_id TEXT NOT NULL REFERENCES money_accounts(account_id),
 counted_on TEXT NOT NULL CHECK (length(counted_on) = 10),
 counted_paisa INTEGER NOT NULL CHECK (typeof(counted_paisa) = 'integer' AND counted_paisa >= 0),
 book_paisa INTEGER NOT NULL CHECK (typeof(book_paisa) = 'integer'),
 counted_by TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS bank_clearings (
 account_id TEXT NOT NULL REFERENCES money_accounts(account_id),
 source TEXT NOT NULL CHECK (source IN ('ledger','supplier_ledger','expense','deposit','transfer','journal',
                                        'salary_payment','staff_advance','statutory_payment')),
 source_id TEXT NOT NULL,
 cleared_on TEXT NOT NULL CHECK (length(cleared_on) = 10),
 cleared_by TEXT NOT NULL, cleared_at TEXT NOT NULL,
 PRIMARY KEY (account_id, source, source_id));
CREATE TABLE IF NOT EXISTS reconciliations (
 recon_id INTEGER PRIMARY KEY,
 account_id TEXT NOT NULL REFERENCES money_accounts(account_id),
 statement_date TEXT NOT NULL CHECK (length(statement_date) = 10),
 statement_paisa INTEGER NOT NULL CHECK (typeof(statement_paisa) = 'integer'),
 book_paisa INTEGER NOT NULL CHECK (typeof(book_paisa) = 'integer'),
 uncleared_in_paisa INTEGER NOT NULL CHECK (typeof(uncleared_in_paisa) = 'integer'),
 uncleared_out_paisa INTEGER NOT NULL CHECK (typeof(uncleared_out_paisa) = 'integer'),
 difference_paisa INTEGER NOT NULL CHECK (typeof(difference_paisa) = 'integer'),
 done_by TEXT NOT NULL, done_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_reconciliations ON reconciliations(account_id, statement_date, recon_id);

CREATE TABLE IF NOT EXISTS period_closes (
 close_id INTEGER PRIMARY KEY,
 through_date TEXT NOT NULL CHECK (length(through_date) = 10),
 closed_by TEXT NOT NULL, closed_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
 snapshot TEXT NOT NULL,
 reopened_by TEXT, reopened_at TEXT, reopen_reason TEXT);
CREATE VIEW IF NOT EXISTS books_lock AS
 SELECT COALESCE(MAX(through_date), '0000-00-00') AS through_date FROM period_closes WHERE reopened_at IS NULL
"""

def _append_only(table: str) -> list[str]:
    return [f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_{op.lower()} BEFORE {op} ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only: post a reversal instead of editing or deleting'); END"
            for op in ("UPDATE", "DELETE")]


def _period_lock(table: str, column: str) -> str:
    # RAISE() takes a literal message in SQLite, so the date is not interpolated; the repository checks first
    # (assert_period_open) and names the date. This trigger is the guarantee behind it.
    return (f"CREATE TRIGGER IF NOT EXISTS {table}_period_lock BEFORE INSERT ON {table} "
            f"WHEN NEW.{column} <= (SELECT through_date FROM books_lock) "
            f"BEGIN SELECT RAISE(ABORT, 'books are closed for that date: post it in an open period or ask the owner to reopen'); END")


V8_TRIGGERS = [
    *[t for table in ("account_transfers", "journal_entries", "journal_lines", "cash_counts", "reconciliations", "loans",
                      "fixed_assets", "method_routes") for t in _append_only(table)],
    """CREATE TRIGGER IF NOT EXISTS journal_lines_closed BEFORE INSERT ON journal_lines
       WHEN EXISTS (SELECT 1 FROM journal_entries WHERE je_id = NEW.je_id)
       BEGIN SELECT RAISE(ABORT, 'journal entry already posted'); END""",
    """CREATE TRIGGER IF NOT EXISTS journal_entries_balanced BEFORE INSERT ON journal_entries
       WHEN (SELECT COUNT(*) FROM journal_lines WHERE je_id = NEW.je_id) < 2
         OR (SELECT SUM(debit_paisa) - SUM(credit_paisa) FROM journal_lines WHERE je_id = NEW.je_id) <> 0
       BEGIN SELECT RAISE(ABORT, 'journal entry does not balance'); END""",
    """CREATE TRIGGER IF NOT EXISTS period_closes_no_delete BEFORE DELETE ON period_closes
       BEGIN SELECT RAISE(ABORT, 'period_closes is append-only: reopen a close instead of deleting it'); END""",
    """CREATE TRIGGER IF NOT EXISTS period_closes_reopen_only BEFORE UPDATE ON period_closes
       WHEN NOT (OLD.reopened_at IS NULL AND NEW.reopened_at IS NOT NULL AND NEW.reopened_by IS NOT NULL
                 AND length(COALESCE(NEW.reopen_reason, '')) >= 3
                 AND NEW.close_id IS OLD.close_id AND NEW.through_date IS OLD.through_date AND NEW.closed_by IS OLD.closed_by
                 AND NEW.closed_at IS OLD.closed_at AND NEW.note IS OLD.note AND NEW.snapshot IS OLD.snapshot)
       BEGIN SELECT RAISE(ABORT, 'period_closes is append-only (a close may only be reopened, once, with a reason)'); END""",
    """CREATE TRIGGER IF NOT EXISTS money_accounts_no_delete BEFORE DELETE ON money_accounts
       BEGIN SELECT RAISE(ABORT, 'money accounts are never deleted: deactivate the account instead'); END""",
    """CREATE TRIGGER IF NOT EXISTS money_accounts_identity BEFORE UPDATE OF account_id, kind ON money_accounts
       WHEN NEW.account_id IS NOT OLD.account_id OR NEW.kind IS NOT OLD.kind
       BEGIN SELECT RAISE(ABORT, 'a money account keeps its id and kind: add a new account instead'); END""",
    _period_lock("expenses", "expense_date"),
    _period_lock("journal_entries", "entry_date"),
    _period_lock("account_transfers", "transfer_date"),
    _period_lock("method_routes", "effective_from"),
]


def v8(conn: sqlite3.Connection) -> None:
    from munshi.domain.accounts import CASH_ACCOUNT_ID
    from munshi.domain.migrations import _run_sql
    from munshi.domain.models import now_iso

    _run_sql(conn, V8)
    for stmt in V8_TRIGGERS:        # trigger bodies contain ';': executed one by one
        conn.execute(stmt)
    stamp = now_iso()
    conn.execute("INSERT OR IGNORE INTO money_accounts (account_id, kind, name, provider, number_last4, opening_date, is_default, active, created_at) "
                 "VALUES (?, 'cash', 'Cash in hand (galla)', '', '', NULL, 1, 1, ?)", (CASH_ACCOUNT_ID, stamp))
    if not conn.execute("SELECT 1 FROM method_routes WHERE method='cash'").fetchone():
        conn.execute("INSERT INTO method_routes (method, account_id, effective_from, set_by, set_at) VALUES ('cash', ?, '0001-01-01', 'V8', ?)",
                     (CASH_ACCOUNT_ID, stamp))


STEP: Callable[[sqlite3.Connection], None] | None = v8
