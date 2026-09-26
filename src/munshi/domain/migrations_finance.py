"""Migration V8: company finance -- money accounts, method routes, account transfers, the journal, loans, fixed assets,
cash counts, bank clearings and reconciliations, period closes + the books lock. OWNED BY STREAM B (plan §4.1).

HOW THIS STEP IS REGISTERED (read before editing; the pattern is shared by V8, V9, V10):
  * `STEP` is None until the schema below is really written. migrations.py registers only steps that are not None,
    and only CONTIGUOUSLY (V9 waits for V8, V10 for V9). An unwritten stub is therefore never applied, so no database
    is ever stamped "version 8" without V8's tables -- the failure a registered no-op stub would cause, because
    migrate() skips every version at or below the file's highest applied one, forever.
  * When V8 is written: set `STEP = v8`. From the moment that lands on main and any real file applies it, V8 is
    frozen like V1-V7 (a later fix is a new step). Until then it may change.
  * Do NOT import munshi.domain.migrations at module level here (migrations.py imports this module while it is being
    imported). Import its helpers inside v8(), as below.
  * Follow the _v7 pattern: plain SQL through _run_sql, then triggers one by one (their bodies contain ';').
    The whole step runs inside migrate()'s single BEGIN IMMEDIATE transaction; never COMMIT inside it.
  * While V8 is not registered, tests can apply it by hand without stamping schema_version:
        from munshi.domain.migrations import run_step_unstamped; run_step_unstamped(repo._conn, v8)
  * tests/test_seams.py (extend it) proves: :memory: and a V7 file both migrate cleanly to the newest version.

Seeds (plan §4.1): money_accounts ('CASH', 'cash', 'Cash in hand (galla)', ..., is_default=1) and
method_routes ('cash' -> 'CASH' from '0001-01-01'). Chart codes, CASH_ACCOUNT_ID and UNASSIGNED_ACCOUNT_ID come from
munshi.domain.accounts."""
from __future__ import annotations

import sqlite3
from typing import Callable

VERSION = 8


def v8(conn: sqlite3.Connection) -> None:
    raise NotImplementedError("V8 (finance) is Stream B's; it is not registered until STEP is set")


STEP: Callable[[sqlite3.Connection], None] | None = None     # Stream B: STEP = v8
