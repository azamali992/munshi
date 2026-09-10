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
}


@dataclass(frozen=True)
class Principal:
    user_id: str
    business_id: str
    role: str
    name: str
    phone: str = ""

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
