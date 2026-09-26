"""The payroll engine: pure functions over integer paisa (plan §5). No database, no framework, no clock.

The repository (domain/repository/payroll.py) gathers one employee's month -- pay terms, attendance, adjustments,
commission bases, open advances, year-to-date tax figures -- into an `EmployeeMonth`, the statutory settings into
`Rules`, and calls `compute_employee`. The result is the payslip's lines, totals, warnings (the owner may proceed)
and errors (the run cannot be approved). Nothing here raises for a business rule: refusals come back as `errors`,
so a preview can show every problem at once; `PayrollRefusal` is raised only by the single-value checks the
repository calls at write time (advances, instalments).

Every statutory figure is a DATED SETTING read from `statutory_rates` (seeded from the plan, graded, "last verified"
dated); a figure the plan could not verify (the PESSI wage ceiling, the disputed EOBI base) is passed in as None and
the engine refuses to compute that line. Munshi calculates; it does not give tax or legal advice (accounts.BOUNDARY_TEXT).
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field

from munshi.domain.models import mul_div

PLC_SWITCH = "Punjab Labour Code 2026 limit; the owner can switch payroll to the old law in Settings"
EARNING_CODES = ("BASIC", "OT", "ALW", "COMM", "BONUS", "ARREARS", "OTHER_EARN")
DEDUCTION_CODES = ("ABSENCE", "EOBI_EE", "SS_EE", "TAX", "ADV", "FINE", "LOSS", "OTHER_DED", "IN_LIEU")
EMPLOYER_CODES = ("EOBI_ER", "SS_ER")
# payslip order (PLC s.165(15) / plan §7.1.1): basic and allowances, then the deductions, then "other payments"
SLIP_ORDER = ("BASIC", "ALW", "ARREARS", "OTHER_EARN", "ABSENCE", "EOBI_EE", "SS_EE", "TAX", "ADV", "FINE", "LOSS",
              "OTHER_DED", "IN_LIEU", "OT", "COMM", "BONUS")
OTHER_PAYMENTS = frozenset({"OT", "COMM", "BONUS"})
ADJUSTMENT_CODE = {"bonus": "BONUS", "arrears": "ARREARS", "other_earning": "OTHER_EARN", "fine": "FINE",
                   "loss_recovery": "LOSS", "other_deduction": "OTHER_DED"}
# leave entitlements per calendar year, in days (legacy: Shops & Establishments Ordinance ss.14-16; PLC ss.192-195)
LEAVE_ENTITLEMENT = {"legacy_1969": {"annual": 14, "casual": 10, "sick": 8}, "plc_2026": {"annual": 18, "casual": 10, "sick": 8}}


class PayrollRefusal(Exception):
    """A write the rules refuse (the repository turns it into a StateError)."""


# ============================================================================ calendar helpers
def period_bounds(period: str) -> tuple[str, str, int]:
    """'2026-09' -> ('2026-09-01', '2026-09-30', 30). Refuses anything that is not YYYY-MM."""
    if len(period) != 7 or period[4] != "-" or not (period[:4].isdigit() and period[5:].isdigit()):
        raise ValueError(f"period must be YYYY-MM, got {period!r}")
    y, m = int(period[:4]), int(period[5:])
    if not 1 <= m <= 12 or y < 2000:
        raise ValueError(f"period must be YYYY-MM, got {period!r}")
    n = calendar.monthrange(y, m)[1]
    return f"{period}-01", f"{period}-{n:02d}", n


def tax_year_of(period: str) -> int:
    """Pakistan's tax year runs July-June and is named by the year it ends: Sep 2026 is in TY2027."""
    y, m = int(period[:4]), int(period[5:7])
    return y + 1 if m >= 7 else y


def months_remaining(period: str) -> int:
    """Months left in the tax year INCLUDING this one: July 12 ... September 10 ... June 1."""
    m = int(period[5:7])
    return 19 - m if m >= 7 else 7 - m


def days_employed(period: str, joined_on: str, left_on: str | None) -> int:
    start, end, n = period_bounds(period)
    first = max(start, joined_on)
    last = min(end, left_on) if left_on else end
    if last < first:
        return 0
    return int(last[8:10]) - int(first[8:10]) + 1


def age_on(date_of_birth: str | None, on: str) -> int | None:
    if not date_of_birth or len(date_of_birth) != 10:
        return None
    y, m, d = (int(x) for x in date_of_birth.split("-"))
    oy, om, od = (int(x) for x in on.split("-"))
    return oy - y - ((om, od) < (m, d))


# ============================================================================ single figures
def prorate(amount: int, days: int, days_in_month: int) -> int:
    return mul_div(amount, days, days_in_month)


def absence_deduction(basic: int, unpaid_absent_x2: int, working_days_basis: int) -> int:
    """Unpaid absence on the notification's 26-day basis: 2 days of 40,000 = 3,076.92."""
    return mul_div(basic, unpaid_absent_x2, 2 * working_days_basis)


def overtime(base_amount: int, multiplier_x100: int, minutes: int, base_minutes: int) -> int:
    """OT pay: base_amount is the monthly basic (base_minutes = basis x 8 h x 60) or the daily rate (8 h x 60)."""
    if minutes <= 0:
        return 0
    return mul_div(base_amount * multiplier_x100, minutes, base_minutes * 100)


def eobi_shares(base: int, ee_bp: int, er_bp: int) -> tuple[int, int]:
    """(employee, employer) EOBI on the notified wage base, not on actual pay."""
    return mul_div(base, ee_bp, 10000), mul_div(base, er_bp, 10000)


def social_security(wages: int, rate_bp: int, ceiling: int | None, mode: str = "exclude") -> int:
    """Employer's provincial social security. A worker paid above the ceiling is not 'secured' (0) unless mode 'cap'.
    An unknown ceiling (None) is refused: the plan could not verify it, and guessing would misstate a liability."""
    if ceiling is None:
        raise PayrollRefusal("the social security wage ceiling is not set (the plan could not verify it): set "
                             "ss_wage_ceiling in statutory rates before computing it")
    if wages > ceiling:
        return mul_div(ceiling, rate_bp, 10000) if mode == "cap" else 0
    return mul_div(wages, rate_bp, 10000)


def validate_slabs(slabs: dict) -> None:
    """The tax table must be internally cumulative: each band's base = previous base + rate x width."""
    bands = slabs.get("bands") or []
    if not bands or bands[0]["over"] != 0:
        raise ValueError("tax slabs must start at 0")
    for prev, cur in zip(bands, bands[1:], strict=False):
        if prev["upto"] != cur["over"]:
            raise ValueError(f"tax slab gap at {cur['over']}")
        expect = prev["base"] + mul_div(prev["upto"] - prev["over"], prev["rate_bp"], 10000)
        if cur["base"] != expect:
            raise ValueError(f"tax slab over {cur['over']}: base {cur['base']} should be {expect}")
    if bands[-1]["upto"] is not None:
        raise ValueError("the last tax slab must be open-ended")


def slab_tax(annual: int, slabs: dict) -> int:
    if annual <= 0:
        return 0
    for b in slabs["bands"]:
        if b["upto"] is None or annual <= b["upto"]:
            return b["base"] + mul_div(annual - b["over"], b["rate_bp"], 10000)
    raise ValueError("tax slabs do not cover the amount")        # unreachable after validate_slabs


def monthly_withholding(ytd_taxable: int, ytd_tax: int, this_taxable: int, regular: int, period: str, slabs: dict,
                        round_rupee: bool = True) -> dict:
    """Salary withholding by annual projection (plan §5.2.8): project the year, apply the slab, spread what is still
    due over the months left including this one. Commission, OT and bonus count only when paid (not projected)."""
    m = months_remaining(period)
    projected = ytd_taxable + this_taxable + regular * (m - 1)
    annual = slab_tax(projected, slabs)
    tax = max(0, mul_div(annual - ytd_tax, 1, m))
    if round_rupee:
        tax = mul_div(tax, 1, 100) * 100
    return {"months_remaining": m, "projected": projected, "annual_tax": annual, "tax": tax}


def fine_cap(earnings: int, cap_bp: int) -> int:
    return mul_div(earnings, cap_bp, 10000)


def check_advance(amount: int, min_wage_monthly: int, limits: dict, open_outstanding: int) -> None:
    """PLC s.18: an advance at most 3 x the monthly minimum wage, and no new one while the last is unpaid."""
    cap_x = limits.get("advance_cap_min_wages")
    if cap_x and amount > cap_x * min_wage_monthly:
        raise PayrollRefusal(f"an advance may be at most {cap_x} x the monthly minimum wage (Rs {cap_x * min_wage_monthly / 100:,.0f}): {PLC_SWITCH}")
    if limits.get("one_open_advance") and open_outstanding > 0:
        raise PayrollRefusal(f"this employee still owes Rs {open_outstanding / 100:,.2f} on an earlier advance; "
                             f"a new advance must wait until it is repaid: {PLC_SWITCH}")


def check_instalment(instalment: int, monthly_pay: int, limits: dict) -> None:
    cap_bp = limits.get("advance_instalment_cap_bp")
    if cap_bp is None:
        return
    if instalment <= 0:
        raise PayrollRefusal(f"set a monthly instalment (at most {cap_bp / 100:g}% of pay): {PLC_SWITCH}")
    if instalment > mul_div(monthly_pay, cap_bp, 10000):
        raise PayrollRefusal(f"an instalment may be at most {cap_bp / 100:g}% of pay (Rs {mul_div(monthly_pay, cap_bp, 10000) / 100:,.2f}): {PLC_SWITCH}")


def check_payment_method(method: str, limits: dict, cash_allowed: bool, what: str = "salary") -> bool:
    """PLC s.165(5)/s.18: wages and advances through a bank or wallet. Returns True when the payment relies on the
    owner's per-employee cash exemption (the payslip and audit trail say so)."""
    if method != "cash" or not limits.get("cashless_only"):
        return False
    if cash_allowed:
        return True
    raise PayrollRefusal(f"{what} must be paid by bank, JazzCash, Easypaisa or cheque (or the owner marks this employee "
                         f"'cash allowed'): {PLC_SWITCH}")


# ============================================================================ one employee's month
@dataclass(frozen=True)
class Rules:
    profile: str
    limits: dict
    working_days_basis: int = 26
    eobi_registered: bool = False
    eobi_base: int | None = None             # None: seeded 'verify' -> EOBI refused
    eobi_ee_bp: int = 100
    eobi_er_bp: int = 500
    ss_registered: bool = False
    ss_rate_bp: int = 0
    ss_ceiling: int | None = None            # None: unknown -> refused
    ss_mode: str = "exclude"
    ss_worker_share: int = 0
    slabs: dict | None = None
    round_rupee: bool = True
    min_wage_monthly: int = 0
    min_wage_daily: int = 0


@dataclass
class EmployeeMonth:
    employee_id: str
    name: str
    period: str
    pay_basis: str                   # monthly | daily
    basic: int = 0                   # monthly basic (full month)
    daily_rate: int = 0
    components: list = field(default_factory=list)
    ot_eligible: bool = True
    days_employed: int = 0
    attendance: dict | None = None   # *_x2, ot minutes, trips; None = none recorded
    adjustments: list = field(default_factory=list)    # {adj_id, code, amount, taxable, note, ref}
    commissions: list = field(default_factory=list)    # {rule_id, label, basis, amount}
    advances: list = field(default_factory=list)       # {advance_id, installment, outstanding} oldest first
    eobi_covered: bool = False
    ss_covered: bool = False
    tax_mode: str = "auto"
    ytd_taxable: int = 0
    ytd_tax: int = 0
    age: int | None = None
    role_hint: str = "other"


def _line(code: str, side: str, label: str, amount: int, taxable: bool = False, qty_x100: int | None = None,
          rate: int | None = None, ref: str | None = None) -> dict:
    return {"code": code, "side": side, "label": label, "amount": int(amount), "taxable": int(bool(taxable)),
            "qty_x100": qty_x100, "rate_paisa": rate, "ref": ref}


def compute_employee(e: EmployeeMonth, r: Rules) -> dict:
    """Plan §5.2 for one employee. Returns {lines, gross, deductions, net, employer, taxable, tax, ytd_*, warnings,
    errors, withholding}. All integer paisa."""
    _, period_end, dim = period_bounds(e.period)
    lines: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []
    att = e.attendance or {}
    days_x2 = int(att.get("days_worked_x2", 0))
    absent_x2 = int(att.get("unpaid_absent_x2", 0))
    basis = r.working_days_basis
    plc = r.profile == "plc_2026"

    # 1. basic
    if e.pay_basis == "monthly":
        basic = prorate(e.basic, e.days_employed, dim) if e.days_employed < dim else e.basic
        lines.append(_line("BASIC", "earning", "Basic salary" + (f" ({e.days_employed}/{dim} days)" if e.days_employed < dim else ""),
                           basic, True, e.days_employed * 100, e.basic))
        if absent_x2:
            lines.append(_line("ABSENCE", "deduction", f"Unpaid absence ({absent_x2 / 2:g} days on a {basis}-day basis)",
                               absence_deduction(e.basic, absent_x2, basis), False, absent_x2 * 50, mul_div(e.basic, 1, basis)))
        if e.attendance is None:
            warnings.append(f"{e.name}: no attendance recorded for {e.period}; a full month is assumed")
        if r.min_wage_monthly and e.basic < r.min_wage_monthly:
            warnings.append(f"{e.name}: monthly basic Rs {e.basic / 100:,.2f} is below the minimum wage Rs {r.min_wage_monthly / 100:,.2f}")
    else:
        basic = mul_div(e.daily_rate, days_x2, 2)
        lines.append(_line("BASIC", "earning", f"Daily wages ({days_x2 / 2:g} days)", basic, True, days_x2 * 50, e.daily_rate))
        if e.attendance is None:
            warnings.append(f"{e.name}: daily-wage worker with no attendance for {e.period}; nothing is due")
        if r.min_wage_daily and e.daily_rate < r.min_wage_daily:
            warnings.append(f"{e.name}: daily rate Rs {e.daily_rate / 100:,.2f} is below the minimum daily wage Rs {r.min_wage_daily / 100:,.2f}")

    # 2. overtime
    ot_min = int(att.get("ot_minutes", 0)) + int(att.get("restday_ot_minutes", 0))
    hol_min = int(att.get("holiday_ot_minutes", 0))
    if ot_min or hol_min:
        if not e.ot_eligible:
            warnings.append(f"{e.name}: overtime recorded but the pay terms say not OT-eligible; ignored")
        else:
            base_amt, base_min = (e.basic, basis * 8 * 60) if e.pay_basis == "monthly" else (e.daily_rate, 8 * 60)
            mult, hmult = r.limits.get("ot_multiplier_x100", 200), r.limits.get("holiday_ot_multiplier_x100", 200)
            ot = overtime(base_amt, mult, ot_min, base_min) + overtime(base_amt, hmult, hol_min, base_min)
            lines.append(_line("OT", "earning", f"Overtime ({(ot_min + hol_min) / 60:g} h)", ot, True, mul_div(ot_min + hol_min, 100, 60)))
            if plc and ot_min + hol_min > mul_div(8 * 60 * dim, 1, 7):
                warnings.append(f"{e.name}: overtime above 8 hours a week (PLC s.180)")

    # 3. components
    trips = int(att.get("trips", 0))
    for comp in e.components:
        calc, amt = comp.get("calc"), int(comp.get("amount_paisa", 0))
        if calc == "fixed":
            v = prorate(amt, e.days_employed, dim) if comp.get("prorate") and e.days_employed < dim else amt
            qty = None
        elif calc == "per_day":
            v, qty = mul_div(amt, days_x2, 2), days_x2 * 50
        elif calc == "per_trip":
            v, qty = amt * trips, trips * 100
        elif calc == "pct_basic":
            v, qty = mul_div(basic, int(comp.get("rate_bp", 0)), 10000), None
        else:
            errors.append(f"{e.name}: unknown pay component calculation {calc!r}")
            continue
        if v == 0:
            continue
        if comp.get("side", "earning") == "deduction":
            lines.append(_line("OTHER_DED", "deduction", comp.get("label") or comp.get("code") or "Deduction", v, False, qty, amt or None))
        else:
            lines.append(_line("ALW", "earning", comp.get("label") or comp.get("code") or "Allowance", v, bool(comp.get("taxable", 1)), qty, amt or None))

    # 4. commission (bases computed by the repository from the salesman's own delivered sales)
    for cm in e.commissions:
        if cm["amount"]:
            lines.append(_line("COMM", "earning", cm["label"], cm["amount"], True, None, None, cm["rule_id"]))

    # 5. adjustments
    for a in e.adjustments:
        code = ADJUSTMENT_CODE[a["code"]]
        side = "earning" if code in EARNING_CODES else "deduction"
        label = {"BONUS": "Bonus", "ARREARS": "Arrears", "OTHER_EARN": "Other earning", "FINE": "Fine",
                 "LOSS": "Loss / shortage recovery", "OTHER_DED": "Other deduction"}[code]
        lines.append(_line(code, side, f"{label}: {a['note']}"[:80], a["amount"], bool(a.get("taxable")) and side == "earning", None, None, a["adj_id"]))

    gross = sum(ln["amount"] for ln in lines if ln["side"] == "earning")
    absence = sum(ln["amount"] for ln in lines if ln["code"] == "ABSENCE")
    wages = gross - absence

    # fines: cap and conditions (refusals)
    fines = sum(ln["amount"] for ln in lines if ln["code"] == "FINE")
    if fines:
        cap_bp = int(r.limits.get("fine_cap_bp", 300))
        if fines > fine_cap(gross, cap_bp):
            errors.append(f"{e.name}: fines Rs {fines / 100:,.2f} exceed {cap_bp / 100:g}% of this month's pay "
                          f"(Rs {fine_cap(gross, cap_bp) / 100:,.2f})" + (f": {PLC_SWITCH}" if plc else " (Payment of Wages Act s.8)"))
        if e.age is not None and e.age < 18:
            errors.append(f"{e.name}: a worker under 18 may not be fined")

    # 6. EOBI
    if e.eobi_covered and r.eobi_registered:
        if r.eobi_base is None:
            errors.append(f"{e.name}: the EOBI wage base is marked VERIFY (the plan found it disputed); the owner must set "
                          "eobi_wage_base in statutory rates before EOBI is computed")
        else:
            ee, er = eobi_shares(r.eobi_base, r.eobi_ee_bp, r.eobi_er_bp)
            lines.append(_line("EOBI_EE", "deduction", f"EOBI (employee {r.eobi_ee_bp / 100:g}%)", ee))
            lines.append(_line("EOBI_ER", "employer", f"EOBI (employer {r.eobi_er_bp / 100:g}%)", er))

    # 7. social security
    if e.ss_covered and r.ss_registered:
        try:
            ss = social_security(wages, r.ss_rate_bp, r.ss_ceiling, r.ss_mode)
            if ss:
                lines.append(_line("SS_ER", "employer", f"Social security (employer {r.ss_rate_bp / 100:g}%)", ss))
                if r.ss_worker_share:
                    lines.append(_line("SS_EE", "deduction", "Social security (worker share)", r.ss_worker_share))
        except PayrollRefusal as ex:
            errors.append(f"{e.name}: {ex}")

    # 8. income tax by annual projection
    taxable = max(0, sum(ln["amount"] for ln in lines if ln["side"] == "earning" and ln["taxable"]) - absence)
    withholding = None
    tax = 0
    if e.tax_mode == "auto":
        if not r.slabs:
            errors.append(f"{e.name}: no salary tax slabs are set for tax year {tax_year_of(e.period)}")
        elif int(r.slabs.get("tax_year", 0)) != tax_year_of(e.period):
            errors.append(f"{e.name}: the salary tax slabs are for TY{r.slabs.get('tax_year')}, not TY{tax_year_of(e.period)}; "
                          "add the new year's slabs in statutory rates")
        else:
            if e.pay_basis == "monthly":
                regular = e.basic + sum(int(c.get("amount_paisa", 0)) for c in e.components
                                        if c.get("calc") == "fixed" and c.get("side", "earning") == "earning" and c.get("taxable", 1))
            else:
                regular = basic
            withholding = monthly_withholding(e.ytd_taxable, e.ytd_tax, taxable, regular, e.period, r.slabs, r.round_rupee)
            tax = withholding["tax"]
            if tax:
                lines.append(_line("TAX", "deduction", "Income tax withheld (s.149)", tax))

    # 9. advance recovery (after every other deduction, so net pay never goes below zero)
    other_ded = sum(ln["amount"] for ln in lines if ln["side"] == "deduction")
    available = gross - other_ded
    if available < 0:
        errors.append(f"{e.name}: deductions exceed pay by Rs {-available / 100:,.2f}; net pay cannot be below zero")
        available = 0
    inst_cap_bp = r.limits.get("advance_instalment_cap_bp")
    adv_cap = mul_div(gross, inst_cap_bp, 10000) if inst_cap_bp else None
    taken = 0
    for adv in e.advances:
        want = min(adv["installment"] or adv["outstanding"], adv["outstanding"])
        room = available - taken
        if adv_cap is not None:
            room = min(room, adv_cap - taken)
        amt = max(0, min(want, room))
        if amt < want:
            warnings.append(f"{e.name}: advance recovery {adv['advance_id']} cut to Rs {amt / 100:,.2f} "
                            + ("(at most 20% of this month's pay under the Punjab Labour Code)" if adv_cap is not None and amt == adv_cap - taken else "(net pay cannot go below zero)"))
        if amt:
            lines.append(_line("ADV", "deduction", f"Advance recovery {adv['advance_id']}", amt, False, None, None, adv["advance_id"]))
            taken += amt

    deductions = sum(ln["amount"] for ln in lines if ln["side"] == "deduction")
    employer = sum(ln["amount"] for ln in lines if ln["side"] == "employer")
    net = gross - deductions
    if not plc and r.limits.get("deduction_cap_bp_warn") and gross and deductions - absence > mul_div(gross, r.limits["deduction_cap_bp_warn"], 10000):
        warnings.append(f"{e.name}: deductions are above 50% of pay")
    if plc and e.role_hint == "driver" and e.age is not None and e.age < 21:
        warnings.append(f"{e.name}: a driver must be at least 21 (PLC s.249)")
    order = {c: i for i, c in enumerate(SLIP_ORDER)}
    lines.sort(key=lambda ln: (ln["side"] == "employer", order.get(ln["code"], 99)))
    return {"employee_id": e.employee_id, "lines": lines, "gross": gross, "deductions": deductions, "net": net,
            "employer": employer, "taxable": taxable, "tax": tax, "ytd_taxable": e.ytd_taxable + taxable,
            "ytd_tax": e.ytd_tax + tax, "withholding": withholding, "warnings": warnings, "errors": errors,
            "period_end": period_end}
