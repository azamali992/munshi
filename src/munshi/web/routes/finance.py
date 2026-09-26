"""Money accounts, account books, transfers, cash counts, bank reconciliation, method routes, the journal (capital,
drawings, loans, assets, depreciation, openings), statements, KPIs, period close. OWNED BY STREAM B (plan §9 Stream B
REST list). Stream 0: an empty router, already mounted in web/app.py.

Permissions (auth/principal.py):
  books:read (owner, clerk)    /api/accounts*, account books -- pass redact_payroll=not principal.can("payroll:read")
  books:write (owner, clerk)   transfers, cash counts, clearing ticks, reconciliations
  finance:read (owner)         /api/finance/{pnl|balance-sheet|cash-flow|trial-balance|kpis|margins|assets|loans}, general journal
  finance:write (owner)        accounts, method routes, journal, capital, drawings, loans, assets, depreciation, close / reopen
Form writes carry the signed-in human (Ctx.signature) as approved_by, like the existing money routes."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["finance"])
