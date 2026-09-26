"""Shared fixtures for Stream A's payroll tests (no tests of its own).

V9 is written but not yet registered (it waits for B's V8, SEAMS §6), so the tests apply it by hand, unstamped, with
migrations.run_step_unstamped. V9 needs two V8 objects -- money_accounts with the 'CASH' row, and the books_lock view --
which this helper creates as a minimal stand-in ONLY when V8 has not already created them. Once the lead registers V8
and V9 at merge, `enable_payroll` finds everything in place and does nothing."""
from __future__ import annotations

import importlib
from datetime import UTC, timedelta, timezone
from datetime import date as _real_date
from datetime import datetime as _real_datetime

from munshi.domain.migrations import run_step_unstamped
from munshi.domain.migrations_payroll import v9

V8_STAND_IN = [
    """CREATE TABLE IF NOT EXISTS money_accounts (account_id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
       provider TEXT NOT NULL DEFAULT '', number_last4 TEXT NOT NULL DEFAULT '', opening_date TEXT, is_default INTEGER NOT NULL DEFAULT 0,
       active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)""",
    "INSERT OR IGNORE INTO money_accounts VALUES ('CASH','cash','Cash in hand (galla)','','',NULL,1,1,'2026-01-01T00:00:00+00:00')",
    "CREATE TABLE IF NOT EXISTS period_closes (close_id INTEGER PRIMARY KEY, through_date TEXT NOT NULL, reopened_at TEXT)",
    "CREATE VIEW IF NOT EXISTS books_lock AS SELECT COALESCE(MAX(through_date), '0000-00-00') AS through_date FROM period_closes WHERE reopened_at IS NULL",
]


def enable_payroll(repo, accounts: tuple = (("ACC-HBL", "bank", "HBL current"), ("ACC-JAZZ", "wallet", "JazzCash"), ("ACC-EP", "wallet", "Easypaisa"))):
    """Apply V9 (and the V8 stand-in it needs) to a repository's file; add money accounts for the tests."""
    conn = repo._conn
    with repo._lock:
        has = conn.execute("SELECT 1 FROM sqlite_master WHERE name='money_accounts'").fetchone()
        if not has:
            def stand_in(c):
                for s in V8_STAND_IN:
                    c.execute(s)
            run_step_unstamped(conn, stand_in)
        if not repo.payroll_ready():
            run_step_unstamped(conn, v9)
        for acc_id, kind, name in accounts:
            conn.execute("INSERT OR IGNORE INTO money_accounts (account_id, kind, name, created_at) VALUES (?,?,?,?)",
                         (acc_id, kind, name, "2026-01-01T00:00:00+00:00"))
    return repo


def set_eobi_base(repo, rupees: int = 37_000, who: str = "owner"):
    """The owner verifies the EOBI wage base (the seeded one is flagged VERIFY and refused)."""
    return repo.add_statutory_rate("eobi_wage_base", "pk", str(rupees * 100), "2024-07-01", "EOBI PR-03 challan, checked by owner", "",
                                   "2026-09-26", "B", "owner-verified", who, f"owner:{who}")


PKT = timezone(timedelta(hours=5), "PKT")
CLOCK_MODULES = ("munshi.domain.models", "munshi.domain.seed", "munshi.domain.repository.base", "munshi.domain.repository.cash",
                 "munshi.domain.repository.reports", "munshi.domain.repository.collections", "munshi.domain.repository.dispatch",
                 "munshi.domain.repository.orders", "munshi.domain.repository.master", "munshi.domain.repository.payroll")


class Clock:
    utc = _real_datetime(2026, 9, 1, tzinfo=UTC)

    def at(self, day: int, hh: int, mm: int = 0, month: int = 9) -> None:
        self.utc = _real_datetime(2026, month, day, hh, mm, tzinfo=PKT).astimezone(UTC)


def install_clock(monkeypatch) -> Clock:
    c = Clock()

    class FakeDateTime(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            return c.utc.astimezone(tz) if tz else c.utc.replace(tzinfo=None)

    class FakeDate(_real_date):
        @classmethod
        def today(cls):
            return c.utc.date()

    for name in CLOCK_MODULES:
        mod = importlib.import_module(name)
        if getattr(mod, "datetime", None) is _real_datetime:
            monkeypatch.setattr(mod, "datetime", FakeDateTime)
        if getattr(mod, "date", None) is _real_date:
            monkeypatch.setattr(mod, "date", FakeDate)
    return c
