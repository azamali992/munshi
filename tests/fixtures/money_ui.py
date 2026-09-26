"""JSON fixtures for the payroll / finance / payment-proof screens (Stream C), in the REST shapes of the plan
(reviews/payroll_finance_plan.md §7 report columns, §9 Streams A and B REST lists) and SEAMS §2 (the table shape).

Every table here is built with the REAL `accounts.table()` / `accounts.col()`, so a fixture can never drift from the
shape the console renders. The figures are the plan's worked month (§10.2 payroll, §10.3 Part B finance).

How the screens use them: they never do. The console and phone code always call the live endpoints. The browser check
(tests/ui_money_check.py) intercepts a request and answers it from here ONLY while the live server has no such route
(404/405) -- the single switch is that harness's --fixtures flag: `auto` (default: fixture only where the API is not
live yet), `all` (every money route from here), `off` (live only). Once Streams A, B and E land, `off` is the check.

Where the plan leaves a response shape open, the choice made here is marked ASSUMED; the screens read exactly these
fields, so an integrator can diff the live response against this file.
"""
from __future__ import annotations

import re
from typing import Any

from munshi.domain.accounts import BOUNDARY_TEXT, col, table

PERIOD = "2026-09"
VERIFIED_ON = "2026-09-26"
BOUNDARY = BOUNDARY_TEXT["en"].format(verified_on=VERIFIED_ON, source="Punjab Gazette 09-09-2025, Finance Act 2026")

# ------------------------------------------------------------------------------------------------ employees (A)
_EMP = [
    # id, emp_no, name, designation, role_hint, basis, basic, daily, pay_method, login(role|None), status, cash_allowed
    ("EMP-BILAL001", "E-001", "Bilal Ahmed", "Munshi / clerk", "clerk", "monthly", 120000, 0, "bank", "clerk", "active", False),
    ("EMP-IMRAN002", "E-002", "Imran Khalid", "Order booker", "salesman", "monthly", 40000, 0, "jazzcash", "salesman", "active", False),
    ("EMP-RAFIQ003", "E-003", "Rafiq Hussain", "Driver", "driver", "monthly", 40000, 0, "cash", "driver", "active", True),
    ("EMP-NADEE004", "E-004", "Nadeem Abbas", "Godown keeper", "godown", "monthly", 40000, 0, "cash", None, "active", True),
    ("EMP-SHAFI005", "E-005", "Shafiq Masih", "Loader", "loader", "daily", 0, 1600, "cash", None, "active", True),
    ("EMP-KASHI006", "E-006", "Kashif Ali", "Loader", "loader", "daily", 0, 1600, "cash", None, "active", False),
    ("EMP-ASLAM007", "E-007", "Aslam Pervaiz", "Guard", "guard", "monthly", 38000, 0, "cash", None, "left", False),
]


def employee(row: tuple, pay: bool) -> dict:
    eid, no, name, desig, hint, basis, basic, daily, method, login, status, cash_ok = row
    e = {"employee_id": eid, "emp_no": no, "name": name, "designation": desig, "role_hint": hint,
         "phone": f"0301-55{no[-3:]}0{no[-1]}", "joined_on": "2025-03-01", "left_on": "2026-06-30" if status == "left" else None,
         "status": status, "user_id": f"U-{eid[-3:]}" if login else None,
         "login": {"role": login, "phone": f"0301-55{no[-3:]}0{no[-1]}", "active": status == "active", "must_change_pin": no == "E-002"} if login else None,
         "province": "punjab", "eobi_covered": basis == "monthly", "ss_covered": False, "cnic_last4": "4417"}
    if pay:   # a caller without payroll:read never receives these keys (Stream A drops them server-side)
        e |= {"pay_basis": basis, "basic": basic, "daily_rate": daily, "pay_method": method, "payee_ref": "PK36HABB0000001123456702" if method == "bank" else "",
              "cash_allowed": cash_ok, "tax_mode": "auto", "components": [{"code": "TRIP", "label": "Trip allowance", "calc": "per_trip", "amount": 500}] if no == "E-003" else []}
    return e


def employees(status: str, pay: bool) -> dict:
    rows = [employee(r, pay) for r in _EMP if status == "all" or r[10] == status]
    cols = [col("emp_no", "No."), col("name", "Name"), col("designation", "Designation"), col("login", "App login", badge=True), col("status", "Status")]
    if pay:
        cols[3:3] = [col("basis", "Basis"), col("rate", "Basic / rate", "money")]
    trows = [{"emp_no": e["emp_no"], "name": e["name"], "designation": e["designation"], "login": (e["login"] or {}).get("role") or "",
              "status": e["status"], "basis": e.get("pay_basis"), "rate": e.get("basic") or e.get("daily_rate")} for e in rows]
    return {"employees": rows, "table": table("Employees", cols, trows)}


# ------------------------------------------------------------------------------------------------ payroll (A)
# the plan's worked September: (emp_no, name, desig, basis, days, absent, basic, ot, allow, comm, bonus, gross,
#   absence, eobi_ee, ss_ee, tax, adv, fines, total_ded, net, eobi_er, ss_er)
_REG = [
    ("E-001", "Bilal Ahmed", "Munshi / clerk", "monthly", 30, 0, 120000, 0, 0, 0, 0, 120000, 0, 370, 0, 2700, 0, 0, 3070, 116930, 1850, 0),
    ("E-002", "Imran Khalid", "Order booker", "monthly", 30, 0, 40000, 0, 0, 3190.80, 0, 43190.80, 0, 370, 0, 0, 5000, 0, 5370, 37820.80, 1850, 0),
    ("E-003", "Rafiq Hussain", "Driver", "monthly", 30, 0, 40000, 0, 1500, 0, 0, 41500, 0, 370, 0, 0, 0, 0, 370, 41130, 1850, 0),
    ("E-004", "Nadeem Abbas", "Godown keeper", "monthly", 28, 2, 40000, 0, 0, 0, 0, 40000, 3076.92, 370, 0, 0, 0, 0, 3446.92, 36553.08, 1850, 0),
    ("E-005", "Shafiq Masih", "Loader", "daily", 22, 0, 35200, 0, 0, 0, 0, 35200, 0, 0, 0, 0, 0, 0, 0, 35200, 0, 0),
    ("E-006", "Kashif Ali", "Loader", "daily", 18, 0, 28800, 0, 0, 0, 0, 28800, 0, 0, 0, 0, 0, 0, 0, 28800, 0, 0),
]
_REG_COLS = [col("emp_no", "No."), col("name", "Name"), col("designation", "Designation"), col("basis", "Basis"),
             col("days", "Days", "days"), col("absent", "Absent", "days"),
             col("basic", "Basic", "money"), col("overtime", "Overtime", "money"), col("allowances", "Allowances", "money"),
             col("commission", "Commission", "money"), col("bonus", "Bonus", "money"), col("gross", "Gross", "money"),
             col("absence", "Absence", "money"), col("eobi_ee", "EOBI (1%)", "money"), col("ss_ee", "Social security", "money"),
             col("income_tax", "Income tax", "money"), col("advance", "Advance", "money"), col("fines_other", "Fines / other", "money"),
             col("total_deductions", "Total deductions", "money"), col("net_pay", "Net pay", "money"),
             col("paid", "Paid", "money"), col("balance_due", "Balance due", "money"), col("method", "Method"),
             col("eobi_er", "EOBI employer (5%)", "money"), col("ss_er", "SS employer", "money"), col("employer_cost", "Employer cost", "money")]
_MONEY_KEYS = [c["key"] for c in _REG_COLS if c["kind"] == "money"]
_METHOD = {"E-001": "bank", "E-002": "jazzcash"}
_EID = {r[1]: r[0] for r in _EMP}


def _reg_rows(paid: dict[str, float]) -> list[dict]:
    out = []
    for r in _REG:
        (no, name, desig, basis, days, absent, basic, ot, allow, comm, bonus, gross, absence, eobi_ee, ss_ee, tax, adv, fines,
         ded, net, eobi_er, ss_er) = r
        p = paid.get(no, 0)
        out.append({"emp_no": no, "name": name, "designation": desig, "basis": basis, "days": days, "absent": absent, "basic": basic,
                    "overtime": ot, "allowances": allow, "commission": comm, "bonus": bonus, "gross": gross, "absence": absence,
                    "eobi_ee": eobi_ee, "ss_ee": ss_ee, "income_tax": tax, "advance": adv, "fines_other": fines, "total_deductions": ded,
                    "net_pay": net, "paid": p, "balance_due": round(net - p, 2), "method": _METHOD.get(no, "cash"),
                    "eobi_er": eobi_er, "ss_er": ss_er, "employer_cost": round(gross + eobi_er + ss_er, 2)})
    return out


def register(paid: dict[str, float] | None = None, title: str = "Payroll register, September 2026") -> dict:
    rows = _reg_rows(paid or {})
    totals = {k: round(sum(r[k] for r in rows), 2) for k in _MONEY_KEYS}
    return table(title, _REG_COLS, rows, totals=totals, note=f"2 warnings. Rates last checked {VERIFIED_ON}.")


def attendance(locked: bool = False) -> dict:
    """GET /api/payroll/{period}/attendance -- clerk-safe (attendance:write): no rupee anywhere, and (confirmed
    against the live route) no `basis` either -- the screen falls back to "monthly" when it is absent. Overtime is
    `ot_hours` (float hours) plus `restday_ot_minutes` / `holiday_ot_minutes` (int minutes, NOT hours: the PUT body
    is a pydantic model with extra="forbid", so a wrong key here 422s the whole save)."""
    rows = []
    for r in _REG:
        no, name, desig, basis, days, absent = r[:6]
        rows.append({"employee_id": _EID[no], "emp_no": no, "name": name, "designation": desig,
                     "recorded": True, "days_worked": days, "unpaid_absent": absent, "annual_leave": 0, "casual_leave": 0,
                     "sick_leave": 0, "ot_hours": 0, "restday_ot_minutes": 0, "holiday_ot_minutes": 0, "trips": 3 if no == "E-003" else 0,
                     "source": "register", "recorded_by": "Bilal Ahmed"})
    return {"period": PERIOD, "days_in_month": 30, "locked": locked, "rows": rows,
            "note": "Days worked and absences in half-day steps. No pay is shown here."}


def adjustments() -> list[dict]:
    return [{"adj_id": "ADJ-7Q2M4K1P", "employee_id": "EMP-IMRAN002", "name": "Imran Khalid", "code": "bonus", "amount": 0.0,
             "note": "placeholder removed", "voided": True},
            {"adj_id": "ADJ-9X3N5T2R", "employee_id": "EMP-NADEE004", "name": "Nadeem Abbas", "code": "other_deduction", "amount": 500.0,
             "note": "uniform, agreed in writing", "voided": False}]


def preview(approved_run: str | None = None) -> dict:
    """GET /api/payroll/{period}/preview (confirmed against the live route): `run` (None until approved -- the
    screen's rail and its Pay/Payslips steps are unreachable without it), `skipped` (employees with no pay terms
    this month), `can_approve`, and `errors` (blocking: the engine will not approve -- PLC limits, an existing run)
    separate from `warnings` (the owner may proceed). Both are plain strings here, not {code, text} objects; the
    screens read `w.text || w` so either shape renders, but the live route sends strings."""
    reg = register()
    return {"period": PERIOD, "profile": "plc_2026", "fingerprint": "9f2c41d0e7ab53c6a8e1f04d2b7c9e35d61a0f8b2c4e7d9013a5b6c8d0e2f4a1",
            "headcount": 6, "totals": {"gross": 308690.80, "deductions": 12256.92, "net": 296433.88, "employer": 7400.00,
                                       "cost": 316090.80, "cash_net": 141683.08, "cashless_net": 154750.80},
            "table": reg,
            "warnings": ["Shafiq Masih is not covered by EOBI although the business has 5 or more staff.",
                         "Punjab minimum wage Rs 40,000 was last checked 2026-09-26; a FY2026-27 notice may be due."],
            "errors": [], "skipped": [], "can_approve": approved_run is None,
            "adjustments": adjustments(),
            "run": {"run_id": approved_run, "period": PERIOD, "approved_by": "Haji Rasheed", "approved_at": "2026-09-30T10:15:00Z"} if approved_run else None,
            "rules_verified_on": VERIFIED_ON, "boundary": BOUNDARY}


def slips(paid: dict[str, float] | None = None) -> list[dict]:
    paid = paid or {}
    out = []
    for i, r in enumerate(_REG, start=1):
        no, name, net = r[0], r[1], r[19]
        eid = _EID[no]
        cash_ok = next(e[11] for e in _EMP if e[1] == no)
        out.append({"slip_id": f"PSL-2026-{i:06d}", "employee_id": eid, "emp_no": no, "name": name, "designation": r[2],
                    "net": net, "paid": paid.get(no, 0), "balance_due": round(net - paid.get(no, 0), 2), "pay_method": _METHOD.get(no, "cash"),
                    "pay_account_id": "ACC-HBL" if no == "E-001" else ("ACC-JAZZ" if no == "E-002" else "CASH"), "cash_allowed": cash_ok})
    return out


def run(run_id: str, paid: dict[str, float] | None = None) -> dict:
    """GET /api/payroll/runs/{run_id}. ASSUMED: {run, table (register with paid / balance due), payslips}."""
    return {"run": {"run_id": run_id, "period": PERIOD, "kind": "regular", "profile": "plc_2026", "approved_by": "Haji Rasheed",
                    "created_at": "2026-09-30T10:15:00Z", "gross": 308690.80, "deductions": 12256.92, "net": 296433.88, "employer": 7400.00,
                    "headcount": 6, "reversed_by": None},
            "table": register(paid, title=f"Payroll register {run_id}"), "payslips": slips(paid)}


def payslip(slip_id: str = "PSL-2026-000002") -> dict:
    """GET /api/payslips/{slip_id} (owner, and self via payroll:self -- confirmed against the live route). meta uses
    `business_name` (not `business`) and `cash_exemption` (not `cash_allowed`); net/gross/deductions/status live at
    the TOP level, not inside meta (meta keeps its own, separately-named `paid_status`)."""
    cols = [col("item", "Item"), col("earnings", "Earnings", "money"), col("deductions", "Deductions", "money")]
    rows = [{"item": "Basic (30 days × Rs 1,333.33)", "earnings": 40000.00},
            {"item": "Gross", "earnings": 40000.00, "_em": True},
            {"item": "EOBI (employee 1%)", "deductions": 370.00},
            {"item": "Income tax", "deductions": 0.00},
            {"item": "Advance recovery ADV-2026-000001", "deductions": 5000.00},
            {"item": "Total deductions", "deductions": 5370.00, "_em": True},
            {"item": "Net remuneration", "earnings": 34630.00, "_em": True},
            {"item": "Commission (1% of Rs 319,080 delivered)", "earnings": 3190.80}]
    t = table("Payslip September 2026", cols, rows, totals={"earnings": 43190.80, "deductions": 5370.00, "net": 37820.80, "total_payment": 37820.80},
              note=BOUNDARY)
    return {"slip_id": slip_id, "employee_id": "EMP-IMRAN002", "table": t, "gross": 43190.80, "deductions": 5370.00, "net": 37820.80, "status": "paid",
            "meta": {"slip_no": slip_id, "period": PERIOD, "period_label": "September 2026", "emp_no": "E-002", "name": "Imran Khalid",
                     "designation": "Order booker", "cnic_last4": "4417", "joined_on": "2025-03-01", "pay_basis": "monthly",
                     "days_worked": 30, "unpaid_absent": 0, "paid_leave": {"annual": 0, "casual": 0, "sick": 0},
                     "leave_balances": {"annual": 18, "casual": 10, "sick": 8}, "pay_method": "jazzcash", "cash_exemption": False,
                     "paid_status": "paid", "ytd_taxable": 123190.80, "ytd_tax": 0,
                     "employer_eobi": 1850.00, "rules_verified_on": VERIFIED_ON, "business_name": "Punjab Agro Distributors",
                     "boundary": BOUNDARY}}


def my_payslips() -> dict:
    """GET /api/me/payslips (confirmed against the live route): a LIGHT list -- slip_id, period, month, gross,
    deductions, net, paid, status -- not a whole payslip each; the phone fetches the full payslip (meta + table) on
    demand from GET /api/payslips/{slip_id} when a slip is opened, the same call an owner makes."""
    s1 = payslip("PSL-2026-000002")
    s0 = payslip("PSL-2026-000000")
    s0["meta"] = s0["meta"] | {"period": "2026-08", "period_label": "August 2026"}
    s0["net"], s0["gross"], s0["deductions"] = 39630.0, 40000.0, 370.0
    s0["table"] = table("Payslip August 2026", s0["table"]["columns"],
                        [{"item": "Basic (31 days)", "earnings": 40000.0}, {"item": "Gross", "earnings": 40000.0, "_em": True},
                         {"item": "EOBI (employee 1%)", "deductions": 370.0}, {"item": "Total deductions", "deductions": 370.0, "_em": True},
                         {"item": "Net remuneration", "earnings": 39630.0, "_em": True}],
                        totals={"earnings": 40000.0, "deductions": 370.0}, note=BOUNDARY)
    light = lambda s: {"slip_id": s["slip_id"], "period": s["meta"]["period"], "month": s["meta"]["period_label"],
                        "gross": s["gross"], "deductions": s["deductions"], "net": s["net"], "paid": s["net"], "status": s["status"]}
    return {"payslips": [light(s1), light(s0)]}


def advances() -> dict:
    cols = [col("adv_no", "Advance"), col("name", "Name"), col("kind", "Kind"), col("given_on", "Given", "date"),
            col("amount", "Amount", "money"), col("recovered", "Recovered", "money"), col("repaid_direct", "Repaid direct", "money"),
            col("outstanding", "Outstanding", "money"), col("instalment", "Instalment", "money"), col("next_period", "Next"),
            col("method", "Method"), col("flags", "Flags", badge=True)]
    rows = [{"adv_no": "ADV-2026-000001", "advance_id": "ADV-2026-000001", "employee_id": "EMP-IMRAN002", "name": "Imran Khalid", "kind": "advance",
             "given_on": "2026-09-10", "amount": 10000.0, "recovered": 5000.0, "repaid_direct": 0.0, "outstanding": 5000.0,
             "instalment": 5000.0, "next_period": "2026-10", "method": "jazzcash", "flags": ""}]
    t = table("Staff advances (open)", cols, rows, totals={"amount": 10000.0, "recovered": 5000.0, "repaid_direct": 0.0, "outstanding": 5000.0})
    schedule = [{"advance_id": "ADV-2026-000001", "period": "2026-09", "amount": 5000.0, "status": "recovered", "run_id": "PAY-2026-000001"},
                {"advance_id": "ADV-2026-000001", "period": "2026-10", "amount": 5000.0, "status": "due", "run_id": None}]
    return {"table": t, "advances": rows, "schedule": schedule,
            "limits": {"profile": "plc_2026", "max_advance": 120000.0, "max_instalment_pct": 20, "one_open": True, "cashless_only": True}}


def statutory(kind: str) -> dict:
    if kind == "eobi":
        cols = [col("emp_no", "No."), col("name", "Name"), col("cnic", "CNIC"), col("eobi_no", "EOBI no."), col("wage_base", "Wage base", "money"),
                col("employee_1pct", "Employee 1%", "money"), col("employer_5pct", "Employer 5%", "money"), col("total", "Total", "money")]
        rows = [{"emp_no": r[0], "name": r[1], "cnic": "•••••••••4417", "eobi_no": f"PB-{i:05d}", "wage_base": 37000.0, "employee_1pct": 370.0,
                 "employer_5pct": 1850.0, "total": 2220.0} for i, r in enumerate(_REG[:4], start=1)]
        t = table("EOBI, September 2026", cols, rows, totals={"wage_base": 148000.0, "employee_1pct": 1480.0, "employer_5pct": 7400.0, "total": 8880.0},
                  note=f"Deposit with EOBI by the 15th of next month (verify); base Rs 37,000, last checked {VERIFIED_ON}.")
        return {"kind": kind, "period": PERIOD, "table": t, "due": 8880.0, "paid": 0.0, "boundary": BOUNDARY}
    if kind == "income_tax":
        cols = [col("emp_no", "No."), col("name", "Name"), col("cnic", "CNIC"), col("taxable_this_month", "Taxable this month", "money"),
                col("ytd_taxable_before", "YTD taxable before", "money"), col("projected_annual", "Projected annual", "money"),
                col("annual_tax", "Annual tax", "money"), col("ytd_tax_before", "YTD tax before", "money"), col("tax_this_month", "Tax this month", "money")]
        rows = [{"emp_no": "E-001", "name": "Bilal Ahmed", "cnic": "•••••••••4417", "taxable_this_month": 120000.0, "ytd_taxable_before": 240000.0,
                 "projected_annual": 1440000.0, "annual_tax": 32400.0, "ytd_tax_before": 5400.0, "tax_this_month": 2700.0}]
        t = table("Salary tax withheld, September 2026", cols, rows, totals={"taxable_this_month": 120000.0, "tax_this_month": 2700.0},
                  note="Deposit and filing dates are contested: confirm on IRIS with your advisor.")
        return {"kind": kind, "period": PERIOD, "table": t, "due": 2700.0, "paid": 0.0, "boundary": BOUNDARY}
    cols = [col("emp_no", "No."), col("name", "Name"), col("ss_no", "SS no."), col("wages", "Wages", "money"),
            col("contributable_wage", "Contributable", "money"), col("employer_share", "Employer", "money"),
            col("worker_share", "Worker", "money"), col("total", "Total", "money")]
    t = table("Social security, September 2026", cols, [], totals=None,
              note="Nobody is covered by PESSI. The Punjab wage ceiling is not set (grade U): the engine will not compute PESSI until it is.")
    return {"kind": "ss", "period": PERIOD, "table": t, "due": 0.0, "paid": 0.0, "boundary": BOUNDARY}


def rates() -> dict:
    rows = [
        {"key": "min_wage_monthly", "jurisdiction": "punjab", "value": "40000", "unit": "Rs", "effective_from": "2025-07-01", "source": "Punjab Gazette 09-09-2025",
         "source_url": "https://labour.punjab.gov.pk/", "verified_on": VERIFIED_ON, "grade": "A", "note": "check for a FY2026-27 notice"},
        {"key": "eobi_wage_base", "jurisdiction": "pk", "value": "37000", "unit": "Rs", "effective_from": "2024-07-01", "source": "Employsome / Peoplifi (2026)",
         "source_url": "", "verified_on": VERIFIED_ON, "grade": "U", "note": "VERIFY with EOBI; may be 40,000 or 40,700"},
        {"key": "eobi_employee_bp", "jurisdiction": "pk", "value": "1", "unit": "%", "effective_from": "2023-07-01", "source": "EOBI letter (2023)",
         "source_url": "", "verified_on": VERIFIED_ON, "grade": "B", "note": ""},
        {"key": "eobi_employer_bp", "jurisdiction": "pk", "value": "5", "unit": "%", "effective_from": "2023-07-01", "source": "EOBI letter (2023)",
         "source_url": "", "verified_on": VERIFIED_ON, "grade": "B", "note": ""},
        {"key": "ss_wage_ceiling", "jurisdiction": "punjab", "value": "unknown", "unit": "Rs", "effective_from": "", "source": "not found",
         "source_url": "", "verified_on": VERIFIED_ON, "grade": "U", "note": "PESSI is not computed until this is set"},
        {"key": "salary_tax_slabs", "jurisdiction": "pk", "value": "TY2027 slabs", "unit": "", "effective_from": "2026-07-01", "source": "Finance Act 2026, Gazette 26-06-2026",
         "source_url": "https://fbr.gov.pk/", "verified_on": VERIFIED_ON, "grade": "A", "note": ""},
    ]
    return {"rates": rows, "profile": "plc_2026", "boundary": BOUNDARY}


def payroll_settings() -> dict:
    return {"payroll_profile": "plc_2026", "payroll_province": "punjab", "eobi_registered": "1", "ss_registered": "0",
            "payroll_pay_day": "7", "payroll_tax_round_rupee": "1", "payroll_enabled": "1", "finance_start_date": "2026-09-01"}


# ------------------------------------------------------------------------------------------------ finance (B)
_ACCOUNTS = [
    {"account_id": "CASH", "kind": "cash", "name": "Cash in hand (galla)", "provider": "", "number_last4": "", "balance": 46117.17,
     "as_of": "2026-09-30", "last_reconciled": None, "uncleared_in": 0.0, "uncleared_out": 0.0, "last_statement_balance": None,
     "difference": None, "is_default": True, "active": True, "today_in": 20000.0, "today_out": 142883.08},
    {"account_id": "ACC-HBL", "kind": "bank", "name": "HBL current", "provider": "HBL", "number_last4": "0702", "balance": 373070.00,
     "as_of": "2026-09-30", "last_reconciled": "2026-08-31", "uncleared_in": 0.0, "uncleared_out": 116930.0, "last_statement_balance": 490000.0,
     "difference": 0.0, "is_default": True, "active": True, "today_in": 0.0, "today_out": 116930.0},
    {"account_id": "ACC-JAZZ", "kind": "wallet", "name": "JazzCash business", "provider": "jazzcash", "number_last4": "4567", "balance": 12179.20,
     "as_of": "2026-09-30", "last_reconciled": None, "uncleared_in": 0.0, "uncleared_out": 0.0, "last_statement_balance": None,
     "difference": None, "is_default": True, "active": True, "today_in": 0.0, "today_out": 37820.80},
]


def money_accounts() -> dict:
    cols = [col("account", "Account"), col("kind", "Kind"), col("balance", "Balance", "money"), col("as_of", "As of", "date"),
            col("last_reconciled", "Last reconciled", "date"), col("uncleared_in", "Uncleared in", "money"),
            col("uncleared_out", "Uncleared out", "money"), col("last_statement_balance", "Last statement", "money"),
            col("difference", "Difference", "money")]
    rows = [{"account": a["name"]} | {k: a[k] for k in ("kind", "balance", "as_of", "last_reconciled", "uncleared_in", "uncleared_out",
                                                          "last_statement_balance", "difference")} for a in _ACCOUNTS]
    return {"accounts": _ACCOUNTS, "total": 431366.37, "today_in": 20000.0, "today_out": 297633.88,
            "table": table("Money accounts", cols, rows, totals={"balance": 431366.37})}


def account_book(account_id: str, redact: bool) -> dict:
    cols = [col("date", "Date", "date"), col("doc", "Doc"), col("kind", "Kind"), col("narration", "Narration"),
            col("money_in", "In", "money"), col("money_out", "Out", "money"), col("balance", "Balance", "money"),
            col("cleared", "Cleared", badge=True), col("by", "By")]
    if account_id == "ACC-HBL":
        rows = [{"date": "2026-09-01", "doc": "", "kind": "", "narration": "Opening balance", "balance": 500000.0, "_em": True},
                {"date": "2026-09-01", "doc": "JV-2026-000001", "kind": "opening", "narration": "Opening balances", "money_in": 500000.0, "balance": 500000.0, "cleared": "cleared", "by": "Haji Rasheed"},
                {"date": "2026-09-05", "doc": "JV-2026-000002", "kind": "loan", "narration": "Loan from Haji Rasheed (informal)", "money_in": 200000.0, "balance": 700000.0, "cleared": "cleared", "by": "Haji Rasheed"},
                {"date": "2026-09-05", "doc": "XFR-2026-000001", "kind": "transfer", "narration": "To JazzCash business", "money_out": 60000.0, "balance": 640000.0, "cleared": "cleared", "by": "Bilal Ahmed"},
                {"date": "2026-09-12", "doc": "RCP-2026-000014", "kind": "payment", "narration": "Rana Brothers (cheque)", "money_in": 100000.0, "balance": 740000.0, "cleared": "cleared", "by": "Bilal Ahmed"},
                {"date": "2026-09-15", "doc": "RCP-2026-000014R", "kind": "reversal", "narration": "Cheque bounced: Rana Brothers", "money_out": 100000.0, "balance": 640000.0, "cleared": "cleared", "by": "Bilal Ahmed"},
                {"date": "2026-09-20", "doc": "SUP-2026-000006", "kind": "supplier payment", "narration": "Fauji Fertilizer (IBFT)", "money_out": 150000.0, "balance": 490000.0, "cleared": "cleared", "by": "Haji Rasheed"},
                ({"date": "2026-09-30", "doc": "", "kind": "staff", "narration": "Staff payments (owner only)", "money_out": 116930.0, "balance": 373070.0, "cleared": "", "by": ""} if redact else
                 {"date": "2026-09-30", "doc": "SPM-2026-000001", "kind": "salary", "narration": "Salary Bilal Ahmed (PSL-2026-000001)", "money_out": 116930.0, "balance": 373070.0, "cleared": "", "by": "Haji Rasheed"}),
                {"date": "2026-09-30", "doc": "", "kind": "", "narration": "Closing balance", "balance": 373070.0, "_em": True}]
        return {"account": _ACCOUNTS[1], "start": "2026-09-01", "end": "2026-09-30", "opening": 0.0, "closing": 373070.0,
                "table": table("HBL current, September 2026", cols, rows, totals={"money_in": 800000.0, "money_out": 426930.0})}
    acc = next((a for a in _ACCOUNTS if a["account_id"] == account_id), _ACCOUNTS[0])
    rows = [{"date": "2026-09-01", "doc": "", "narration": "Opening balance", "balance": 400000.0, "_em": True},
            {"date": "2026-09-25", "doc": "JV-2026-000004", "kind": "drawing", "narration": "Owner drawing (home)", "money_out": 30000.0, "balance": 370000.0, "by": "Haji Rasheed"},
            {"date": "2026-09-30", "doc": "", "narration": "Closing balance", "balance": acc["balance"], "_em": True}]
    return {"account": acc, "start": "2026-09-01", "end": "2026-09-30", "opening": 400000.0, "closing": acc["balance"],
            "table": table(f"{acc['name']}, September 2026", cols, rows)}


def reconciliation(cleared: set[str]) -> dict:
    """GET /api/accounts/{id}/reconciliation?statement_date= (confirmed against the live route): only what is still
    UNCLEARED, split by direction with unsigned amounts (`uncleared_in` / `uncleared_out`), never a unified `items`
    list with a `cleared` flag, no `doc` (the document reference is `source_id`), and the book balance is `book`, not
    `book_balance`. No `last` key either. The screen normalises this into the unified, signed, tickable list it
    ticks off client-side; an item ticked (POST .../clear) simply does not come back on the next statement_date."""
    all_items = [{"source": "salary_payment", "source_id": "SPM-2026-000001", "date": "2026-09-30", "narration": "Salary Bilal Ahmed", "amount": -116930.0},
                 {"source": "ledger", "source_id": "RCP-2026-000014", "date": "2026-09-12", "narration": "Rana Brothers (cheque)", "amount": 100000.0},
                 {"source": "ledger", "source_id": "RCP-2026-000014R", "date": "2026-09-15", "narration": "Cheque bounced", "amount": -100000.0},
                 {"source": "supplier_ledger", "source_id": "SUP-2026-000006", "date": "2026-09-20", "narration": "Fauji Fertilizer (IBFT)", "amount": -150000.0},
                 {"source": "transfer", "source_id": "XFR-2026-000001", "date": "2026-09-05", "narration": "To JazzCash business", "amount": -60000.0}]
    still_open = [it for it in all_items if it["source_id"] not in cleared]
    uin = [{k: v for k, v in it.items() if k != "amount"} | {"amount": it["amount"]} for it in still_open if it["amount"] > 0]
    uout = [{k: v for k, v in it.items() if k != "amount"} | {"amount": -it["amount"]} for it in still_open if it["amount"] < 0]
    return {"account_id": "ACC-HBL", "statement_date": "2026-09-30", "book": 373070.0,
            "uncleared_in": uin, "uncleared_out": uout,
            "uncleared_in_total": sum(i["amount"] for i in uin), "uncleared_out_total": sum(i["amount"] for i in uout),
            "adjusted": None, "statement": None, "difference": None}


def _stmt(title: str, rows: list[dict], cols: list[dict], note: str | None = None) -> dict:
    return table(title, cols, rows, note=note)


def pnl(clerk: bool = False) -> dict:
    cols = [col("line", "Line"), col("amount", "Amount", "money"), col("pct_of_revenue", "% of revenue", "pct"),
            col("prior_period", "August", "money"), col("change", "Change", "money")]
    staff = ([{"line": "Staff costs", "amount": -313013.88, "pct_of_revenue": -65.5, "prior_period": 0.0, "change": -313013.88}] if clerk else
             [{"line": "  Salaries", "amount": -236923.08, "pct_of_revenue": -49.6, "prior_period": 0.0, "change": -236923.08},
              {"line": "  Wages", "amount": -64000.0, "pct_of_revenue": -13.4, "prior_period": 0.0, "change": -64000.0},
              {"line": "  Allowances", "amount": -1500.0, "pct_of_revenue": -0.3, "prior_period": 0.0, "change": -1500.0},
              {"line": "  Commission", "amount": -3190.80, "pct_of_revenue": -0.7, "prior_period": 0.0, "change": -3190.80},
              {"line": "  Employer contributions", "amount": -7400.0, "pct_of_revenue": -1.5, "prior_period": 0.0, "change": -7400.0},
              {"line": "Staff costs", "amount": -313013.88, "pct_of_revenue": -65.5, "prior_period": 0.0, "change": -313013.88, "_em": True}])
    rows = [{"line": "Sales", "amount": 482580.0, "pct_of_revenue": 101.0, "prior_period": 401200.0, "change": 81380.0},
            {"line": "Credit notes", "amount": -5000.0, "pct_of_revenue": -1.0, "prior_period": 0.0, "change": -5000.0},
            {"line": "Net revenue", "amount": 477580.0, "pct_of_revenue": 100.0, "prior_period": 401200.0, "change": 76380.0, "_em": True},
            {"line": "Cost of goods sold", "amount": -463429.10, "pct_of_revenue": -97.0, "prior_period": -380100.0, "change": -83329.10},
            {"line": "Gross profit", "amount": 14150.90, "pct_of_revenue": 3.0, "prior_period": 21100.0, "change": -6949.10, "_em": True},
            {"line": "  Fuel", "amount": -5500.0, "pct_of_revenue": -1.2, "prior_period": -6100.0, "change": 600.0},
            {"line": "  Cash shortage", "amount": -500.0, "pct_of_revenue": -0.1, "prior_period": 0.0, "change": -500.0},
            *staff,
            {"line": "Stock write-offs / adjustments", "amount": -11121.25, "pct_of_revenue": -2.3, "prior_period": 0.0, "change": -11121.25},
            {"line": "Depreciation", "amount": -40000.0, "pct_of_revenue": -8.4, "prior_period": 0.0, "change": -40000.0},
            {"line": "Net profit", "amount": -355984.23, "pct_of_revenue": -74.5, "prior_period": 15000.0, "change": -370984.23, "_em": True}]
    main = table("Profit and loss, 1–30 September 2026", cols, rows, note="Compared with August 2026. Cost is missing for 0 lines.")
    rc = table("Reconciliation to operating profit", [col("line", "Line"), col("amount", "Amount", "money")],
               [{"line": "profit_summary net", "amount": -304862.98}, {"line": "less stock adjustments", "amount": -11121.25},
                {"line": "less depreciation", "amount": -40000.0}, {"line": "equals net profit", "amount": -355984.23, "_em": True}])
    return {"start": "2026-09-01", "end": "2026-09-30", "net": -355984.23, "table": main, "tables": [main, rc]}


def balance_sheet() -> dict:
    cols = [col("line", "Line"), col("amount", "Amount", "money")]
    rows = [{"line": "ASSETS", "_em": True},
            {"line": "Cash in hand (galla)", "amount": 46117.17}, {"line": "HBL current", "amount": 373070.0}, {"line": "JazzCash business", "amount": 12179.20},
            {"line": "Trade receivables", "amount": 432579.75}, {"line": "Staff advances", "amount": 5000.0},
            {"line": "Stock in godowns", "amount": 1300559.65}, {"line": "Fixed assets at cost", "amount": 2400000.0},
            {"line": "Less accumulated depreciation", "amount": -40000.0}, {"line": "Total assets", "amount": 4529505.77, "_em": True},
            {"line": "LIABILITIES", "_em": True},
            {"line": "Trade payables", "amount": 870110.0}, {"line": "Salaries payable", "amount": 28800.0}, {"line": "Income tax withheld", "amount": 2700.0},
            {"line": "EOBI payable", "amount": 8880.0}, {"line": "Loan: Haji Rasheed", "amount": 200000.0},
            {"line": "Total liabilities", "amount": 1110490.0, "_em": True},
            {"line": "EQUITY", "_em": True},
            {"line": "Opening balance equity", "amount": 3805000.0}, {"line": "Profit for the period", "amount": -355984.23},
            {"line": "Drawings", "amount": -30000.0}, {"line": "Total equity", "amount": 3419015.77, "_em": True},
            {"line": "Assets − (liabilities + equity)", "amount": 0.0, "_em": True}]
    return {"as_of": "2026-09-30", "difference": 0.0, "alarms": [], "table": table("Balance sheet at 30 September 2026", cols, rows)}


def cash_flow() -> dict:
    cols = [col("line", "Line"), col("amount", "Amount", "money")]
    rows = [{"line": "Opening money", "amount": 900000.0, "_em": True},
            {"line": "Received from customers", "amount": 94500.25}, {"line": "Paid to suppliers", "amount": -450000.0},
            {"line": "Expenses paid", "amount": -5500.0}, {"line": "Salaries paid", "amount": -267633.88}, {"line": "Staff advances (net)", "amount": -10000.0},
            {"line": "Operating", "amount": -638633.63, "_em": True},
            {"line": "Loans received", "amount": 200000.0}, {"line": "Drawings", "amount": -30000.0}, {"line": "Financing", "amount": 170000.0, "_em": True},
            {"line": "Net change", "amount": -468633.63, "_em": True}, {"line": "Closing money", "amount": 431366.37, "_em": True},
            {"line": "Check: sum of accounts", "amount": 431366.37}]
    return {"start": "2026-09-01", "end": "2026-09-30", "table": table("Cash flow, September 2026", cols, rows, note="Memo: transfers between own accounts Rs 60,000.")}


def trial_balance() -> dict:
    cols = [col("code", "Code"), col("account", "Account"), col("debit", "Debit", "money"), col("credit", "Credit", "money")]
    rows = [("1000", "Money accounts", 431366.37, 0), ("1100", "Trade receivables", 432579.75, 0), ("1150", "Staff advances", 5000.0, 0),
            ("1200", "Stock in godowns", 1300559.65, 0), ("1500", "Fixed assets at cost", 2400000.0, 0), ("1510", "Accumulated depreciation", 0, 40000.0),
            ("2000", "Trade payables", 0, 870110.0), ("2100", "Salaries payable", 0, 28800.0), ("2110", "Income tax withheld", 0, 2700.0),
            ("2120", "EOBI payable", 0, 8880.0), ("2200", "Loans payable", 0, 200000.0), ("3010", "Opening balance equity", 0, 3805000.0),
            ("3100", "Drawings", 30000.0, 0), ("4000", "Sales", 0, 482580.0), ("4010", "Sales returns", 5000.0, 0),
            ("5000", "Cost of goods sold", 463429.10, 0), ("5100", "Stock adjustments", 11121.25, 0), ("6000", "Operating expenses", 6000.0, 0),
            ("6100", "Salaries", 236923.08, 0), ("6110", "Wages", 64000.0, 0), ("6120", "Allowances", 1500.0, 0), ("6130", "Commission", 3190.80, 0),
            ("6200", "Employer contributions", 7400.0, 0), ("6300", "Depreciation", 40000.0, 0)]
    trows = [{"code": c, "account": a, "debit": d or None, "credit": cr or None} for c, a, d, cr in rows]
    d, c = round(sum(r[2] for r in rows), 2), round(sum(r[3] for r in rows), 2)
    return {"as_of": "2026-09-30", "balanced": d == c, "table": table("Trial balance at 30 September 2026", cols, trows, totals={"debit": d, "credit": c})}


def kpis() -> dict:
    cols = [col("kpi", "KPI"), col("value", "Value", "qty"), col("unit", "Unit"), col("prior", "Prior", "qty"), col("note", "Note")]
    rows = [{"kpi": "Revenue MTD", "value": 477580, "unit": "Rs", "prior": 401200, "note": ""},
            {"kpi": "Gross margin", "value": 3.0, "unit": "%", "prior": 5.3, "note": ""},
            {"kpi": "Net profit MTD", "value": -355984, "unit": "Rs", "prior": 15000, "note": "payroll month"},
            {"kpi": "Cash + bank + wallets", "value": 431366, "unit": "Rs", "prior": 900000, "note": ""},
            {"kpi": "DSO", "value": 38, "unit": "days", "prior": 41, "note": "AR ÷ credit sales per day (90 d)"},
            {"kpi": "DPO", "value": 52, "unit": "days", "prior": 47, "note": ""},
            {"kpi": "Stock days", "value": 84, "unit": "days", "prior": 90, "note": ""},
            {"kpi": "Payroll % of revenue", "value": 65.5, "unit": "%", "prior": 0, "note": ""},
            {"kpi": "Collection rate MTD", "value": 71.2, "unit": "%", "prior": 68.0, "note": ""}]
    return {"as_of": "2026-09-30", "table": table("Owner KPIs", cols, rows)}


def margins(by: str) -> dict:
    cols = [col("name", by.title()), col("qty", "Qty", "qty"), col("revenue", "Revenue", "money"), col("cost", "Cost", "money"),
            col("margin", "Margin", "money"), col("margin_pct", "Margin %", "pct")]
    rows = [{"name": "Urea 50kg", "qty": 70, "revenue": 269500.0, "cost": 262000.0, "margin": 7500.0, "margin_pct": 2.8},
            {"name": "DAP 50kg", "qty": 20, "revenue": 208080.0, "cost": 201429.10, "margin": 6650.90, "margin_pct": 3.2}]
    return {"by": by, "table": table(f"Margins by {by}", cols, rows, totals={"qty": 90, "revenue": 477580.0, "cost": 463429.10, "margin": 14150.90})}


def assets() -> dict:
    cols = [col("asset", "Asset"), col("category", "Category"), col("acquired_on", "Acquired", "date"), col("cost", "Cost", "money"),
            col("life_months", "Life (months)", "qty"), col("monthly_dep", "Monthly dep.", "money"), col("acc_dep", "Acc. dep.", "money"),
            col("book_value", "Book value", "money"), col("status", "Status")]
    rows = [{"asset_id": "FA-SHZ24001", "asset": "Hyundai Shehzore LES-2231", "category": "vehicle", "acquired_on": "2026-09-01", "cost": 2400000.0,
             "life_months": 60, "monthly_dep": 40000.0, "acc_dep": 40000.0, "book_value": 2360000.0, "status": "in use"}]
    return {"assets": rows, "depreciated_through": "2026-09", "table": table("Fixed assets", cols, rows, totals={"cost": 2400000.0, "acc_dep": 40000.0, "book_value": 2360000.0})}


def loans() -> dict:
    cols = [col("loan", "Loan"), col("lender", "Lender"), col("kind", "Kind"), col("received", "Received", "money"), col("repaid", "Repaid", "money"),
            col("interest_paid", "Interest paid", "money"), col("outstanding", "Outstanding", "money"), col("terms", "Terms")]
    rows = [{"loan_id": "LN-HR7Q2M4K", "loan": "LN-HR7Q2M4K", "lender": "Haji Rasheed", "kind": "informal", "received": 200000.0, "repaid": 0.0,
             "interest_paid": 0.0, "outstanding": 200000.0, "terms": "no markup; repay by Dec"}]
    return {"loans": rows, "table": table("Loans", cols, rows, totals={"received": 200000.0, "outstanding": 200000.0})}


def periods(closed_through: str | None) -> dict:
    """GET /api/finance/periods (confirmed against the live route): `locked_through` (not `through_date`), `closes`
    (through_date/closed_by/closed_at/note/reopened_by/reopened_at/reopen_reason -- this part matches exactly), and
    `table`. NOT confirmed, in fact confirmed ABSENT from the live route: `checklist` and `suggested_through`. No
    pre-close checklist (payroll approved / depreciation run / cash counted / banks reconciled / clearing accounts
    zero / trial balance balanced) is computed server-side yet -- close.js degrades to an empty "0/0 all done"
    checklist, which reads as an all-clear rather than "not available". Kept here, still marked ASSUMED, as the
    target shape for when that lands; a truthful `--fixtures off` run will not see it."""
    closes = [{"close_id": 1, "through_date": "2026-08-31", "closed_by": "Haji Rasheed", "closed_at": "2026-09-03T08:00:00Z", "note": "August",
               "reopened_by": None, "reopened_at": None, "reopen_reason": None}]
    if closed_through:
        closes.insert(0, {"close_id": 2, "through_date": closed_through, "closed_by": "Haji Rasheed", "closed_at": "2026-10-02T08:00:00Z",
                          "note": "September", "reopened_by": None, "reopened_at": None, "reopen_reason": None})
    checklist = [{"key": "payroll", "label": "September payroll approved", "ok": True, "detail": "PAY-2026-000001"},   # ASSUMED: not computed live yet
                 {"key": "depreciation", "label": "Depreciation run through September", "ok": True, "detail": "Rs 40,000"},
                 {"key": "cash_count", "label": "Cash counted at month end", "ok": False, "detail": "last count 2026-09-14"},
                 {"key": "banks", "label": "Banks reconciled", "ok": False, "detail": "HBL last reconciled 2026-08-31"},
                 {"key": "clearing", "label": "Driver cash, stock on vehicles, goods not billed at zero", "ok": True, "detail": "1050, 1210, 2050 = 0"},
                 {"key": "tb", "label": "Trial balance balances", "ok": True, "detail": "debits = credits"}]
    return {"locked_through": closed_through, "closes": closes, "checklist": checklist, "suggested_through": "2026-09-30"}


# ------------------------------------------------------------------------------------------------ routing table
Handler = Any   # (method, path, query, body, role, state) -> (status, json)


def _j(v: Any, status: int = 200):
    return lambda *a: (status, v)


def routes() -> list[tuple[str, str, Any]]:
    """(METHOD, path regex, handler). Handlers take (m: re.Match, query: dict, body: dict|None, role: str, state: dict)
    and return (status, json). `state` lets a flow run end to end in the browser (approve -> run exists -> pay)."""
    def emp_list(m, q, b, role, st):
        return 200, employees(q.get("status", "active"), pay=role == "owner")

    def emp_add(m, q, b, role, st):
        if role != "owner":
            return 403, {"detail": "only the owner can add employees"}
        b = b or {}
        e = {"employee_id": "EMP-NEW00001", "emp_no": "E-008", "name": b.get("name", ""), "designation": b.get("designation", ""),
             "role_hint": b.get("role_hint", "other"), "phone": b.get("phone", ""), "joined_on": b.get("joined_on"), "status": "active", "login": None}
        out: dict = {"employee": e}
        lg = b.get("app_login")
        if lg:
            out["login"] = {"user_id": "U-NEW", "phone": lg.get("phone") or b.get("phone"), "role": lg.get("role"), "must_change_pin": True}
            if lg.get("generate"):
                out["login"] |= {"pin_once": "483920", "whatsapp_text": f"Assalam o alaikum {e['name'].split(' ')[0]}, Munshi app par aap ka login: "
                                 f"{lg.get('phone') or b.get('phone')}, PIN 483920. Pehli dafa sign-in par naya PIN banayein. http://127.0.0.1/"}
        return 201, out

    def emp_login(m, q, b, role, st):
        b = b or {}
        out = {"login": {"user_id": "U-X", "phone": b.get("phone", "0301-5500505"), "role": b.get("role", "driver"), "must_change_pin": True}}
        if b.get("generate", True) and not b.get("pin"):
            out["login"] |= {"pin_once": "705913", "whatsapp_text": "Assalam o alaikum, Munshi app par aap ka naya PIN 705913 he. Pehli dafa sign-in par naya PIN banayein. http://127.0.0.1/"}
        return 200, out

    def preview_h(m, q, b, role, st):
        if role != "owner":
            return 403, {"detail": "payroll is the owner's"}
        return 200, preview(st.get("run_id"))

    def approve_h(m, q, b, role, st):
        if (b or {}).get("fingerprint") != preview()["fingerprint"]:
            return 409, {"detail": "attendance or adjustments changed after this preview: preview again, then approve"}
        st["run_id"] = "PAY-2026-000001"
        return 200, {"run_id": "PAY-2026-000001", "period": PERIOD, "net": 296433.88, "cost": 316090.80, "payslips": 6}

    def run_h(m, q, b, role, st):
        return 200, run(m.group(1), st.get("paid"))

    def pay_h(m, q, b, role, st):
        pays = (b or {}).get("payments", [])
        cash_bad = [p for p in pays if p.get("method") == "cash" and not next(s for s in slips() if s["slip_id"] == p["slip_id"])["cash_allowed"]]
        if cash_bad:
            s = next(s for s in slips() if s["slip_id"] == cash_bad[0]["slip_id"])
            return 400, {"detail": f"Refused under the Punjab Labour Code 2026 (s.165(5)): {s['name']}'s salary must go by bank, JazzCash, "
                                   "Easypaisa or cheque. The owner can allow cash for this employee on the Employees screen, or switch the "
                                   "payroll profile in Settings."}
        paid = st.setdefault("paid", {})
        for p in pays:
            s = next(s for s in slips() if s["slip_id"] == p["slip_id"])
            paid[s["emp_no"]] = paid.get(s["emp_no"], 0) + float(p["amount"])
        return 200, {"paid": len(pays), "payment_ids": [f"SPM-2026-{i:06d}" for i in range(1, len(pays) + 1)]}

    def adv_post(m, q, b, role, st):
        b = b or {}
        if b.get("kind", "advance") != "repayment" and float(b.get("amount") or 0) > 120000:
            return 400, {"detail": "Refused under the Punjab Labour Code 2026 (s.18): an advance can be at most 3 × the minimum wage "
                                   "(Rs 120,000). Switch the payroll profile in Settings if the legacy rules apply to this business."}
        if b.get("method") == "cash":
            return 400, {"detail": "Refused under the Punjab Labour Code 2026 (s.18): an advance must be paid by bank, JazzCash, Easypaisa or cheque."}
        return 201, {"advance_id": "ADV-2026-000002"}

    def attach(m, q, b, role, st):
        n = st["att"] = st.get("att", 0) + 1
        return 201, {"id": f"ATT-FIX{n:05d}", "kind": "image/jpeg", "size": 184322, "view_url": f"/api/attachments/ATT-FIX{n:05d}/file"}

    def chat(m, q, b, role, st):
        st.setdefault("chat_bodies", []).append(b)
        ids = (b or {}).get("attachment_ids") or []
        return 200, {"text": f"Got it -- {len(ids)} proof(s) attached. (fixture reply)" if ids else "OK (fixture reply)", "specialist": "manager",
                     "pending": None, "tables": []}

    def close_h(m, q, b, role, st):
        st["closed_through"] = (b or {}).get("through_date")
        return 200, {"close_id": 2, "through_date": st["closed_through"]}

    ok = _j({"ok": True})
    return [
        ("GET", r"/api/employees/?$", emp_list),
        ("POST", r"/api/employees/?$", emp_add),
        ("GET", r"/api/employees/(EMP-[A-Z0-9]+)$", lambda m, q, b, r, s: (200, {"employee": next((employee(e, r == "owner") for e in _EMP if e[0] == m.group(1)), None),
                                                                                  "events": [{"kind": "joined", "at": "2025-03-01T05:00:00Z", "by_user": "Haji Rasheed", "detail": {}}]})),
        ("PATCH", r"/api/employees/(EMP-[A-Z0-9]+)$", ok),
        ("POST", r"/api/employees/(EMP-[A-Z0-9]+)/pay-structure$", ok),
        ("POST", r"/api/employees/(EMP-[A-Z0-9]+)/login$", emp_login),
        ("POST", r"/api/employees/(EMP-[A-Z0-9]+)/login/reset-pin$", emp_login),
        ("POST", r"/api/employees/(EMP-[A-Z0-9]+)/(end|rehire)$", ok),
        ("GET", r"/api/payroll/(\d{4}-\d{2})/attendance$", lambda m, q, b, r, s: (200, attendance(bool(s.get("run_id"))))),
        ("PUT", r"/api/payroll/(\d{4}-\d{2})/attendance$", lambda m, q, b, r, s: (200, {"saved": len((b or {}).get("rows", []))})),
        ("POST", r"/api/payroll/(\d{4}-\d{2})/adjustments$", _j({"adj_id": "ADJ-NEW00001"}, 201)),
        ("DELETE", r"/api/payroll/adjustments/(ADJ-[A-Z0-9]+)$", ok),
        ("GET", r"/api/payroll/(\d{4}-\d{2})/preview$", preview_h),
        ("POST", r"/api/payroll/(\d{4}-\d{2})/approve$", approve_h),
        ("GET", r"/api/payroll/runs/(PAY-[0-9-]+)$", run_h),
        ("POST", r"/api/payroll/runs/(PAY-[0-9-]+)/pay$", pay_h),
        ("POST", r"/api/payroll/runs/(PAY-[0-9-]+)/reverse$", ok),
        ("GET", r"/api/payslips/(PSL-[0-9-]+)$", lambda m, q, b, r, s: (200, payslip(m.group(1)))),
        ("GET", r"/api/me/payslips$", _j(my_payslips())),
        ("GET", r"/api/staff-advances$", _j(advances())),
        ("POST", r"/api/staff-advances$", adv_post),
        ("POST", r"/api/staff-advances/(ADV-[0-9-]+)/reverse$", ok),
        ("GET", r"/api/payroll/(\d{4}-\d{2})/statutory$", lambda m, q, b, r, s: (200, statutory(q.get("kind", "eobi")))),
        ("POST", r"/api/statutory-payments$", _j({"stat_id": "STY-2026-000001"}, 201)),
        ("GET", r"/api/statutory-rates$", _j(rates())),
        ("POST", r"/api/statutory-rates$", _j({"rate_id": 99}, 201)),
        ("GET", r"/api/payroll/settings$", _j(payroll_settings())),
        ("PUT", r"/api/payroll/settings$", ok),
        ("GET", r"/api/accounts/?$", _j(money_accounts())),
        ("POST", r"/api/accounts/?$", _j({"account_id": "ACC-MEEZAN"}, 201)),
        ("GET", r"/api/accounts/([A-Z0-9-]+)/book$", lambda m, q, b, r, s: (200, account_book(m.group(1), redact=r != "owner"))),
        ("POST", r"/api/accounts/transfer$", _j({"transfer_id": "XFR-2026-000002"}, 201)),
        ("POST", r"/api/accounts/([A-Z0-9-]+)/cash-count$", lambda m, q, b, r, s: (201, {"count_id": "CC-7Q2M4K1P", "book": 46117.17,
                                                                                         "counted": float((b or {}).get("counted") or 0),
                                                                                         "difference": round(float((b or {}).get("counted") or 0) - 46117.17, 2)})),
        ("POST", r"/api/cash-counts/(CC-[A-Z0-9]+)/post$", _j({"je_id": "JV-2026-000009"})),
        ("GET", r"/api/accounts/([A-Z0-9-]+)/reconciliation$", lambda m, q, b, r, s: (200, reconciliation(set(s.get("cleared", []))))),
        ("POST", r"/api/accounts/([A-Z0-9-]+)/clear$", lambda m, q, b, r, s: (s.setdefault("cleared", []).extend(
            i["source_id"] for i in (b or {}).get("items", []) if (b or {}).get("cleared", True)) or (200, {"ok": True}))),
        ("POST", r"/api/accounts/([A-Z0-9-]+)/reconciliations$", _j({"recon_id": 3, "difference": 0.0}, 201)),
        ("GET", r"/api/finance/pnl$", lambda m, q, b, r, s: (200, pnl(clerk=r != "owner"))),
        ("GET", r"/api/finance/balance-sheet$", _j(balance_sheet())),
        ("GET", r"/api/finance/cash-flow$", _j(cash_flow())),
        ("GET", r"/api/finance/trial-balance$", _j(trial_balance())),
        ("GET", r"/api/finance/kpis$", _j(kpis())),
        ("GET", r"/api/finance/margins$", lambda m, q, b, r, s: (200, margins(q.get("by", "product")))),
        ("GET", r"/api/finance/assets$", _j(assets())),
        ("GET", r"/api/finance/loans$", _j(loans())),
        ("POST", r"/api/finance/(capital|drawings|loans|assets|journal|opening-balances|depreciation/run)$", _j({"je_id": "JV-2026-000010"}, 201)),
        ("POST", r"/api/finance/(loans|assets|journal)/([A-Z0-9-]+)/(repay|dispose|reverse)$", _j({"je_id": "JV-2026-000011"}, 201)),
        ("GET", r"/api/finance/periods$", lambda m, q, b, r, s: (200, periods(s.get("closed_through")))),
        ("POST", r"/api/finance/periods/close$", close_h),
        ("POST", r"/api/finance/periods/(\d+)/reopen$", ok),
        ("POST", r"/api/attachments$", attach),
        ("POST", r"/api/chat$", chat),
    ]


def match(method: str, path: str) -> tuple[Any, re.Match] | None:
    for m, rx, h in routes():
        if m == method:
            hit = re.match(rx, path)
            if hit:
                return h, hit
    return None
