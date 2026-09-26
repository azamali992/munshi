"""Employees, app logins for employees, attendance, payroll runs, payslips, salary payments, staff advances,
statutory summaries and rates. OWNED BY STREAM A (plan §4.3, §9 Stream A REST list). Stream 0: an empty router,
already mounted in web/app.py.

Permissions (auth/principal.py; owner decision 2 -- salaries are the owner's alone):
  employees:read (owner, clerk)    GET /api/employees -- NO pay field for a caller without payroll:read
  attendance:write (owner, clerk)  GET/PUT /api/payroll/{period}/attendance -- days / leave / OT / trips only
  payroll:read / payroll:write     everything with a rupee in it: owner only
  payroll:approve                  POST /api/payroll/{period}/approve, /api/payroll/runs/{id}/reverse
  staff:manage                     POST /api/employees/{id}/login, /login/reset-pin (PIN once, Cache-Control: no-store)
  payroll:self (every role)        GET /api/me/payslips -- the caller's own, resolved from principal.user_id ONLY
Validate every body with a pydantic model; never trust a client-supplied employee id for self service. Domain errors
map through app.py's handlers (StateError 409, NotFoundError 404, ValueError 400)."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["payroll"])
