"""Acceptance: the plan's §10.2 worked month for a 6-person distributor, September 2026, under the owner's default
Punjab Labour Code 2026 profile. Every expected figure is worked by hand in the comments, never read back from the
code under test.

People (Punjab; EOBI registered; the owner has verified the EOBI base at Rs 37,000; 26-day basis; tax rounded to the rupee):
  Bilal   clerk     monthly 120,000; EOBI; opening YTD (Jul+Aug) taxable 240,000, tax 5,400       -> income tax withheld
  Imran   salesman  monthly 40,000; EOBI; 1% commission on HIS OWN delivered sales; advance 10,000 by JazzCash on
                    Sep 10, instalment 5,000; opening YTD 80,000 / 0
  Rafiq   driver    monthly 40,000 + Rs 500 per trip (3 trips); EOBI; opening YTD 80,000 / 0
  Nadeem  godown    monthly 40,000; EOBI; 2 days unpaid absence; opening YTD 80,000 / 0
  Shafiq  loader    daily 1,600 x 22 days; not EOBI-covered; CASH ALLOWED by the owner (daily-wage loader)
  Kashif  loader    daily 1,600 x 18 days; left unpaid at month end
"""
from __future__ import annotations

import sqlite3

import pytest

from munshi.domain.models import Customer, Product, Route, Vehicle, Warehouse
from munshi.domain.repository import MunshiRepository, StateError
from tests.test_payroll_support import enable_payroll, install_clock, set_eobi_base

OWNER = "owner:Sultan"
P = 100          # paisa per rupee


@pytest.fixture
def clock(monkeypatch):
    return install_clock(monkeypatch)


def _sales_by_imran(repo: MunshiRepository, clock) -> None:
    """Sep 3: Imran books two orders; the driver delivers them (C2 two bags short). Delivered revenue:
    C1 30 x 3,850 = 115,500.00; C2 at 2.5% off: UREA 3,753.75 x 38 = 142,642.50 + DAP 6,093.75 x 10 = 60,937.50 -> 203,580.00.
    Imran's basis = 115,500 + 203,580 = 319,080.00. A sale booked by someone else must not count."""
    repo.upsert_product(Product("UREA", "Urea 50kg", 3850, cost_price=3600))
    repo.upsert_product(Product("DAP", "DAP 50kg", 6250, cost_price=5900))
    repo.upsert_warehouse(Warehouse("WH-A", "Main godown"))
    repo.upsert_customer(Customer("C1", "Malik Agro", "0300-1", credit_limit=2_000_000, route_id="R-A"))
    repo.upsert_customer(Customer("C2", "Chaudhry Farms", "0300-2", credit_limit=2_000_000, route_id="R-A", discount_pct=2.5))
    repo.upsert_customer(Customer("C3", "Green Valley", "0300-3", credit_limit=1_000_000, route_id="R-A"))
    repo.upsert_route(Route("R-A", "Route A", "WH-A", ["C1", "C2", "C3"]))
    repo.upsert_vehicle(Vehicle("V1", "MNK-1", "large", 500))
    repo.set_stock("WH-A", "UREA", 200); repo.set_stock("WH-A", "DAP", 50)
    clock.at(3, 11)
    oids = []
    for who, cust, lines in (("Imran", "C1", [{"sku": "UREA", "qty": 30}]),
                             ("Imran", "C2", [{"sku": "UREA", "qty": 40}, {"sku": "DAP", "qty": 10}]),
                             ("Bilal", "C3", [{"sku": "DAP", "qty": 5}])):          # Bilal's order: not Imran's commission
        with repo.acting_as(who):
            o = repo.create_order(cust, lines, "app", "", "salesman")
        repo.confirm_order(o.order_id, "office", "clerk"); repo.allocate_order(o.order_id, "WH-A", "godown", "clerk")
        oids.append(o.order_id)
    plan = repo.create_dispatch_plan("2026-09-03", "R-A", "V1", oids, "godown")
    repo.approve_dispatch_plan(plan.plan_id, "godown", "owner")
    stops = {s.customer_id: s for s in repo.list_stops(plan.plan_id)}
    clock.at(3, 15)
    assert repo.close_stop(stops["C1"].stop_id, [{"sku": "UREA", "qty": 30}], [], 0, stops["C1"].otp, "driver")["invoiced"] == 115_500.0
    assert repo.close_stop(stops["C2"].stop_id, [{"sku": "UREA", "qty": 38}, {"sku": "DAP", "qty": 10}], [{"sku": "UREA", "qty": 2}], 0,
                           stops["C2"].otp, "driver")["invoiced"] == 203_580.0
    assert repo.close_stop(stops["C3"].stop_id, [{"sku": "DAP", "qty": 5}], [], 0, stops["C3"].otp, "driver")["invoiced"] == 31_250.0


def _staff(repo: MunshiRepository) -> dict:
    ids = {}
    people = [
        ("Bilal", "clerk", "bank", 120_000, None, True, 240_000, 5_400, {}),
        ("Imran", "salesman", "jazzcash", 40_000, None, True, 80_000, 0, {}),
        ("Rafiq", "driver", "bank", 40_000, None, True, 80_000, 0, {}),
        ("Nadeem", "godown", "bank", 40_000, None, True, 80_000, 0, {}),
        ("Shafiq", "loader", "cash", None, 1_600, False, 0, 0, {"cash_allowed": True, "cash_allowed_note": "daily-wage loader, paid at day end, no bank account"}),
        ("Kashif", "loader", "easypaisa", None, 1_600, False, 0, 0, {}),
    ]
    for name, role, method, basic, daily, eobi, ytd_t, ytd_x, extra in people:
        e = repo.add_employee({"name": name, "role_hint": role, "designation": role, "joined_on": "2025-03-01", "eobi_covered": eobi,
                               "opening_tax_year": 2027, "opening_ytd_taxable": ytd_t, "opening_ytd_tax": ytd_x, "pay_method": method,
                               "pay_account_id": {"bank": "ACC-HBL", "jazzcash": "ACC-JAZZ", "easypaisa": "ACC-EP"}.get(method), **extra}, "owner", OWNER)
        comps = [{"code": "TRIP", "label": "Trip allowance", "calc": "per_trip", "amount": 500}] if name == "Rafiq" else []
        if basic:
            repo.set_pay_structure(e["employee_id"], "2025-03-01", "monthly", basic=basic, components=comps, actor="owner", approved_by=OWNER)
        else:
            repo.set_pay_structure(e["employee_id"], "2025-03-01", "daily", daily_rate=daily, actor="owner", approved_by=OWNER)
        ids[name] = e["employee_id"]
    repo.set_commission_rule(ids["Imran"], "booked_sales", rate_pct=1, effective_from="2026-07-01", actor="owner", approved_by=OWNER)
    return ids


def _attendance(repo, ids, kashif_days=18):
    repo.set_attendance("2026-09", [
        {"employee_id": ids["Bilal"], "days_worked": 26}, {"employee_id": ids["Imran"], "days_worked": 26},
        {"employee_id": ids["Rafiq"], "days_worked": 26, "trips": 3}, {"employee_id": ids["Nadeem"], "days_worked": 24, "unpaid_absent": 2},
        {"employee_id": ids["Shafiq"], "days_worked": 22}, {"employee_id": ids["Kashif"], "days_worked": kashif_days}], "clerk", "clerk:Bilal")


def _sum(postings, code, side):
    return sum(getattr(p, f"{side}_paisa") for p in postings if p.code == code)


def test_the_worked_month(clock):
    repo = enable_payroll(MunshiRepository())
    clock.at(1, 9)
    repo.set_setting("business_name", "Sultan Traders")
    assert repo.payroll_profile() == "plc_2026"                          # the owner's default
    repo.set_payroll_settings({"eobi_registered": "1"}, "owner", OWNER)
    _sales_by_imran(repo, clock)
    ids = _staff(repo)

    # ---------------- the EOBI base is seeded flagged VERIFY: EOBI is refused until the owner sets it
    clock.at(10, 10)
    adv = repo.give_staff_advance(ids["Imran"], 10_000, "jazzcash", "ACC-JAZZ", installment=5_000, actor="owner", approved_by=OWNER, given_on="2026-09-10")
    #   PLC: 10,000 <= 3 x 40,000; instalment 5,000 <= 20% of 40,000 = 8,000; JazzCash is a digital channel
    assert adv["advance_id"] == "ADV-2026-000001" and [s["instalment"] for s in adv["schedule"]] == [5_000.0, 5_000.0]
    _attendance(repo, ids)
    clock.at(30, 17)
    pv = repo.preview_payroll("2026-09")
    assert not pv["can_approve"] and sum("EOBI wage base is marked VERIFY" in e for e in pv["errors"]) == 4
    set_eobi_base(repo, 37_000)

    pv = repo.preview_payroll("2026-09")
    assert pv["errors"] == [] and pv["can_approve"], pv["errors"]
    by = {e["name"]: e for e in pv["employees"]}

    def lines(name):
        return {ln["code"]: ln["amount"] for ln in by[name]["lines"]}
    # Bilal: tax 2,700 (projected 240,000 + 120,000 + 9 x 120,000 = 1,440,000 -> 32,400; (32,400 - 5,400) / 10)
    assert lines("Bilal") == {"BASIC": 120_000.0, "EOBI_EE": 370.0, "TAX": 2_700.0, "EOBI_ER": 1_850.0}
    assert by["Bilal"]["net"] == 116_930.0                              # 120,000 - 370 - 2,700
    # Imran: commission 1% of 319,080 = 3,190.80 (Bilal's C3 order is not his); advance 5,000; tax 0
    assert lines("Imran") == {"BASIC": 40_000.0, "COMM": 3_190.8, "EOBI_EE": 370.0, "ADV": 5_000.0, "EOBI_ER": 1_850.0}
    assert (by["Imran"]["gross"], by["Imran"]["net"]) == (43_190.8, 37_820.8)   # 43,190.80 - 370 - 5,000
    assert pv["commission_basis"][0]["basis"] == 319_080.0
    # Rafiq: 3 trips x 500 = 1,500 allowance
    assert lines("Rafiq") == {"BASIC": 40_000.0, "ALW": 1_500.0, "EOBI_EE": 370.0, "EOBI_ER": 1_850.0} and by["Rafiq"]["net"] == 41_130.0
    # Nadeem: absence 40,000 x 2 / 26 = 3,076.92
    assert lines("Nadeem") == {"BASIC": 40_000.0, "ABSENCE": 3_076.92, "EOBI_EE": 370.0, "EOBI_ER": 1_850.0} and by["Nadeem"]["net"] == 36_553.08
    # loaders: 1,600 x 22 = 35,200; 1,600 x 18 = 28,800
    assert lines("Shafiq") == {"BASIC": 35_200.0} and lines("Kashif") == {"BASIC": 28_800.0}
    t = pv["table"]["totals"]
    assert (t["basic"], t["allowances"], t["commission"], t["gross"]) == (304_000.0, 1_500.0, 3_190.8, 308_690.8)
    assert (t["absence"], t["eobi_ee"], t["income_tax"], t["advance"], t["total_deductions"]) == (3_076.92, 1_480.0, 2_700.0, 5_000.0, 12_256.92)
    assert (t["net_pay"], t["eobi_er"]) == (296_433.88, 7_400.0)

    # ---------------- a stale preview is refused; a clerk (no named approver) cannot approve
    _attendance(repo, ids, kashif_days=19)
    with pytest.raises(StateError, match="out of date"):
        repo.approve_payroll("2026-09", pv["fingerprint"], "owner", OWNER)
    _attendance(repo, ids, kashif_days=18)
    fp = repo.preview_payroll("2026-09")["fingerprint"]
    with pytest.raises(PermissionError):
        repo.approve_payroll("2026-09", fp, "clerk", None)
    reg = repo.approve_payroll("2026-09", fp, "owner", OWNER)
    run_id = reg["run"]["run_id"]
    assert run_id == "PAY-2026-000001" and reg["run"]["net"] == 296_433.88 and reg["run"]["headcount"] == 6
    assert [s["slip_id"] for s in reg["payslips"]] == [f"PSL-2026-00000{i}" for i in range(1, 7)]

    # ---------------- attendance and adjustments are locked once approved (repository AND database trigger)
    with pytest.raises(StateError, match="approved"):
        _attendance(repo, ids)
    with pytest.raises(sqlite3.IntegrityError, match="approved"):
        repo._conn.execute("UPDATE attendance_months SET trips=9 WHERE employee_id=? AND period='2026-09'", (ids["Rafiq"],))
    with pytest.raises(StateError, match="approved"):
        repo.add_payroll_adjustment(ids["Rafiq"], "2026-09", "bonus", 1_000, "Eid bonus", actor="owner", approved_by=OWNER)

    # ---------------- expense rows (method 'payroll', dated the period end), decision 3
    exp = {r["category"]: r["amount"] for r in repo._all("SELECT category, amount FROM expenses WHERE method='payroll'")}
    assert exp == {"staff_salaries": 23_692_308,        # 240,000 - 3,076.92 = 236,923.08
                   "staff_wages": 6_400_000,            # 35,200 + 28,800
                   "staff_allowances": 150_000, "staff_commission": 319_080,
                   "employer_contributions": 740_000}   # 4 x 1,850
    assert sum(exp.values()) == 31_301_388              # 313,013.88
    assert {r["expense_date"] for r in repo._all("SELECT expense_date FROM expenses WHERE method='payroll'")} == {"2026-09-30"}

    # ---------------- payroll postings (accrual on approval): balanced at 313,013.88
    run_posts = [p for p in repo._payroll_postings("2026-09-30", "2026-09-30") if p.source == "payroll_run"]
    assert {c: _sum(run_posts, c, "debit") for c in ("6100", "6110", "6120", "6130", "6200")} == {
        "6100": 23_692_308, "6110": 6_400_000, "6120": 150_000, "6130": 319_080, "6200": 740_000}
    assert {c: _sum(run_posts, c, "credit") for c in ("2100", "2110", "2120", "1150")} == {
        "2100": 29_643_388, "2110": 270_000, "2120": 888_000, "1150": 500_000}      # 2120 = 1,480 + 7,400
    assert sum(p.debit_paisa for p in run_posts) == sum(p.credit_paisa for p in run_posts) == 31_301_388

    # ---------------- statutory summaries
    eobi = repo.statutory_summary("2026-09", "eobi")
    assert (eobi["due"], len(eobi["rows"])) == (8_880.0, 4)                     # 4 x (370 + 1,850)
    wht = repo.statutory_summary("2026-09", "income_tax")
    assert wht["due"] == 2_700.0 and [r["name"] for r in wht["rows"]] == ["Bilal"]
    assert wht["rows"][0]["projected_annual"] == 1_440_000.0 and wht["rows"][0]["annual_tax"] == 32_400.0
    assert "does not give tax or legal advice" in wht["table"]["note"]

    # ---------------- salary payments, Sep 30. PLC: cash refused for Nadeem; Shafiq relies on the owner's exemption
    with pytest.raises(StateError, match="Punjab Labour Code 2026"):
        repo.pay_salaries(run_id, [{"employee_id": ids["Nadeem"], "method": "cash", "paid_on": "2026-09-30"}], "owner", OWNER)
    paid = repo.pay_salaries(run_id, [
        {"employee_id": ids["Bilal"], "paid_on": "2026-09-30"},                                   # bank HBL (his default) 116,930
        {"employee_id": ids["Imran"], "paid_on": "2026-09-30"},                                   # JazzCash 37,820.80
        {"employee_id": ids["Rafiq"], "paid_on": "2026-09-30"}, {"employee_id": ids["Nadeem"], "paid_on": "2026-09-30"},
        {"employee_id": ids["Shafiq"], "method": "cash", "paid_on": "2026-09-30"}], "owner", OWNER)
    assert [(p["name"], p["amount"], p["account_id"], p["cash_exemption"]) for p in paid["payments"]] == [
        ("Bilal", 116_930.0, "ACC-HBL", False), ("Imran", 37_820.8, "ACC-JAZZ", False), ("Rafiq", 41_130.0, "ACC-HBL", False),
        ("Nadeem", 36_553.08, "ACC-HBL", False), ("Shafiq", 35_200.0, "CASH", True)]
    #   41,130 + 36,553.08 + 35,200 = 112,883.08 -- the plan's "Rafiq, Nadeem and Shafiq" total
    assert sum(p["amount"] for p in paid["payments"][2:]) == pytest.approx(112_883.08)
    with pytest.raises(StateError, match="owed Rs 0.00|nothing is due"):
        repo.pay_salaries(run_id, [{"employee_id": ids["Bilal"], "amount": 1, "paid_on": "2026-09-30"}], "owner", OWNER)
    slip = repo.payslip(employee_id=ids["Shafiq"], period="2026-09")
    assert slip["meta"]["cash_exemption_note"] == "Paid in cash with the owner's exemption" and slip["status"] == "paid"
    assert "Paid in cash with the owner's exemption" in slip["table"]["note"]
    imran = repo.payslip(employee_id=ids["Imran"], period="2026-09")
    items = [r["item"] for r in imran["table"]["rows"]]
    assert items.index("Gross (basic and allowances)") < items.index("Total deductions") < items.index("Net remuneration") < items.index("Total payment")
    assert imran["meta"]["leave_balances"] == {"annual": 18.0, "casual": 10.0, "sick": 8.0}       # PLC entitlements, none taken

    # ---------------- what is left: Kashif's 28,800 owed; the advance's other half; WHT and EOBI due
    liab = repo.payroll_liabilities_paisa("2026-09-30")
    assert (liab["2100"], liab["2110"], liab["2120"], liab["1150"]) == (2_880_000, 270_000, 888_000, 500_000)
    assert repo.staff_advances_report()["outstanding"] == 5_000.0
    assert repo.staff_advances_report()["advances"][0]["next_period"] == "2026-10"

    # ---------------- the books (Stream B) see the real payroll: the balance sheet balances and shows what is owed
    bs = repo.balance_sheet("2026-09-30")
    assert bs["balanced"], bs
    ok = repo.verify_books("2026-09-30")
    assert not [a for a in ok["alarms"] if a["code"] != "negative_money"], ok["alarms"]   # the test's banks start empty

    # ---------------- reversal: refused while payments stand; after reversing them, every expense row nets to zero
    with pytest.raises(StateError, match="reverse those payments first"):
        repo.reverse_payroll_run(run_id, "wrong month", "owner", OWNER)
    clock.at(30, 18)
    for p in paid["payments"]:
        repo.reverse_salary_payment(p["payment_id"], "reissue after reversal", "owner", OWNER)
    rev = repo.reverse_payroll_run(run_id, "attendance was wrong", "owner", OWNER)
    assert rev["reversal"] == "PAY-2026-000002"
    net = repo._all("SELECT category, SUM(amount) s FROM expenses WHERE method='payroll' GROUP BY category")
    assert net and all(r["s"] == 0 for r in net)
    liab = repo.payroll_liabilities_paisa("2026-09-30")
    assert (liab["2100"], liab["2110"], liab["2120"], liab["1150"]) == (0, 0, 0, 1_000_000)     # the advance is fully owed again
    assert repo.payroll_register(run_id=run_id)["run"]["status"] == "reversed"

    # ---------------- the month unlocks; a second generation re-approves with the same figures
    _attendance(repo, ids)
    pv2 = repo.preview_payroll("2026-09")
    reg2 = repo.approve_payroll("2026-09", pv2["fingerprint"], "owner", OWNER)
    assert reg2["run"]["generation"] == 2 and reg2["run"]["net"] == 296_433.88
    # one audit row per gated call: approve_payroll_run twice (two approvals), reverse once
    acts = [r["action"] for r in repo._all("SELECT action FROM audit")]
    assert acts.count("approve_payroll_run") == 2 and acts.count("reverse_payroll_run") == 1 and acts.count("pay_salaries") == 1
    # audit payloads carry no rupee figure of anyone's pay (clerks can read the audit list)
    for r in repo._all("SELECT payload FROM audit WHERE action IN ('approve_payroll_run','pay_salaries','set_pay_structure','give_staff_advance')"):
        assert "116930" not in r["payload"] and "120000" not in r["payload"] and "10000" not in r["payload"]
