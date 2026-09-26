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
    "cashbook": RiskTier.READ_ONLY,
    "find_supplier": RiskTier.READ_ONLY,
    "list_suppliers": RiskTier.READ_ONLY,
    "supplier_khata": RiskTier.READ_ONLY,
    "payables_report": RiskTier.READ_ONLY,
    "broken_promises": RiskTier.READ_ONLY,
    "sales_report": RiskTier.READ_ONLY,
    "profit_summary": RiskTier.READ_ONLY,
    "collection_report": RiskTier.READ_ONLY,
    "stock_ledger": RiskTier.READ_ONLY,
    "stock_valuation": RiskTier.READ_ONLY,
    "slow_stock": RiskTier.READ_ONLY,
    "top_customers": RiskTier.READ_ONLY,
    # order desk
    "create_order": RiskTier.LOW_RISK,
    "update_order": RiskTier.LOW_RISK,      # edit a DRAFT's lines: what create_order asks for, asked again -- same tier
    "confirm_order": RiskTier.LOW_RISK,
    "cancel_order": RiskTier.LOW_RISK,
    # godown
    "allocate_order": RiskTier.LOW_RISK,
    "create_dispatch_plan": RiskTier.LOW_RISK,
    "approve_dispatch_plan": RiskTier.LOW_RISK,
    "transfer_stock": RiskTier.LOW_RISK,
    "adjust_stock": RiskTier.HIGH_RISK,
    # delivery
    "close_stop": RiskTier.OTP_GATED,
    # hisaab
    "record_deposit": RiskTier.LOW_RISK,
    "record_payment": RiskTier.LOW_RISK,
    "record_expense": RiskTier.LOW_RISK,
    "credit_note": RiskTier.HIGH_RISK,
    # reversals cancel money that already moved: owner-approved, like credit_note / pay_supplier
    "reverse_ledger_entry": RiskTier.HIGH_RISK,
    "reverse_expense": RiskTier.HIGH_RISK,
    # khareed
    "record_purchase": RiskTier.LOW_RISK,
    "pay_supplier": RiskTier.HIGH_RISK,
    "reverse_purchase": RiskTier.HIGH_RISK,
    "reverse_supplier_entry": RiskTier.HIGH_RISK,
    # wasooli
    "draft_reminder": RiskTier.LOW_RISK,
    "draft_due_reminders": RiskTier.LOW_RISK,
    "send_reminder": RiskTier.LOW_RISK,
    "log_promise": RiskTier.LOW_RISK,
}

# Payroll, company finance and payment proofs (plan §6; frozen by Stream 0 on 2026-09-26). The tiers are FINAL here.
# They live in their own table only because tests/test_safety.py requires RISK_REGISTRY == the tools tools/ exposes
# today, and these tools are not written yet (Stream D writes them). risk_of() already answers for them (approver_for,
# the guard and the stub model see the right tier). tools_requiring_approval() -- the HITL interrupt list -- does NOT
# yet include them, because tests/test_approval_cards requires a card builder for each. So Stream D, in the change
# that exposes a tool in build_tools() AND adds its CardBuilder._c_<tool>, promotes it into RISK_REGISTRY:
# RISK_REGISTRY.update(MONEY_TOOL_TIERS) (or a subset) -- the one permitted edit to this file. Exposing a gated tool
# without promoting it would leave it un-interrupted: tests/test_safety (registry == tools) catches that. Every LOW/HIGH tool below is also in
# eval/run_eval.py ALWAYS_GATED (registry-independent), with its audit action from domain/accounts.AUDIT_ACTION.
# Salary visibility (owner decision 2) is not a tier: see domain/accounts.TOOL_PERMISSION.
MONEY_TOOL_TIERS: dict[str, RiskTier] = {
    # ---- reads
    **{t: RiskTier.READ_ONLY for t in (
        "list_employees", "find_employee", "payroll_preview", "payroll_register", "payslip", "my_payslips",
        "staff_advances_report", "statutory_summary", "money_accounts", "account_book", "reconciliation_status",
        "trial_balance", "income_statement", "balance_sheet", "cash_flow", "owner_kpis", "margins_report",
        "fixed_assets_register", "loans_report", "period_status", "list_attachments")},
    # ---- clerical entry: clerk or owner, four-eyes
    "record_attendance": RiskTier.LOW_RISK,            # days / leave / OT minutes / trips: no pay figure on the card
    "transfer_between_accounts": RiskTier.LOW_RISK,    # money stays inside owner-created accounts
    "count_cash": RiskTier.LOW_RISK,                   # records only; the difference is posted by the owner
    "mark_cleared": RiskTier.LOW_RISK,
    "save_reconciliation": RiskTier.LOW_RISK,
    # ---- someone's pay: owner
    "add_employee": RiskTier.HIGH_RISK,
    "update_employee": RiskTier.HIGH_RISK,
    "rehire_employee": RiskTier.HIGH_RISK,
    "set_pay_structure": RiskTier.HIGH_RISK,
    "set_commission_rule": RiskTier.HIGH_RISK,
    "end_employment": RiskTier.HIGH_RISK,
    "add_payroll_adjustment": RiskTier.HIGH_RISK,      # bonus / fine / loss recovery: fines are legally capped
    "void_payroll_adjustment": RiskTier.HIGH_RISK,
    "approve_payroll_run": RiskTier.HIGH_RISK,         # books the month's salary cost (accrual)
    "reverse_payroll_run": RiskTier.HIGH_RISK,
    "pay_salaries": RiskTier.HIGH_RISK,                # money out, like pay_supplier
    "reverse_salary_payment": RiskTier.HIGH_RISK,
    "give_staff_advance": RiskTier.HIGH_RISK,          # money out; PLC s.18
    "repay_staff_advance": RiskTier.HIGH_RISK,         # money in, but an advance balance is owner-only (decision 2)
    "reverse_staff_advance": RiskTier.HIGH_RISK,
    "record_statutory_payment": RiskTier.HIGH_RISK,
    "add_statutory_rate": RiskTier.HIGH_RISK,          # changes every future payslip
    "set_payroll_settings": RiskTier.HIGH_RISK,        # the PLC 2026 / old-law switch, registrations
    # ---- the books: owner
    "add_money_account": RiskTier.HIGH_RISK,           # the theft vector for transfers
    "set_method_route": RiskTier.HIGH_RISK,            # where 'bank' money lands
    "record_capital": RiskTier.HIGH_RISK,
    "record_drawing": RiskTier.HIGH_RISK,
    "record_loan": RiskTier.HIGH_RISK,
    "repay_loan": RiskTier.HIGH_RISK,
    "add_fixed_asset": RiskTier.HIGH_RISK,
    "dispose_fixed_asset": RiskTier.HIGH_RISK,
    "run_depreciation": RiskTier.HIGH_RISK,
    "post_journal_entry": RiskTier.HIGH_RISK,
    "reverse_journal_entry": RiskTier.HIGH_RISK,
    "reverse_account_transfer": RiskTier.HIGH_RISK,
    "post_cash_difference": RiskTier.HIGH_RISK,
    "record_opening_balances": RiskTier.HIGH_RISK,
    "close_period": RiskTier.HIGH_RISK,
    "reopen_period": RiskTier.HIGH_RISK,
}

_ORDER = [RiskTier.READ_ONLY, RiskTier.OTP_GATED, RiskTier.LOW_RISK, RiskTier.HIGH_RISK]


def risk_of(tool_name: str) -> RiskTier:
    try:
        return RISK_REGISTRY[tool_name]
    except KeyError:
        pass
    try:
        return MONEY_TOOL_TIERS[tool_name]
    except KeyError as e:
        raise ValueError(f"unregistered tool {tool_name!r}: add it to RISK_REGISTRY before exposing it") from e


def tools_requiring_approval() -> list[str]:
    """Tools that pause for a human in the app. OTP-gated tools are not here:
    the customer's code is their approval, and it is verified in the repository."""
    # Live tools only: every one needs an approval-card builder (platform.CardBuilder._c_<tool>, tests/test_approval_cards).
    # A MONEY_TOOL_TIERS tool joins this list when Stream D promotes it into RISK_REGISTRY with its card builder.
    return [n for n, t in RISK_REGISTRY.items() if t in (RiskTier.LOW_RISK, RiskTier.HIGH_RISK)]


def approver_for(tool_name: str) -> str:
    """Which human role may approve this tool's action."""
    t = risk_of(tool_name)
    return {RiskTier.HIGH_RISK: "owner", RiskTier.LOW_RISK: "clerk"}.get(t, "none")


def role_may_approve(role: str, tool_name: str) -> bool:
    """Only the owner and the clerk approve anything; a salesman or driver
    can request an action (if their agent has the tool) but never clear it."""
    need = approver_for(tool_name)
    if need == "none": return False
    if role == "owner": return True
    return role == "clerk" and need == "clerk"


_RANK = {"none": 0, "clerk": 1, "owner": 2}


def stricter_role(a: str, b: str) -> str:
    """The more senior of two approver requirements. An approval's requirement
    may be escalated between request and decision, never relaxed."""
    return a if _RANK.get(a, 2) >= _RANK.get(b, 2) else b


def same_person(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def approval_refusal(role: str, tool_name: str, needs_role: str, approver: str, requester: str) -> str | None:
    """Why this approver may not clear this action, or None if they may.

    Four-eyes rule: whoever asked for a gated action cannot be the one who
    clears it. The single exception is the owner: the owner is the business's
    final authority and is always available as the second person for anyone
    else's request, so a one-clerk shop routes the clerk's requests to the
    owner, and an owner (often working alone) may clear their own.

    Identity is compared only when the request carries one (web sessions
    always do). A named request cannot be cleared by an unnamed approver: we
    could not tell whether it is the same person."""
    if not (role_may_approve(role, tool_name) and (role == "owner" or needs_role == "clerk")):
        return f"{tool_name} needs {needs_role} approval; you are {role}"
    if role == "owner" or not requester.strip():
        return None
    if not approver.strip():
        return "this request was made by a named user; the approver must be signed in so we can tell it's someone else"
    if same_person(approver, requester):
        return f"you asked for this action, so someone else must approve it (another {needs_role} or the owner)"
    return None
