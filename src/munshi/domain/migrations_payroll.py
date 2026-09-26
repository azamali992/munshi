"""Migration V9: payroll -- employees (+ events), pay structures, commission rules, attendance months, payroll
adjustments, staff advances, payroll runs / lines, payslips, salary payments, statutory payments, statutory rates
(+ seeds). OWNED BY STREAM A (plan §4.2).

Registration pattern: exactly as documented in migrations_finance.py -- `STEP` stays None until the schema is
written, then `STEP = v9`. V9 is registered only once V8 is (contiguity), which also matches its dependencies:
salary_payments / staff_advances reference money_accounts and the period-lock triggers read the books_lock view.
While V8 is unregistered, Stream A's tests apply V9 by hand, unstamped (run_step_unstamped(repo._conn, v9)), after
creating in the test fixture the two V8 objects V9 depends on (money_accounts with a 'CASH' row, the books_lock view)
-- never in this file; once B's v8 exists, apply that first instead.

Owner decisions this schema must carry (domain/accounts.py): payroll_profile defaults to 'plc_2026' (a settings row
is NOT seeded -- the default comes from accounts.PAYROLL_SETTINGS_DEFAULTS, so a business that never touches the
switch follows the current default); statutory_payments ids use the 'STY' series (not 'STP'); payslips carry the
PLC s.165(15) content including leave balances."""
from __future__ import annotations

import sqlite3
from typing import Callable

VERSION = 9


def v9(conn: sqlite3.Connection) -> None:
    raise NotImplementedError("V9 (payroll) is Stream A's; it is not registered until STEP is set")


STEP: Callable[[sqlite3.Connection], None] | None = None     # Stream A: STEP = v9
