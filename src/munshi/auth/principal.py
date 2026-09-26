"""Who is acting, and what they may do.

A Principal is the signed-in human: one user, one business, one role. Every
HTTP endpoint declares the permission it needs; the matrix below is the
single place that says which roles hold it. Agent tool visibility is a
separate, stricter layer (safety/risk.py + role-gated middleware): a role
that cannot *see* a tool cannot ask for it, and a role that can request an
action still cannot approve it unless the registry says so."""
from __future__ import annotations

from dataclasses import dataclass

ROLES = ("owner", "clerk", "salesman", "driver")
ROLE_TITLES = {"owner": "Owner", "clerk": "Clerk (munshi)", "salesman": "Salesman / order booker", "driver": "Driver"}

# permission -> roles that hold it
PERMISSIONS: dict[str, frozenset[str]] = {
    "chat":            frozenset(ROLES),
    "approvals:read":  frozenset({"owner", "clerk"}),
    "approvals:decide": frozenset({"owner", "clerk"}),          # tool-level check still applies
    "orders:read":     frozenset(ROLES),
    "orders:create":   frozenset({"owner", "clerk", "salesman"}),
    "dispatch:read":   frozenset({"owner", "clerk", "driver"}),
    "dispatch:write":  frozenset({"owner", "clerk"}),
    "stops:close":     frozenset({"owner", "clerk", "driver"}),
    "khata:read":      frozenset({"owner", "clerk", "salesman"}),
    "payments:write":  frozenset({"owner", "clerk"}),
    "purchases:read":  frozenset({"owner", "clerk"}),
    "purchases:write": frozenset({"owner", "clerk"}),
    "expenses:write":  frozenset({"owner", "clerk"}),
    "stock:read":      frozenset({"owner", "clerk", "salesman"}),
    "reports:read":    frozenset({"owner", "clerk"}),
    "reminders:read":  frozenset({"owner", "clerk", "salesman"}),
    "reminders:send":  frozenset({"owner", "clerk"}),
    "customers:read":  frozenset({"owner", "clerk", "salesman", "driver"}),
    "customers:write": frozenset({"owner", "clerk"}),
    "setup:write":     frozenset({"owner"}),
    "staff:manage":    frozenset({"owner"}),
    "audit:read":      frozenset({"owner", "clerk"}),
    "export":          frozenset({"owner"}),
    "backup":          frozenset({"owner"}),
    "settings:write":  frozenset({"owner"}),
    "notifications":   frozenset(ROLES),
    # ---- payroll (Stream 0, 2026-09-26). OWNER DECISION: salaries are visible to the owner only. A clerk records
    # attendance but never sees pay; every employee (any role) sees only their own payslips. This deliberately
    # narrows the plan's §6 table (which gave clerks payroll:read/write) and drops its payroll_clerk_access setting.
    "employees:read":   frozenset({"owner", "clerk"}),     # names, emp_no, designation, status, login badge -- NEVER a pay field
    "attendance:write": frozenset({"owner", "clerk"}),     # days worked, leave, OT minutes, trips: no rupee in or out
    "payroll:read":     frozenset({"owner"}),              # register, anyone's payslip, pay structures, commission, advances, statutory
    "payroll:write":    frozenset({"owner"}),              # employees' pay terms, adjustments, advances, salary payments, rates, settings
    "payroll:approve":  frozenset({"owner"}),              # approve / reverse a payroll run
    "payroll:self":     frozenset(ROLES),                  # MY payslips only: employees.user_id == principal.user_id
    # ---- books and company finance
    "books:read":       frozenset({"owner", "clerk"}),     # money accounts + account books (salary lines redacted for non-owners)
    "books:write":      frozenset({"owner", "clerk"}),     # transfers, cash counts, clearing ticks, reconciliations
    "finance:read":     frozenset({"owner"}),              # P&L, balance sheet, cash flow, TB, KPIs, capital, drawings, loans
    "finance:write":    frozenset({"owner"}),              # accounts, journal, assets, loans, capital, period close / reopen
    # ---- payment proofs (Stream E): uploading is not a money write; linking rides on an approved action
    "attachments:write": frozenset(ROLES),                 # a driver photographs a cheque
    "attachments:read":  frozenset({"owner", "clerk"}),    # everyone else: only their OWN uploads (uploaded_by == user_id),
                                                           # checked in the route with attachments:write, not granted here
}


@dataclass(frozen=True)
class Principal:
    user_id: str
    business_id: str
    role: str
    name: str
    phone: str = ""
    must_change_pin: bool = False     # set by the registry after an owner-issued PIN; web/deps gates on it (Stream A)

    def can(self, permission: str) -> bool:
        try:
            return self.role in PERMISSIONS[permission]
        except KeyError as e:
            raise ValueError(f"unknown permission {permission!r}") from e

    @property
    def label(self) -> str:
        return f"{self.name} ({self.role})"


def roles_with(permission: str) -> list[str]:
    return [r for r in ROLES if r in PERMISSIONS[permission]]
