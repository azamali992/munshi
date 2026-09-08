"""One risk registry for every tool in Munshi. Declaring a tool's tier here
is what makes it approval-gated in the agents, role-restricted in the app,
and asserted on by the CI safety gate. A tool not listed here cannot be
called at all — the registry fails closed."""
from __future__ import annotations

from enum import Enum


class RiskTier(str, Enum):
    READ_ONLY = "read_only"   # never gated
    OTP_GATED = "otp_gated"   # gated by a code the customer holds, not by the app
    LOW_RISK = "low_risk"     # clerk or owner must approve before it runs
    HIGH_RISK = "high_risk"   # owner must approve before it runs


RISK_REGISTRY: dict[str, RiskTier] = {
    # lookups
    "find_customer": RiskTier.READ_ONLY,
    "get_customer_khata": RiskTier.READ_ONLY,
    "search_products": RiskTier.READ_ONLY,
    "get_stock": RiskTier.READ_ONLY,
    "list_orders": RiskTier.READ_ONLY,
    "get_order": RiskTier.READ_ONLY,
    "list_routes": RiskTier.READ_ONLY,
    "list_vehicles": RiskTier.READ_ONLY,
    "get_plan": RiskTier.READ_ONLY,
    "list_stops": RiskTier.READ_ONLY,
    "aging_report": RiskTier.READ_ONLY,
    "get_digest": RiskTier.READ_ONLY,
    "suggest_dispatch": RiskTier.READ_ONLY,
    # order desk
    "create_order": RiskTier.LOW_RISK,
    "confirm_order": RiskTier.LOW_RISK,
    # godown
    "allocate_order": RiskTier.LOW_RISK,
    "create_dispatch_plan": RiskTier.LOW_RISK,
    "approve_dispatch_plan": RiskTier.LOW_RISK,
    "adjust_stock": RiskTier.HIGH_RISK,
    # delivery
    "close_stop": RiskTier.OTP_GATED,
    # hisaab
    "record_deposit": RiskTier.LOW_RISK,
    "credit_note": RiskTier.HIGH_RISK,
    # wasooli
    "draft_reminder": RiskTier.LOW_RISK,
    "draft_due_reminders": RiskTier.LOW_RISK,
    "send_reminder": RiskTier.LOW_RISK,
    "log_promise": RiskTier.LOW_RISK,
}

_ORDER = [RiskTier.READ_ONLY, RiskTier.OTP_GATED, RiskTier.LOW_RISK, RiskTier.HIGH_RISK]


def risk_of(tool_name: str) -> RiskTier:
    try:
        return RISK_REGISTRY[tool_name]
    except KeyError as e:
        raise ValueError(f"unregistered tool {tool_name!r}: add it to RISK_REGISTRY before exposing it") from e


def tools_requiring_approval() -> list[str]:
    """Tools that pause for a human in the app. OTP-gated tools are not here:
    the customer's code is their approval, and it is verified in the repository."""
    return [n for n, t in RISK_REGISTRY.items() if t in (RiskTier.LOW_RISK, RiskTier.HIGH_RISK)]


def approver_for(tool_name: str) -> str:
    """Which human role may approve this tool's action."""
    t = risk_of(tool_name)
    return {RiskTier.HIGH_RISK: "owner", RiskTier.LOW_RISK: "clerk"}.get(t, "none")


def role_may_approve(role: str, tool_name: str) -> bool:
    need = approver_for(tool_name)
    if need == "none": return False
    if role == "owner": return True
    return role == "clerk" and need == "clerk"
