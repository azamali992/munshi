"""Employees (+ optional app logins), pay structures, commission, attendance, adjustments, staff advances, the payroll
engine's runs / payslips / salary payments, statutory summaries and rates. OWNED BY STREAM A (plan §4.2, §4.3, §5,
§7, §9 Stream A). Stream 0 skeleton: signatures from §9 raising NotImplementedError, so B and D can mock the exact
names; rupees at the edge; every write takes actor + approved_by.

Working today (safe defaults B builds on before A lands):
  * payroll_setting(key)            -- the business's setting, else accounts.PAYROLL_SETTINGS_DEFAULTS
  * payroll_profile()               -- 'plc_2026' unless the owner switched it (owner decision 1)
  * _payroll_postings(start, end)   -- [] (no payroll yet = nothing to post)
  * payroll_liabilities_paisa(as_of) -- all zero

THE OWNER'S DECISIONS A implements (full text in domain/accounts.py):
  1. plc_2026 by default; accounts.PROFILE_LIMITS[profile] are ENFORCED (StateError naming the switch): advance cap
     3 x monthly minimum wage, one open advance, instalment <= 20% of pay, fine <= 3% (legacy 3.125%), salary and
     advance only by accounts.CASHLESS_METHODS, payslip in accounts.PAYSLIP_SECTIONS order with leave balances.
  2. Owner-only pay. Routes: payroll:read / payroll:write / payroll:approve are owner-only; attendance:write (clerk)
     accepts and returns days / leave / OT / trips ONLY; employees:read returns no pay field (no basic, rate,
     components, commission, advance, pay_method/payee_ref); my_payslips(user_id) serves payroll:self and must
     resolve the employee by employees.user_id == the caller's user_id, never by a client-supplied employee id.
  3. Accrual at approve_payroll: postings Dr 6100-6200 / Cr 2100 (+2110/2120/2130/1150/2140), and the
     PAYROLL_METHOD expense rows in accounts.SYSTEM_EXPENSE_CATEGORIES written DIRECTLY (INSERT, not
     record_expense -- which refuses them and would write a gated audit row the safety audit would count).
Other frozen contracts: ids from numbering series payroll/payslip/salary_payment/advance/statutory
(PAY/PSL/SPM/ADV/STY); ONE audit row per gated call named accounts.AUDIT_ACTION[tool]; login/PIN events use
accounts.SECONDARY_ACTIONS; a PIN never reaches this database, the audit payload, chat or an approval card.
Consumes from B: self.resolve_account(method, account_id, on_date), self.assert_period_open(on_date)."""
from __future__ import annotations

from munshi.domain.accounts import (
    DEFAULT_PAYROLL_PROFILE,
    EOBI_PAYABLE,
    INCOME_TAX_WITHHELD,
    PAYROLL_PROFILES,
    PAYROLL_SETTINGS_DEFAULTS,
    SALARIES_PAYABLE,
    SOCIAL_SECURITY_PAYABLE,
    STAFF_ADVANCES,
    STAFF_WELFARE_FUND,
)
from munshi.domain.repository.base import RepositoryBase


def _todo(name: str):
    raise NotImplementedError(f"{name} is Stream A's (payroll); not implemented yet")


class PayrollMixin(RepositoryBase):
    # ------------------------------------------------------------------ working defaults
    def payroll_setting(self, key: str) -> str:
        return self.setting(key, PAYROLL_SETTINGS_DEFAULTS[key])

    def payroll_profile(self) -> str:
        p = self.payroll_setting("payroll_profile")
        return p if p in PAYROLL_PROFILES else DEFAULT_PAYROLL_PROFILE

    def _payroll_postings(self, start: str | None = None, end: str | None = None) -> list:     # list[accounts.Posting]; CONSUMED BY B
        return []

    def payroll_liabilities_paisa(self, as_of: str | None = None) -> dict:                     # B cross-checks the balance sheet
        return {code: 0 for code in (SALARIES_PAYABLE, INCOME_TAX_WITHHELD, EOBI_PAYABLE, SOCIAL_SECURITY_PAYABLE,
                                     STAFF_WELFARE_FUND, STAFF_ADVANCES)}

    # ------------------------------------------------------------------ employees and logins
    def list_employees(self, status: str = "active", include_pay: bool = False) -> dict: _todo("list_employees")   # include_pay only for payroll:read
    def get_employee(self, employee_id: str, include_pay: bool = False) -> dict: _todo("get_employee")
    def find_employee(self, text: str) -> dict: _todo("find_employee")     # {"match": dict|None, "candidates": [dict]}
    def add_employee(self, data: dict, actor: str, approved_by: str | None = None) -> dict: _todo("add_employee")
    def update_employee(self, employee_id: str, changes: dict, actor: str, approved_by: str | None = None) -> dict: _todo("update_employee")
    def rehire_employee(self, employee_id: str, rejoined_on: str, actor: str, approved_by: str | None = None) -> dict: _todo("rehire_employee")
    def set_pay_structure(self, employee_id: str, effective_from: str, pay_basis: str, basic: float = 0.0, daily_rate: float = 0.0,
                          components: tuple = (), ot_eligible: bool = True, actor: str = "", approved_by: str | None = None) -> dict:
        _todo("set_pay_structure")
    def set_commission_rule(self, employee_id: str, basis: str, rate_pct: float = 0.0, per_unit: float = 0.0, sku: str | None = None,
                            min_basis: float = 0.0, effective_from: str | None = None, actor: str = "", approved_by: str | None = None) -> dict:
        _todo("set_commission_rule")
    def link_login(self, employee_id: str, user_id: str, role: str, actor: str) -> dict: _todo("link_login")
    def end_employment(self, employee_id: str, left_on: str, reason: str, actor: str, approved_by: str | None = None) -> dict:
        _todo("end_employment")     # returns user_id for the web layer to disable
    def sync_employees_from_users(self, users: list[dict], actor: str) -> int: _todo("sync_employees_from_users")

    # ------------------------------------------------------------------ the month
    def set_attendance(self, period: str, rows: list[dict], actor: str, approved_by: str | None = None) -> dict: _todo("set_attendance")
    def attendance(self, period: str) -> dict: _todo("attendance")      # attendance:write readers: days / leave / OT / trips only
    def add_payroll_adjustment(self, employee_id: str, period: str, code: str, amount: float, note: str, ref: str | None = None,
                               taxable: bool = True, actor: str = "", approved_by: str | None = None) -> dict: _todo("add_payroll_adjustment")
    def void_payroll_adjustment(self, adj_id: str, actor: str, approved_by: str | None = None) -> dict: _todo("void_payroll_adjustment")
    def preview_payroll(self, period: str, employee_ids: list[str] | None = None) -> dict: _todo("preview_payroll")  # + "fingerprint"
    def approve_payroll(self, period: str, fingerprint: str, actor: str, approved_by: str | None, kind: str = "regular") -> dict:
        _todo("approve_payroll")
    def reverse_payroll_run(self, run_id: str, reason: str, actor: str, approved_by: str | None) -> dict: _todo("reverse_payroll_run")
    def payroll_register(self, run_id: str | None = None, period: str | None = None) -> dict: _todo("payroll_register")
    def payslip(self, slip_id: str | None = None, employee_id: str | None = None, period: str | None = None) -> dict: _todo("payslip")
    def my_payslips(self, user_id: str, limit: int = 12) -> dict: _todo("my_payslips")

    # ------------------------------------------------------------------ money out
    def pay_salaries(self, run_id: str, payments: list[dict], actor: str, approved_by: str | None) -> dict: _todo("pay_salaries")
    def reverse_salary_payment(self, payment_id: str, reason: str, actor: str, approved_by: str | None) -> dict: _todo("reverse_salary_payment")
    def give_staff_advance(self, employee_id: str, amount: float, method: str, account_id: str | None = None, kind: str = "advance",
                           installment: float = 0.0, start_period: str | None = None, note: str = "", actor: str = "",
                           approved_by: str | None = None) -> dict: _todo("give_staff_advance")
    def repay_staff_advance(self, employee_id: str, amount: float, method: str, account_id: str | None = None, actor: str = "",
                            approved_by: str | None = None) -> dict: _todo("repay_staff_advance")
    def reverse_staff_advance(self, advance_id: str, reason: str, actor: str, approved_by: str | None) -> dict: _todo("reverse_staff_advance")
    def staff_advances_report(self, employee_id: str | None = None, status: str = "open") -> dict: _todo("staff_advances_report")

    # ------------------------------------------------------------------ statutory
    def statutory_summary(self, period: str, kind: str) -> dict: _todo("statutory_summary")     # kind: eobi | ss | income_tax
    def record_statutory_payment(self, kind: str, period: str, amount: float, method: str, account_id: str | None, challan_ref: str,
                                 paid_on: str, actor: str, approved_by: str | None) -> dict: _todo("record_statutory_payment")
    def statutory_rate(self, key: str, jurisdiction: str = "pk", on: str | None = None) -> dict: _todo("statutory_rate")
    def add_statutory_rate(self, key: str, jurisdiction: str, value: str, effective_from: str, source: str, source_url: str,
                           verified_on: str, grade: str, note: str, actor: str, approved_by: str | None) -> dict: _todo("add_statutory_rate")
    def set_payroll_settings(self, changes: dict, actor: str, approved_by: str | None) -> dict: _todo("set_payroll_settings")
