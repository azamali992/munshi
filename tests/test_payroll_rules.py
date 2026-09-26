"""The pure payroll engine (domain/payroll_rules.py), plan §10.1. Every expected figure is worked by hand in the comment
beside it, never read back from the code under test. All amounts are integer paisa."""
from __future__ import annotations

import json

import pytest

from munshi.domain import payroll_rules as R
from munshi.domain.accounts import LEGACY_LIMITS, PLC_LIMITS
from munshi.domain.migrations_payroll import SEEDS

SLABS = json.loads(next(v for k, _, v, *_ in SEEDS if k == "salary_tax_slabs"))
RS = 100      # paisa per rupee


# ------------------------------------------------------------------ TY2027 salary slabs (Finance Act 2026)
@pytest.mark.parametrize("income, tax", [
    (600_000, 0),                 # band 1: 0%
    (1_200_000, 6_000),           # 1% of (1.2M - 0.6M) = 6,000
    (1_440_000, 32_400),          # 6,000 + 11% of 240,000 = 6,000 + 26,400
    (2_200_000, 116_000),         # 6,000 + 11% of 1,000,000 = 116,000
    (3_200_000, 316_000),         # 116,000 + 20% of 1,000,000
    (4_100_000, 541_000),         # 316,000 + 25% of 900,000 = 316,000 + 225,000
    (5_600_000, 976_000),         # 541,000 + 29% of 1,500,000 = 541,000 + 435,000
    (7_000_000, 1_424_000),       # 976,000 + 32% of 1,400,000 = 976,000 + 448,000
    (8_000_000, 1_774_000),       # 1,424,000 + 35% of 1,000,000
])
def test_salary_slabs_ty2027(income, tax):
    assert R.slab_tax(income * RS, SLABS) == tax * RS


def test_seeded_slabs_are_internally_cumulative_and_a_mistyped_band_is_refused():
    R.validate_slabs(SLABS)                          # each base = previous base + rate x width
    bad = json.loads(json.dumps(SLABS))
    bad["bands"][3]["base"] = 11_700_000             # 117,000 instead of 116,000
    with pytest.raises(ValueError, match="should be 11600000"):
        R.validate_slabs(bad)
    assert SLABS["tax_year"] == 2027


def test_tax_year_and_months_remaining():
    assert (R.tax_year_of("2026-09"), R.months_remaining("2026-09")) == (2027, 10)   # Sep..Jun = 10 months
    assert (R.tax_year_of("2026-07"), R.months_remaining("2026-07")) == (2027, 12)
    assert (R.tax_year_of("2027-06"), R.months_remaining("2027-06")) == (2027, 1)
    assert (R.tax_year_of("2027-01"), R.months_remaining("2027-01")) == (2027, 6)


def test_withholding_by_annual_projection():
    # Bilal: YTD 240,000 taxable / 5,400 tax; 120,000 this month; projected 240,000 + 120,000 + 9 x 120,000 = 1,440,000
    # annual tax 32,400; monthly (32,400 - 5,400) / 10 = 2,700
    w = R.monthly_withholding(240_000 * RS, 5_400 * RS, 120_000 * RS, 120_000 * RS, "2026-09", SLABS)
    assert (w["projected"], w["annual_tax"], w["tax"], w["months_remaining"]) == (1_440_000 * RS, 32_400 * RS, 2_700 * RS, 10)
    # Imran: 80,000 + 43,190.80 + 9 x 40,000 = 483,190.80 < 600,000 -> 0 (commission is not projected forward)
    assert R.monthly_withholding(80_000 * RS, 0, 4_319_080, 40_000 * RS, "2026-09", SLABS)["tax"] == 0
    # rounding to the rupee (December: 7 months left)
    w = R.monthly_withholding(0, 0, 100_000 * RS, 100_000 * RS, "2026-12", SLABS)      # 7 months left: 700,000 projected
    assert w["annual_tax"] == 1_000 * RS and w["tax"] == 143 * RS                     # 1% of 100,000 = 1,000; /7 = 142.86 -> 143
    assert R.monthly_withholding(0, 0, 100_000 * RS, 100_000 * RS, "2026-12", SLABS, round_rupee=False)["tax"] == 14_286


def test_absence_overtime_proration():
    assert R.absence_deduction(4_000_000, 4, 26) == 307_692            # 40,000 x 2 / 26 = 3,076.923 -> 3,076.92
    # monthly 40,000, 10 h at 2x: 4,000,000 x 200 x 600 / (26 x 480 x 100) = 384,615.38 -> 3,846.15
    assert R.overtime(4_000_000, 200, 600, 26 * 8 * 60) == 384_615
    assert R.overtime(160_000, 300, 120, 8 * 60) == 120_000            # daily 1,600: 2 h holiday at 3x = 1,600/8 x 2 x 3 = 1,200
    assert R.prorate(4_000_000, 15, 30) == 2_000_000                   # joined 16 Sep: 15 of 30 days = 20,000
    assert R.days_employed("2026-09", "2026-09-16", None) == 15
    assert R.days_employed("2026-09", "2025-01-01", "2026-09-10") == 10
    assert R.days_employed("2026-09", "2026-10-01", None) == 0


def test_eobi_and_social_security():
    assert R.eobi_shares(3_700_000, 100, 500) == (37_000, 185_000)     # base 37,000: 370 / 1,850
    assert R.eobi_shares(4_000_000, 100, 500) == (40_000, 200_000)     # base 40,000: 400 / 2,000
    assert R.social_security(4_150_000, 600, 3_700_000, "cap") == 222_000     # 6% of 37,000 = 2,220
    assert R.social_security(4_150_000, 600, 3_700_000, "exclude") == 0       # above the ceiling: not secured
    assert R.social_security(3_000_000, 600, 3_700_000) == 180_000            # 6% of 30,000
    with pytest.raises(R.PayrollRefusal, match="ceiling is not set"):
        R.social_security(3_000_000, 600, None)                                # PESSI ceiling unknown -> refused


def test_fine_caps_by_profile():
    assert R.fine_cap(4_000_000, LEGACY_LIMITS["fine_cap_bp"]) == 124_800      # 3.12% of 40,000 = 1,248
    assert R.fine_cap(4_000_000, PLC_LIMITS["fine_cap_bp"]) == 120_000         # 3% = 1,200
    e = R.EmployeeMonth("E1", "Nadeem", "2026-09", "monthly", basic=4_000_000, days_employed=30,
                        attendance={"days_worked_x2": 52},
                        adjustments=[{"adj_id": "ADJ-1", "code": "fine", "amount": 150_000, "taxable": 0, "note": "late x3 after show-cause"}])
    res = R.compute_employee(e, _rules())
    assert any("fines Rs 1,500.00 exceed 3%" in x and "Punjab Labour Code 2026" in x for x in res["errors"])
    e.adjustments[0]["amount"] = 120_000
    assert not R.compute_employee(e, _rules())["errors"]
    e.age = 17
    assert any("under 18" in x for x in R.compute_employee(e, _rules())["errors"])


def test_plc_advance_and_instalment_refusals():
    with pytest.raises(R.PayrollRefusal, match="3 x the monthly minimum wage"):
        R.check_advance(13_000_000, 4_000_000, PLC_LIMITS, 0)                  # 130,000 > 3 x 40,000 = 120,000
    R.check_advance(12_000_000, 4_000_000, PLC_LIMITS, 0)                      # exactly the cap is fine
    with pytest.raises(R.PayrollRefusal, match="earlier advance"):
        R.check_advance(1_000_000, 4_000_000, PLC_LIMITS, 500_000)             # one open advance at a time
    R.check_advance(13_000_000, 4_000_000, LEGACY_LIMITS, 500_000)             # legacy: no cap, no one-at-a-time
    with pytest.raises(R.PayrollRefusal, match="20% of pay"):
        R.check_instalment(1_000_000, 4_319_080, PLC_LIMITS)                   # 10,000 on 43,190.80 = 23% > 20%
    R.check_instalment(500_000, 4_319_080, PLC_LIMITS)                         # 5,000 = 11.6%
    R.check_instalment(1_000_000, 4_319_080, LEGACY_LIMITS)                    # legacy: no instalment cap


def test_cash_is_refused_under_plc_unless_the_owner_exempted_the_employee():
    with pytest.raises(R.PayrollRefusal, match="Settings"):
        R.check_payment_method("cash", PLC_LIMITS, cash_allowed=False)
    assert R.check_payment_method("cash", PLC_LIMITS, cash_allowed=True) is True     # relies on the exemption
    assert R.check_payment_method("bank", PLC_LIMITS, cash_allowed=False) is False
    assert R.check_payment_method("cash", LEGACY_LIMITS, cash_allowed=False) is False


def _rules(**kw) -> R.Rules:
    base = dict(profile="plc_2026", limits=dict(PLC_LIMITS), working_days_basis=26, eobi_registered=True, eobi_base=3_700_000,
                slabs=SLABS, min_wage_monthly=4_000_000, min_wage_daily=153_846)
    base.update(kw)
    return R.Rules(**base)


def test_one_employee_month_end_to_end():
    # Rafiq, driver: 40,000 + Rs 500/trip x 3 trips; EOBI covered; YTD 80,000 / 0
    e = R.EmployeeMonth("E3", "Rafiq", "2026-09", "monthly", basic=4_000_000, days_employed=30,
                        components=[{"code": "TRIP", "label": "Trip allowance", "side": "earning", "calc": "per_trip", "amount_paisa": 50_000, "taxable": 1}],
                        attendance={"days_worked_x2": 52, "trips": 3}, eobi_covered=True, ytd_taxable=8_000_000)
    res = R.compute_employee(e, _rules())
    assert (res["gross"], res["deductions"], res["net"], res["employer"]) == (4_150_000, 37_000, 4_113_000, 185_000)
    assert res["tax"] == 0 and not res["errors"]
    assert [ln["code"] for ln in res["lines"]] == ["BASIC", "ALW", "EOBI_EE", "EOBI_ER"]


def test_unverified_figures_refuse_their_line():
    e = R.EmployeeMonth("E1", "Bilal", "2026-09", "monthly", basic=12_000_000, days_employed=30, attendance={"days_worked_x2": 52},
                        eobi_covered=True, ss_covered=True)
    res = R.compute_employee(e, _rules(eobi_base=None, ss_registered=True, ss_rate_bp=600, ss_ceiling=None))
    assert any("EOBI wage base is marked VERIFY" in x for x in res["errors"])
    assert any("ceiling is not set" in x for x in res["errors"])
    stale = dict(SLABS, tax_year=2026)
    assert any("not TY2027" in x for x in R.compute_employee(e, _rules(slabs=stale, eobi_registered=False))["errors"])


def test_advance_recovery_never_drives_net_below_zero_and_plc_caps_it_at_20_percent():
    e = R.EmployeeMonth("E2", "Imran", "2026-09", "monthly", basic=4_000_000, days_employed=30, attendance={"days_worked_x2": 52},
                        advances=[{"advance_id": "ADV-1", "installment": 1_000_000, "outstanding": 1_000_000}])
    res = R.compute_employee(e, _rules(eobi_registered=False))
    adv = [ln for ln in res["lines"] if ln["code"] == "ADV"]
    assert adv[0]["amount"] == 800_000                   # 20% of 40,000 = 8,000 (the 10,000 instalment is cut)
    assert any("cut to Rs 8,000.00" in w for w in res["warnings"])
    res = R.compute_employee(e, _rules(profile="legacy_1969", limits=dict(LEGACY_LIMITS), eobi_registered=False))
    assert [ln["amount"] for ln in res["lines"] if ln["code"] == "ADV"] == [1_000_000]   # legacy: the full instalment
