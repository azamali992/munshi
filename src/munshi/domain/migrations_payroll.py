"""Migration V9: payroll -- employees (+ events), pay structures, commission rules, attendance months, payroll
adjustments, staff advances, payroll runs / lines, payslips, salary payments, statutory payments, statutory rates
(+ seeds). OWNED BY STREAM A (plan §4.2).

Registration pattern: exactly as documented in migrations_finance.py -- `STEP` stays None until the schema is
written, then `STEP = v9`. V9 is registered only once V8 is (contiguity), which also matches its dependencies:
salary_payments / staff_advances reference money_accounts and the period-lock triggers read the books_lock view.
While V8 is unregistered, Stream A's tests apply V9 by hand, unstamped (run_step_unstamped(repo._conn, v9)), after
creating in the test fixture the two V8 objects V9 depends on (money_accounts with a 'CASH' row, the books_lock view)
-- never in this file; once B's v8 exists, apply that first instead.

STATUS (Stream A, 2026-09-26): v9 below is WRITTEN. `STEP` is deliberately left None: the lead registers it at merge,
after B's V8 (SEAMS §6). Setting `STEP = v9` is the one-line registration.

Owner decisions this schema carries (domain/accounts.py): payroll_profile defaults to 'plc_2026' (a settings row
is NOT seeded -- the default comes from accounts.PAYROLL_SETTINGS_DEFAULTS, so a business that never touches the
switch follows the current default); statutory_payments ids use the 'STY' series (not 'STP'); payslips carry the
PLC s.165(15) content including leave balances; the owner's per-employee CASH EXEMPTION from the bank/wallet rule
(employees.cash_allowed, recorded on each payment it was relied on: salary_payments / staff_advances.cash_exemption).

Additions to the plan's §4.2 DDL (all additive, all Stream A's):
  * employees.cash_allowed / cash_allowed_note / cash_allowed_by / cash_allowed_at (owner decision: daily-wage cash);
  * employee_events kinds 'cash_exemption' and 'login_enabled';
  * payroll_runs.posted_on -- the business date the run posts on (period end; a reversal: the day it is reversed);
  * payslips.period, payslips.pay_basis, payslips.tax_detail (snapshots for the payslip, the WHT summary and
    self-service lookups);
  * staff_advances.applies_to (which advance a direct repayment pays down) and .cash_exemption;
  * salary_payments.cash_exemption;
  * payroll_requests -- idempotency keys for the REST writes that move money (a retried POST returns the first result).
Statutory seeds carry the plan's grade, source and verified_on; the two figures the plan could NOT verify are seeded
flagged: ss_wage_ceiling/punjab = 'unknown' and eobi_wage_base grade U with a VERIFY note -- the engine refuses those
lines until the owner adds a row of their own."""
from __future__ import annotations

import json
import sqlite3
from typing import Callable

VERSION = 9

_P = "CHECK (typeof({c}) = 'integer')"
_METHODS = "('cash','bank','jazzcash','easypaisa','cheque')"

V9_SQL = f"""
CREATE TABLE IF NOT EXISTS employees (
 employee_id TEXT PRIMARY KEY,
 emp_no TEXT NOT NULL UNIQUE CHECK (length(emp_no) BETWEEN 1 AND 12),
 name TEXT NOT NULL CHECK (length(name) BETWEEN 2 AND 60),
 aliases TEXT NOT NULL DEFAULT '[]',
 father_name TEXT NOT NULL DEFAULT '',
 cnic TEXT NOT NULL DEFAULT '' CHECK (cnic = '' OR (length(cnic) = 13 AND cnic NOT GLOB '*[^0-9]*')),
 phone TEXT NOT NULL DEFAULT '',
 date_of_birth TEXT CHECK (date_of_birth IS NULL OR length(date_of_birth) = 10),
 designation TEXT NOT NULL DEFAULT '',
 role_hint TEXT NOT NULL DEFAULT 'other' CHECK (role_hint IN ('clerk','salesman','driver','helper','loader','godown','guard','accountant','other')),
 joined_on TEXT NOT NULL CHECK (length(joined_on) = 10),
 left_on TEXT CHECK (left_on IS NULL OR length(left_on) = 10),
 status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','left')),
 user_id TEXT UNIQUE,
 province TEXT NOT NULL DEFAULT 'punjab' CHECK (province IN ('punjab','sindh','kp','balochistan','ict')),
 eobi_covered INTEGER NOT NULL DEFAULT 0 CHECK (eobi_covered IN (0,1)), eobi_no TEXT NOT NULL DEFAULT '',
 ss_covered INTEGER NOT NULL DEFAULT 0 CHECK (ss_covered IN (0,1)), ss_no TEXT NOT NULL DEFAULT '',
 tax_mode TEXT NOT NULL DEFAULT 'auto' CHECK (tax_mode IN ('auto','off')),
 opening_tax_year INTEGER,
 opening_ytd_taxable_paisa INTEGER NOT NULL DEFAULT 0 CHECK (typeof(opening_ytd_taxable_paisa) = 'integer' AND opening_ytd_taxable_paisa >= 0),
 opening_ytd_tax_paisa INTEGER NOT NULL DEFAULT 0 CHECK (typeof(opening_ytd_tax_paisa) = 'integer' AND opening_ytd_tax_paisa >= 0),
 pay_method TEXT NOT NULL DEFAULT 'cash' CHECK (pay_method IN {_METHODS}),
 pay_account_id TEXT REFERENCES money_accounts(account_id),
 payee_ref TEXT NOT NULL DEFAULT '',
 default_vehicle_id TEXT, route_ids TEXT NOT NULL DEFAULT '[]',
 cash_allowed INTEGER NOT NULL DEFAULT 0 CHECK (cash_allowed IN (0,1)),
 cash_allowed_note TEXT NOT NULL DEFAULT '',
 cash_allowed_by TEXT, cash_allowed_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK ((status = 'left') = (left_on IS NOT NULL)),
 CHECK (cash_allowed = 0 OR length(cash_allowed_note) >= 3));

CREATE TABLE IF NOT EXISTS employee_events (
 event_id INTEGER PRIMARY KEY,
 employee_id TEXT NOT NULL REFERENCES employees(employee_id),
 kind TEXT NOT NULL CHECK (kind IN ('joined','updated','left','rehired','login_created','login_disabled','login_enabled',
                                    'pin_reset','role_changed','cash_exemption')),
 detail TEXT NOT NULL DEFAULT '{{}}',
 at TEXT NOT NULL, by_user TEXT NOT NULL DEFAULT '', approved_by TEXT);
CREATE INDEX IF NOT EXISTS ix_employee_events ON employee_events(employee_id, event_id);

CREATE TABLE IF NOT EXISTS pay_structures (
 structure_id INTEGER PRIMARY KEY,
 employee_id TEXT NOT NULL REFERENCES employees(employee_id),
 effective_from TEXT NOT NULL CHECK (length(effective_from) = 10),
 pay_basis TEXT NOT NULL CHECK (pay_basis IN ('monthly','daily')),
 basic_paisa INTEGER NOT NULL DEFAULT 0 CHECK (typeof(basic_paisa) = 'integer' AND basic_paisa >= 0),
 daily_rate_paisa INTEGER NOT NULL DEFAULT 0 CHECK (typeof(daily_rate_paisa) = 'integer' AND daily_rate_paisa >= 0),
 ot_eligible INTEGER NOT NULL DEFAULT 1 CHECK (ot_eligible IN (0,1)),
 components TEXT NOT NULL DEFAULT '[]',
 set_by TEXT NOT NULL DEFAULT '', approved_by TEXT, created_at TEXT NOT NULL,
 CHECK ((pay_basis = 'monthly' AND basic_paisa > 0) OR (pay_basis = 'daily' AND daily_rate_paisa > 0)));
CREATE INDEX IF NOT EXISTS ix_pay_structures ON pay_structures(employee_id, effective_from, structure_id);

CREATE TABLE IF NOT EXISTS commission_rules (
 rule_id TEXT PRIMARY KEY,
 employee_id TEXT NOT NULL REFERENCES employees(employee_id),
 basis TEXT NOT NULL CHECK (basis IN ('booked_sales','route_collections','booked_qty')),
 sku TEXT,
 rate_bp INTEGER NOT NULL DEFAULT 0 CHECK (typeof(rate_bp) = 'integer' AND rate_bp BETWEEN 0 AND 10000),
 per_unit_paisa INTEGER NOT NULL DEFAULT 0 CHECK (typeof(per_unit_paisa) = 'integer' AND per_unit_paisa >= 0),
 min_basis_paisa INTEGER NOT NULL DEFAULT 0 CHECK (typeof(min_basis_paisa) = 'integer' AND min_basis_paisa >= 0),
 effective_from TEXT NOT NULL CHECK (length(effective_from) = 10), effective_to TEXT CHECK (effective_to IS NULL OR length(effective_to) = 10),
 set_by TEXT NOT NULL DEFAULT '', approved_by TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_commission_rules ON commission_rules(employee_id, effective_from);

CREATE TABLE IF NOT EXISTS attendance_months (
 employee_id TEXT NOT NULL REFERENCES employees(employee_id),
 period TEXT NOT NULL CHECK (length(period) = 7),
 days_worked_x2 INTEGER NOT NULL DEFAULT 0 CHECK (typeof(days_worked_x2) = 'integer' AND days_worked_x2 >= 0),
 unpaid_absent_x2 INTEGER NOT NULL DEFAULT 0 CHECK (typeof(unpaid_absent_x2) = 'integer' AND unpaid_absent_x2 >= 0),
 annual_leave_x2 INTEGER NOT NULL DEFAULT 0 CHECK (annual_leave_x2 >= 0),
 casual_leave_x2 INTEGER NOT NULL DEFAULT 0 CHECK (casual_leave_x2 >= 0),
 sick_leave_x2 INTEGER NOT NULL DEFAULT 0 CHECK (sick_leave_x2 >= 0),
 ot_minutes INTEGER NOT NULL DEFAULT 0 CHECK (ot_minutes >= 0),
 restday_ot_minutes INTEGER NOT NULL DEFAULT 0 CHECK (restday_ot_minutes >= 0),
 holiday_ot_minutes INTEGER NOT NULL DEFAULT 0 CHECK (holiday_ot_minutes >= 0),
 trips INTEGER NOT NULL DEFAULT 0 CHECK (trips >= 0),
 source TEXT NOT NULL DEFAULT 'register' CHECK (source IN ('register','whatsapp','app','import')),
 recorded_by TEXT NOT NULL, recorded_at TEXT NOT NULL,
 PRIMARY KEY (employee_id, period));

CREATE TABLE IF NOT EXISTS payroll_adjustments (
 adj_id TEXT PRIMARY KEY,
 employee_id TEXT NOT NULL REFERENCES employees(employee_id),
 period TEXT NOT NULL CHECK (length(period) = 7),
 code TEXT NOT NULL CHECK (code IN ('bonus','arrears','other_earning','fine','loss_recovery','other_deduction')),
 amount_paisa INTEGER NOT NULL CHECK (typeof(amount_paisa) = 'integer' AND amount_paisa > 0),
 taxable INTEGER NOT NULL DEFAULT 1 CHECK (taxable IN (0,1)),
 note TEXT NOT NULL CHECK (length(note) >= 3),
 ref TEXT,
 created_by TEXT NOT NULL, approved_by TEXT, created_at TEXT NOT NULL,
 voided_by TEXT, voided_at TEXT,
 CHECK (code <> 'loss_recovery' OR ref IS NOT NULL));
CREATE INDEX IF NOT EXISTS ix_payroll_adjustments ON payroll_adjustments(period, employee_id);

CREATE TABLE IF NOT EXISTS staff_advances (
 advance_id TEXT PRIMARY KEY,
 employee_id TEXT NOT NULL REFERENCES employees(employee_id),
 kind TEXT NOT NULL CHECK (kind IN ('advance','loan','repayment')),
 amount_paisa INTEGER NOT NULL CHECK (typeof(amount_paisa) = 'integer' AND amount_paisa <> 0),
 given_on TEXT NOT NULL CHECK (length(given_on) = 10),
 method TEXT NOT NULL CHECK (method IN {_METHODS}),
 account_id TEXT REFERENCES money_accounts(account_id),
 installment_paisa INTEGER NOT NULL DEFAULT 0 CHECK (typeof(installment_paisa) = 'integer' AND installment_paisa >= 0),
 start_period TEXT CHECK (start_period IS NULL OR length(start_period) = 7),
 applies_to TEXT REFERENCES staff_advances(advance_id),
 cash_exemption INTEGER NOT NULL DEFAULT 0 CHECK (cash_exemption IN (0,1)),
 note TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '', approved_by TEXT, created_at TEXT NOT NULL,
 reversal_of TEXT REFERENCES staff_advances(advance_id),
 CHECK (reversal_of IS NOT NULL OR (kind IN ('advance','loan') AND amount_paisa > 0) OR (kind = 'repayment' AND amount_paisa < 0 AND applies_to IS NOT NULL)));
CREATE UNIQUE INDEX IF NOT EXISTS ux_staff_advances_reversal ON staff_advances(reversal_of) WHERE reversal_of IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_staff_advances_employee ON staff_advances(employee_id, given_on);

CREATE TABLE IF NOT EXISTS payroll_runs (
 run_id TEXT PRIMARY KEY,
 period TEXT NOT NULL CHECK (length(period) = 7),
 kind TEXT NOT NULL CHECK (kind IN ('regular','final','bonus','reversal')),
 generation INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 1),
 period_start TEXT NOT NULL, period_end TEXT NOT NULL,
 posted_on TEXT NOT NULL CHECK (length(posted_on) = 10),
 rules TEXT NOT NULL,
 profile TEXT NOT NULL CHECK (profile IN ('legacy_1969','plc_2026')),
 fingerprint TEXT NOT NULL,
 gross_paisa INTEGER NOT NULL {_P.format(c='gross_paisa')},
 deductions_paisa INTEGER NOT NULL {_P.format(c='deductions_paisa')},
 net_paisa INTEGER NOT NULL {_P.format(c='net_paisa')},
 employer_paisa INTEGER NOT NULL {_P.format(c='employer_paisa')},
 headcount INTEGER NOT NULL,
 created_by TEXT NOT NULL DEFAULT '', approved_by TEXT NOT NULL, created_at TEXT NOT NULL,
 reversal_of TEXT REFERENCES payroll_runs(run_id),
 note TEXT NOT NULL DEFAULT '',
 UNIQUE (period, kind, generation));
CREATE UNIQUE INDEX IF NOT EXISTS ux_payroll_runs_reversal ON payroll_runs(reversal_of) WHERE reversal_of IS NOT NULL;

CREATE TABLE IF NOT EXISTS payroll_lines (
 line_id INTEGER PRIMARY KEY,
 run_id TEXT NOT NULL REFERENCES payroll_runs(run_id),
 employee_id TEXT NOT NULL REFERENCES employees(employee_id),
 code TEXT NOT NULL CHECK (code IN ('BASIC','OT','ALW','COMM','BONUS','ARREARS','OTHER_EARN',
                                    'ABSENCE','EOBI_EE','SS_EE','TAX','ADV','FINE','LOSS','OTHER_DED','IN_LIEU',
                                    'EOBI_ER','SS_ER')),
 side TEXT NOT NULL CHECK (side IN ('earning','deduction','employer')),
 label TEXT NOT NULL,
 amount_paisa INTEGER NOT NULL {_P.format(c='amount_paisa')},
 qty_x100 INTEGER, rate_paisa INTEGER,
 taxable INTEGER NOT NULL DEFAULT 0 CHECK (taxable IN (0,1)),
 ref TEXT);
CREATE INDEX IF NOT EXISTS ix_payroll_lines_run ON payroll_lines(run_id, employee_id);
CREATE INDEX IF NOT EXISTS ix_payroll_lines_ref ON payroll_lines(ref);

CREATE TABLE IF NOT EXISTS payslips (
 slip_id TEXT PRIMARY KEY,
 run_id TEXT NOT NULL REFERENCES payroll_runs(run_id),
 employee_id TEXT NOT NULL REFERENCES employees(employee_id),
 period TEXT NOT NULL CHECK (length(period) = 7),
 emp_no TEXT NOT NULL, name TEXT NOT NULL, designation TEXT NOT NULL, pay_basis TEXT NOT NULL,
 gross_paisa INTEGER NOT NULL {_P.format(c='gross_paisa')},
 deductions_paisa INTEGER NOT NULL {_P.format(c='deductions_paisa')},
 net_paisa INTEGER NOT NULL {_P.format(c='net_paisa')},
 employer_paisa INTEGER NOT NULL {_P.format(c='employer_paisa')},
 taxable_paisa INTEGER NOT NULL {_P.format(c='taxable_paisa')},
 tax_paisa INTEGER NOT NULL {_P.format(c='tax_paisa')},
 tax_year INTEGER NOT NULL,
 ytd_taxable_paisa INTEGER NOT NULL {_P.format(c='ytd_taxable_paisa')},
 ytd_tax_paisa INTEGER NOT NULL {_P.format(c='ytd_tax_paisa')},
 days_worked_x2 INTEGER NOT NULL, unpaid_absent_x2 INTEGER NOT NULL,
 paid_leave TEXT NOT NULL DEFAULT '{{}}',
 leave_balances TEXT NOT NULL DEFAULT '{{}}',
 tax_detail TEXT NOT NULL DEFAULT '{{}}',
 pay_method TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE (run_id, employee_id));
CREATE INDEX IF NOT EXISTS ix_payslips_employee ON payslips(employee_id, period);

CREATE TABLE IF NOT EXISTS salary_payments (
 payment_id TEXT PRIMARY KEY,
 slip_id TEXT NOT NULL REFERENCES payslips(slip_id),
 employee_id TEXT NOT NULL REFERENCES employees(employee_id),
 amount_paisa INTEGER NOT NULL CHECK (typeof(amount_paisa) = 'integer' AND amount_paisa <> 0),
 method TEXT NOT NULL CHECK (method IN {_METHODS}),
 account_id TEXT NOT NULL REFERENCES money_accounts(account_id),
 cash_exemption INTEGER NOT NULL DEFAULT 0 CHECK (cash_exemption IN (0,1)),
 ref TEXT NOT NULL DEFAULT '', paid_on TEXT NOT NULL CHECK (length(paid_on) = 10),
 paid_by TEXT NOT NULL DEFAULT '', approved_by TEXT, created_at TEXT NOT NULL,
 reversal_of TEXT REFERENCES salary_payments(payment_id),
 CHECK ((reversal_of IS NULL) = (amount_paisa > 0)));
CREATE UNIQUE INDEX IF NOT EXISTS ux_salary_payments_reversal ON salary_payments(reversal_of) WHERE reversal_of IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_salary_payments_slip ON salary_payments(slip_id);

CREATE TABLE IF NOT EXISTS statutory_payments (
 stat_id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK (kind IN ('eobi','pessi','sessi','kpessi','income_tax')),
 period TEXT NOT NULL CHECK (length(period) = 7),
 amount_paisa INTEGER NOT NULL CHECK (typeof(amount_paisa) = 'integer' AND amount_paisa <> 0),
 method TEXT NOT NULL CHECK (method IN {_METHODS}),
 account_id TEXT NOT NULL REFERENCES money_accounts(account_id),
 challan_ref TEXT NOT NULL DEFAULT '',
 paid_on TEXT NOT NULL CHECK (length(paid_on) = 10),
 created_by TEXT NOT NULL DEFAULT '', approved_by TEXT, created_at TEXT NOT NULL,
 reversal_of TEXT REFERENCES statutory_payments(stat_id));
CREATE UNIQUE INDEX IF NOT EXISTS ux_statutory_payments_reversal ON statutory_payments(reversal_of) WHERE reversal_of IS NOT NULL;

CREATE TABLE IF NOT EXISTS statutory_rates (
 rate_id INTEGER PRIMARY KEY,
 key TEXT NOT NULL,
 jurisdiction TEXT NOT NULL DEFAULT 'pk',
 value TEXT NOT NULL,
 effective_from TEXT NOT NULL CHECK (length(effective_from) = 10),
 source TEXT NOT NULL, source_url TEXT NOT NULL DEFAULT '',
 verified_on TEXT NOT NULL CHECK (length(verified_on) = 10),
 grade TEXT NOT NULL CHECK (grade IN ('A','B','C','D','U')),
 note TEXT NOT NULL DEFAULT '', set_by TEXT NOT NULL DEFAULT 'seed', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_statutory_rates ON statutory_rates(key, jurisdiction, effective_from, rate_id);

CREATE TABLE IF NOT EXISTS payroll_requests (
 idem_key TEXT PRIMARY KEY CHECK (length(idem_key) BETWEEN 8 AND 80),
 action TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 result TEXT NOT NULL,
 created_at TEXT NOT NULL)
"""

_APPEND_ONLY = ("employee_events", "pay_structures", "staff_advances", "payroll_runs", "payroll_lines", "payslips",
                "salary_payments", "statutory_payments", "statutory_rates", "payroll_requests")
# a standing regular run for a period: a regular run nobody has reversed
_STANDING = ("EXISTS (SELECT 1 FROM payroll_runs r WHERE r.period = {p} AND r.kind = 'regular' "
             "AND NOT EXISTS (SELECT 1 FROM payroll_runs x WHERE x.reversal_of = r.run_id))")
_LOCK_MSG = "the payroll for that month is approved: reverse the run to change attendance or adjustments"
_BOOKS = "(SELECT through_date FROM books_lock)"
_BOOKS_MSG = "books are closed through that date: post it in an open period or ask the owner to reopen"


def _triggers() -> list[str]:
    out = []
    for t in _APPEND_ONLY:
        for op in ("UPDATE", "DELETE"):
            out.append(f"CREATE TRIGGER IF NOT EXISTS {t}_append_only_{op.lower()} BEFORE {op} ON {t} "
                       f"BEGIN SELECT RAISE(ABORT, '{t} is append-only: post a reversal instead of editing or deleting'); END")
    out += [
        """CREATE TRIGGER IF NOT EXISTS commission_rules_end_once BEFORE UPDATE ON commission_rules
           WHEN NOT (OLD.effective_to IS NULL AND NEW.effective_to IS NOT NULL AND NEW.rule_id IS OLD.rule_id
                     AND NEW.employee_id IS OLD.employee_id AND NEW.basis IS OLD.basis AND NEW.sku IS OLD.sku
                     AND NEW.rate_bp IS OLD.rate_bp AND NEW.per_unit_paisa IS OLD.per_unit_paisa
                     AND NEW.min_basis_paisa IS OLD.min_basis_paisa AND NEW.effective_from IS OLD.effective_from
                     AND NEW.set_by IS OLD.set_by AND NEW.approved_by IS OLD.approved_by AND NEW.created_at IS OLD.created_at)
           BEGIN SELECT RAISE(ABORT, 'commission_rules: only an end date may be set, once'); END""",
        """CREATE TRIGGER IF NOT EXISTS commission_rules_no_delete BEFORE DELETE ON commission_rules
           BEGIN SELECT RAISE(ABORT, 'commission_rules: end a rule instead of deleting it'); END""",
        """CREATE TRIGGER IF NOT EXISTS payroll_adjustments_void_once BEFORE UPDATE ON payroll_adjustments
           WHEN NOT (OLD.voided_by IS NULL AND NEW.voided_by IS NOT NULL AND NEW.voided_at IS NOT NULL
                     AND NEW.adj_id IS OLD.adj_id AND NEW.employee_id IS OLD.employee_id AND NEW.period IS OLD.period
                     AND NEW.code IS OLD.code AND NEW.amount_paisa IS OLD.amount_paisa AND NEW.taxable IS OLD.taxable
                     AND NEW.note IS OLD.note AND NEW.ref IS OLD.ref AND NEW.created_by IS OLD.created_by
                     AND NEW.approved_by IS OLD.approved_by AND NEW.created_at IS OLD.created_at)
           BEGIN SELECT RAISE(ABORT, 'payroll_adjustments: only a void may be recorded, once'); END""",
        """CREATE TRIGGER IF NOT EXISTS payroll_adjustments_no_delete BEFORE DELETE ON payroll_adjustments
           BEGIN SELECT RAISE(ABORT, 'payroll_adjustments: void an adjustment instead of deleting it'); END""",
    ]
    for t in ("attendance_months", "payroll_adjustments"):
        out += [
            f"CREATE TRIGGER IF NOT EXISTS {t}_month_lock_insert BEFORE INSERT ON {t} WHEN {_STANDING.format(p='NEW.period')} "
            f"BEGIN SELECT RAISE(ABORT, '{_LOCK_MSG}'); END",
            f"CREATE TRIGGER IF NOT EXISTS {t}_month_lock_update BEFORE UPDATE ON {t} "
            f"WHEN {_STANDING.format(p='OLD.period')} OR {_STANDING.format(p='NEW.period')} "
            f"BEGIN SELECT RAISE(ABORT, '{_LOCK_MSG}'); END",
        ]
    out.append(f"CREATE TRIGGER IF NOT EXISTS attendance_months_month_lock_delete BEFORE DELETE ON attendance_months "
               f"WHEN {_STANDING.format(p='OLD.period')} BEGIN SELECT RAISE(ABORT, '{_LOCK_MSG}'); END")
    for t, col in (("payroll_runs", "posted_on"), ("staff_advances", "given_on"), ("salary_payments", "paid_on"),
                   ("statutory_payments", "paid_on")):
        out.append(f"CREATE TRIGGER IF NOT EXISTS {t}_period_lock BEFORE INSERT ON {t} WHEN NEW.{col} <= {_BOOKS} "
                   f"BEGIN SELECT RAISE(ABORT, '{_BOOKS_MSG}'); END")
    return out


V9_TRIGGERS = _triggers()

VERIFIED_ON = "2026-09-26"
_GAZETTE_PB = "Punjab Gazette 09-09-2025 (SO(L&P)MW/2024)"
_SLABS_TY2027 = {"tax_year": 2027, "bands": [
    {"over": 0, "upto": 60000000, "base": 0, "rate_bp": 0},
    {"over": 60000000, "upto": 120000000, "base": 0, "rate_bp": 100},
    {"over": 120000000, "upto": 220000000, "base": 600000, "rate_bp": 1100},
    {"over": 220000000, "upto": 320000000, "base": 11600000, "rate_bp": 2000},
    {"over": 320000000, "upto": 410000000, "base": 31600000, "rate_bp": 2500},
    {"over": 410000000, "upto": 560000000, "base": 54100000, "rate_bp": 2900},
    {"over": 560000000, "upto": 700000000, "base": 97600000, "rate_bp": 3200},
    {"over": 700000000, "upto": None, "base": 142400000, "rate_bp": 3500}]}
# (key, jurisdiction, value, effective_from, source, grade, note)  -- plan §4.2 seed table, verified 2026-09-26
SEEDS: list[tuple[str, str, str, str, str, str, str]] = [
    ("min_wage_monthly", "punjab", "4000000", "2025-07-01", _GAZETTE_PB, "A", "check for a FY2026-27 notice"),
    ("min_wage_daily", "punjab", "153846", "2025-07-01", _GAZETTE_PB, "A", "8 hours"),
    ("min_wage_hourly", "punjab", "19231", "2025-07-01", _GAZETTE_PB, "A", ""),
    ("in_lieu_food_meal", "punjab", "10000", "2025-07-01", _GAZETTE_PB, "A", "only with written agreement"),
    ("in_lieu_transport_month", "punjab", "180000", "2025-07-01", _GAZETTE_PB, "A", "only with written agreement"),
    ("in_lieu_accommodation_month", "punjab", "200000", "2025-07-01", _GAZETTE_PB, "A", "only with written agreement"),
    ("min_wage_monthly", "sindh", "4300000", "2026-07-01", "Sindh cabinet / notification 17-08-2026 (secondary)", "B", "arrears for July and August"),
    ("min_wage_monthly", "ict", "4070000", "2026-07-01", "ICT notification ADLW 8(20)-ICT/2024-934 (secondary)", "B", ""),
    ("min_wage_monthly", "kp", "4000000", "2025-07-01", "Mercans 2026-09-04 (last known)", "C", "last known"),
    ("min_wage_monthly", "balochistan", "3700000", "2025-07-01", "Mercans 2026-09-04 (last known)", "C", "last known"),
    ("working_days_basis", "pk", "26", "2025-07-01", _GAZETTE_PB, "A", "the Punjab notification's basis"),
    ("salary_tax_slabs", "pk", json.dumps(_SLABS_TY2027), "2026-07-01", "Finance Act 2026, Gazette of Pakistan 26-06-2026", "A", "TY2027"),
    ("eobi_employer_bp", "pk", "500", "2023-07-01", "EOBI Act 1976 s.9 (secondary sources agree)", "B", ""),
    ("eobi_employee_bp", "pk", "100", "2023-07-01", "EOBI Act 1976 s.9 (secondary sources agree)", "B", ""),
    ("eobi_wage_base", "pk", "3700000", "2024-07-01", "Employsome 2026-06-03; Peoplifi 2026-05-06 (disputed)", "U",
     "VERIFY with EOBI: may be 40,000 or 40,700 from July 2026; EOBI is not computed until the owner sets this"),
    ("eobi_min_headcount", "pk", "5", "2000-01-01", "EOBI Act 1976 (secondary)", "C", ""),
    ("ss_rate_bp", "punjab", "600", "2000-01-01", "PESSI website", "A", "PESSI"),
    ("ss_wage_ceiling", "punjab", "unknown", "2000-01-01", "not found on the PESSI pages reached", "U",
     "VERIFY: the engine refuses to compute PESSI until the owner sets this"),
    ("ss_rate_bp", "sindh", "600", "2000-01-01", "SESSI procedure page (stale mirror)", "C", ""),
    ("ss_wage_ceiling", "sindh", "3700000", "2000-01-01", "SESSI procedure page (stale mirror)", "C", "stale page"),
    ("ss_worker_share", "sindh", "4000", "2000-01-01", "SESSI procedure page (stale mirror)", "C", "stale page"),
    ("fine_cap_bp", "legacy_1969", "312", "2000-01-01", "Payment of Wages Act 1936 s.8 (half an anna in the rupee)", "A", "3.125% rounded down"),
    ("fine_cap_bp", "plc_2026", "300", "2026-02-10", "Punjab Labour Code 2026 s.169(4)", "A", "PLC in force: U"),
    ("deduction_cap_bp", "legacy_1969", "5000", "2000-01-01", "Payment of Wages Act s.7(3) (secondary)", "C", "warn only"),
    ("ot_multiplier_x100", "legacy_1969", "200", "2000-01-01", "Shops and Establishments Ordinance 1969 s.9", "A", ""),
    ("holiday_ot_multiplier_x100", "legacy_1969", "200", "2000-01-01", "Shops and Establishments Ordinance 1969 s.9", "A", ""),
    ("ot_multiplier_x100", "plc_2026", "200", "2026-02-10", "Punjab Labour Code 2026 ss.176-183", "A", ""),
    ("holiday_ot_multiplier_x100", "plc_2026", "300", "2026-02-10", "Punjab Labour Code 2026 ss.176-183", "A", ""),
    ("advance_cap_min_wages", "plc_2026", "3", "2026-02-10", "Punjab Labour Code 2026 s.18", "A", "PLC in force: U"),
    ("advance_instalment_cap_bp", "plc_2026", "2000", "2026-02-10", "Punjab Labour Code 2026 s.18", "A", "PLC in force: U"),
]


def v9(conn: sqlite3.Connection) -> None:
    # imported here, not at module level (SEAMS §6: migrations.py imports this module)
    from munshi.domain.migrations import _run_sql
    from munshi.domain.models import now_iso
    _run_sql(conn, V9_SQL)
    for stmt in V9_TRIGGERS:
        conn.execute(stmt)
    if conn.execute("SELECT COUNT(*) FROM statutory_rates").fetchone()[0] == 0:
        stamp = now_iso()
        conn.executemany("INSERT INTO statutory_rates (key, jurisdiction, value, effective_from, source, source_url, verified_on, grade, note, set_by, created_at) "
                         "VALUES (?,?,?,?,?,'',?,?,?,'seed',?)",
                         [(k, j, v, eff, src, VERIFIED_ON, g, note, stamp) for k, j, v, eff, src, g, note in SEEDS])


STEP: Callable[[sqlite3.Connection], None] | None = v9
