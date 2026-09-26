"""Company finance: money accounts, method routes, transfers, cash counts, reconciliation, the journal (capital,
drawings, loans, fixed assets, depreciation, openings), period close and lock, and the reports read from the derived
ledger. OWNED BY STREAM B (plan §3.4, §4.1, §4.4, §7, §9 Stream B). Stream 0 skeleton.

Two methods WORK today, as safe defaults other streams build on before B lands:
  * assert_period_open(on_date)  -- no-op (no books lock exists before V8)
  * resolve_account(method, account_id, on_date) -- the explicit account, else 'CASH' for cash, else None
B replaces both (resolve_account then returns accounts.UNASSIGNED_ACCOUNT_ID, never None, for an unrouted method).
Every other method is a signature raising NotImplementedError, so A and D can mock against the exact names.
If B moves some of them into ledger_projection.py / finance_reports.py mixins, B DELETES the stub here (a stub left
here would shadow the real method depending on MRO order).

Contracts B implements (the owner's decisions are in domain/accounts.py's docstring):
  * decision 3 (accrual): postings() maps payroll via A's _payroll_postings(); salary_payments clear 2100;
    verify_books() alarms if the PAYROLL_METHOD expense rows != the payroll postings.
  * decision 2: account_book(..., redact_payroll=True) for callers without payroll:read (a clerk) aggregates salary,
    advance and statutory lines per day under accounts.PAYROLL_REDACTED_LABEL -- no employee name, no single salary.
  * record_expense refuses accounts.SYSTEM_EXPENSE_CATEGORIES; reverse_expense refuses method='payroll' rows.
  * every write takes actor + approved_by, writes exactly ONE audit row named accounts.AUDIT_ACTION[tool], checks
    assert_period_open, and runs in immediate_tx; money is integer paisa inside, rupees at the edge.
  * reports return {"table": accounts.table(...), ...raw numbers} (multi-part: "tables": [...] too)."""
from __future__ import annotations

from munshi.domain.accounts import CASH_ACCOUNT_ID
from munshi.domain.repository.base import RepositoryBase


def _todo(name: str):
    raise NotImplementedError(f"{name} is Stream B's (finance); not implemented yet")


class FinanceMixin(RepositoryBase):
    # ------------------------------------------------------------------ working defaults (consumed by A and cash.py)
    def assert_period_open(self, on_date: str) -> None:
        """Raise StateError if on_date falls in a closed period. Before V8 there are no closes: always open."""
        return None

    def resolve_account(self, method: str, account_id: str | None = None, on_date: str | None = None) -> str | None:
        """Which money account a payment by `method` lands in. Before V8: the explicit account, else cash -> 'CASH'."""
        if account_id:
            return account_id
        return CASH_ACCOUNT_ID if method == "cash" else None

    # ------------------------------------------------------------------ money accounts
    def list_money_accounts(self, include_inactive: bool = False) -> dict: _todo("list_money_accounts")
    def add_money_account(self, kind: str, name: str, provider: str = "", number_last4: str = "", opening_balance: float = 0.0,
                          opening_date: str | None = None, actor: str = "", approved_by: str | None = None) -> dict: _todo("add_money_account")
    def set_method_route(self, method: str, account_id: str, effective_from: str, actor: str, approved_by: str | None) -> dict: _todo("set_method_route")
    def account_balance_paisa(self, account_id: str, as_of: str | None = None) -> int: _todo("account_balance_paisa")
    def account_book(self, account_id: str, start: str, end: str, redact_payroll: bool = False) -> dict: _todo("account_book")
    def transfer(self, from_account: str, to_account: str, amount: float, on_date: str | None = None, ref: str = "", note: str = "",
                 actor: str = "", approved_by: str | None = None) -> dict: _todo("transfer")
    def reverse_transfer(self, transfer_id: str, reason: str, actor: str, approved_by: str | None) -> dict: _todo("reverse_transfer")
    def count_cash(self, account_id: str, counted: float, note: str = "", actor: str = "") -> dict: _todo("count_cash")
    def post_cash_difference(self, count_id: str, actor: str, approved_by: str | None) -> dict: _todo("post_cash_difference")
    def mark_cleared(self, account_id: str, items: list[dict], cleared_on: str, cleared: bool, actor: str) -> dict: _todo("mark_cleared")
    def save_reconciliation(self, account_id: str, statement_date: str, statement_balance: float, actor: str) -> dict: _todo("save_reconciliation")
    def reconciliation(self, account_id: str, statement_date: str) -> dict: _todo("reconciliation")

    # ------------------------------------------------------------------ journal
    def record_capital(self, amount: float, method: str, account_id: str | None = None, on_date: str | None = None, note: str = "",
                       actor: str = "", approved_by: str | None = None) -> dict: _todo("record_capital")
    def record_drawing(self, amount: float, method: str, account_id: str | None = None, on_date: str | None = None, note: str = "",
                       actor: str = "", approved_by: str | None = None) -> dict: _todo("record_drawing")
    def add_loan(self, lender: str, kind: str, amount: float, method: str, account_id: str | None = None, on_date: str | None = None,
                 terms: str = "", actor: str = "", approved_by: str | None = None) -> dict: _todo("add_loan")
    def repay_loan(self, loan_id: str, principal: float, interest: float = 0.0, method: str = "bank", account_id: str | None = None,
                   on_date: str | None = None, actor: str = "", approved_by: str | None = None) -> dict: _todo("repay_loan")
    def add_fixed_asset(self, name: str, category: str, cost: float, acquired_on: str, life_months: int, salvage: float = 0.0,
                        funded_by: str = "paid", method: str | None = None, account_id: str | None = None, vehicle_id: str | None = None,
                        actor: str = "", approved_by: str | None = None) -> dict: _todo("add_fixed_asset")
    def dispose_fixed_asset(self, asset_id: str, on_date: str, proceeds: float, method: str, account_id: str | None = None,
                            actor: str = "", approved_by: str | None = None) -> dict: _todo("dispose_fixed_asset")
    def run_depreciation(self, through_period: str, actor: str, approved_by: str | None) -> dict: _todo("run_depreciation")
    def post_journal(self, entry_date: str, kind: str, memo: str, lines: list[dict], source: str | None = None, source_id: str | None = None,
                     actor: str = "", approved_by: str | None = None) -> dict: _todo("post_journal")
    def reverse_journal(self, je_id: str, reason: str, actor: str, approved_by: str | None) -> dict: _todo("reverse_journal")
    def record_opening_balances(self, as_of: str, money: list[dict], assets: list[dict], loans: list[dict],
                                capital_label: str = "Opening balance equity", actor: str = "", approved_by: str | None = None) -> dict:
        _todo("record_opening_balances")

    # ------------------------------------------------------------------ periods
    def period_status(self) -> dict: _todo("period_status")
    def close_period(self, through_date: str, note: str = "", force: bool = False, actor: str = "", approved_by: str | None = None) -> dict:
        _todo("close_period")
    def reopen_period(self, close_id: int, reason: str, actor: str, approved_by: str | None) -> dict: _todo("reopen_period")

    # ------------------------------------------------------------------ derived ledger and reports
    def postings(self, start: str | None = None, end: str | None = None) -> list: _todo("postings")     # list[accounts.Posting]
    def trial_balance(self, as_of: str | None = None) -> dict: _todo("trial_balance")
    def general_journal(self, start: str, end: str) -> dict: _todo("general_journal")
    def income_statement(self, start: str, end: str, compare: bool = True) -> dict: _todo("income_statement")
    def balance_sheet(self, as_of: str | None = None) -> dict: _todo("balance_sheet")
    def cash_flow(self, start: str, end: str) -> dict: _todo("cash_flow")
    def owner_kpis(self, as_of: str | None = None) -> dict: _todo("owner_kpis")
    def margins(self, by: str, start: str, end: str) -> dict: _todo("margins")
    def fixed_assets_register(self, as_of: str | None = None) -> dict: _todo("fixed_assets_register")
    def loans_report(self, as_of: str | None = None) -> dict: _todo("loans_report")
    def verify_books(self, as_of: str | None = None) -> dict: _todo("verify_books")
