"""Acceptance: the reconciliation month (tests/test_reconciliation_month.py) through the derived ledger (plan §10.3).

Part A replays that month untouched and requires the balance sheet to balance to the paisa, tie to the recon test's
own numbers (receivables 432,579.75, payables 870,110.00, stock 1,300,559.65, profit_summary net 8,150.90), and the
stock write-off (11,121.25) to reach the income statement -- the gap the accountant found.

Part B is the same month plus company finance and payroll: opening balances, method routes, a loan, a transfer, a
staff advance, drawings, the approved and paid September payroll (as Stream A posts it), and depreciation; then the
balance sheet, income statement, cash flow and HBL reconciliation of §10.3, and the period close. Every expected
figure is worked by hand in the plan, not read back from the code.

Payroll is Stream A's. Until it lands, Part B plugs in exactly what A's approve/pay write (the PAYROLL_METHOD expense
rows, inserted directly, and the postings _payroll_postings() returns) -- the §10.2 register's numbers.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC
from datetime import datetime as _real_datetime

import pytest

from munshi.domain import accounts as A
from munshi.domain.accounts import Posting
from munshi.domain.models import to_paisa
from munshi.domain.repository import InsufficientStockError, MunshiRepository, StateError
from tests.test_reconciliation_month import _business, _load, clock  # noqa: F401  (clock is the fixture)

SEP = ("2026-09-01", "2026-09-30")


def _month(repo: MunshiRepository, clock, hooks: dict | None = None) -> None:  # noqa: F811
    """The reconciliation month, event for event (the same calls, times and amounts as test_reconciliation_month).
    hooks[day] runs right after the clock first reaches that day (Part B's finance events)."""
    hooks = hooks or {}
    ran = set()

    def at(day, hh, mm=0):
        clock.at(day, hh, mm)
        for d in sorted(hooks):
            if d <= day and d not in ran:
                ran.add(d); hooks[d]()
    at(1, 9)
    _business(repo)
    repo.set_stock("WH-A", "UREA", 100); repo.set_stock("WH-A", "DAP", 50)
    repo.opening_balance("C1", 50_000, "owner"); repo.supplier_opening_balance("S1", 200_000, "owner")
    at(2, 10); repo.record_purchase("S1", "WH-A", [{"sku": "UREA", "qty": 200, "unit_cost": 3700.55}], "FF-881", 300_000, "khareed")
    at(3, 11)
    plan1, stops = _load(repo, "R-A", [("C1", [{"sku": "UREA", "qty": 30}]), ("C2", [{"sku": "UREA", "qty": 40}, {"sku": "DAP", "qty": 10}])])
    at(3, 15)
    s1 = stops["C1"]; repo.close_stop(s1.stop_id, [{"sku": "UREA", "qty": 30}], [], 50_000, s1.otp, "driver")
    s2 = stops["C2"]; repo.close_stop(s2.stop_id, [{"sku": "UREA", "qty": 38}, {"sku": "DAP", "qty": 10}], [{"sku": "UREA", "qty": 2}], 0, s2.otp, "driver")
    at(3, 18); repo.record_deposit(plan1.plan_id, 49_500, "cashier", "hisaab")
    at(5, 10)
    urea = repo.get_product("UREA"); urea.cost_price = 9_999; repo.upsert_product(urea)
    repo.record_purchase("S2", "WH-A", [{"sku": "UREA", "qty": 100, "unit_cost": 3800}], "EN-12", 0, "khareed")
    at(8, 12); chq = repo.record_payment("C2", 100_000, "cheque", "HBL 0042", "hisaab", "clerk")
    at(10, 9); wrong = repo.record_expense("fuel", 55_000, "diesel", "cash", "Bilal", "hisaab")
    at(10, 9, 30); repo.reverse_expense(wrong.expense_id, "typed 55,000 for 5,500", "owner", "owner")
    repo.record_expense("fuel", 5_500, "diesel", "cash", "Bilal", "hisaab")
    at(12, 16); repo.reverse_ledger_entry(chq.entry_id, "cheque bounced: insufficient funds", "owner", "owner")
    at(14, 11); repo.pay_supplier("S1", 150_000, "bank", "IBFT 77", "khareed", "owner")
    at(15, 10)
    plan2, stops = _load(repo, "R-A", [("C3", [{"sku": "DAP", "qty": 20}])])
    s3 = stops["C3"]; repo.close_stop(s3.stop_id, [{"sku": "DAP", "qty": 20}], [], 25_000.25, s3.otp, "driver")
    at(15, 19); repo.record_deposit(plan2.plan_id, 25_000.25, "cashier", "hisaab")
    at(18, 10)
    bad = repo.record_purchase("S2", "WH-A", [{"sku": "DAP", "qty": 10, "unit_cost": 6100}], "EN-13?", 10_000, "khareed")
    at(18, 10, 20); repo.reverse_purchase(bad.purchase_id, "wrong supplier bill", "owner", "owner")
    at(20, 10); repo.adjust_stock("WH-A", "UREA", -3, "torn bags", "owner", "owner")
    at(22, 10); repo.transfer_stock("WH-A", "WH-B", "UREA", 20, "godown", "clerk")
    at(25, 17); repo.record_payment("C1", 20_000, "cash", "", "hisaab", "clerk", received_by="office")
    at(28, 9)
    plan3, stops = _load(repo, "R-B", [("C3", [{"sku": "UREA", "qty": 10}])], wh="WH-B")
    s4 = stops["C3"]; repo.close_stop(s4.stop_id, [{"sku": "UREA", "qty": 10}], [], 0, s4.otp, "driver")
    at(28, 12); repo.add_ledger("C1", "credit_note", -5_000, "2 bags damaged in transit", None, "owner", "owner", "adjustment")
    with pytest.raises(InsufficientStockError):
        repo.adjust_stock("WH-B", "UREA", -11, "count", "owner", "owner")
    at(30, 18)


def _rows(t: dict) -> dict[str, float]:
    return {r["line"]: r["amount"] for r in t["rows"] if r.get("amount") is not None}


# ====================================================================== Part A
def test_recon_month_balances_and_ties_to_the_recon_test(clock):  # noqa: F811
    repo = MunshiRepository()
    _month(repo, clock)
    # the recon test's own numbers still hold (nothing about the month changed)
    assert repo.receivables_paisa() == 43_257_975 and repo.payables_paisa() == 87_011_000
    month = repo.profit_summary(*SEP)
    assert (month["net"], month["expenses"]) == (8_150.90, 6_000.0)
    assert (month["stock_adjustments"], month["net_after_stock_adjustments"]) == (11_121.25, -2_970.35)   # the write-off, now visible

    bs = repo.balance_sheet("2026-09-30")
    assert bs["balanced"] and bs["difference"] == 0.0
    assert bs["assets"] == {"money:CASH": -210_999.75, "unassigned": -150_000.0, "receivables": 432_579.75, "stock": 1_300_559.65}
    assert bs["total_assets"] == 1_372_139.65
    assert bs["liabilities"] == {"payables": 870_110.0}
    assert bs["equity"] == {"opening_equity": 505_000.0, "profit": -2_970.35}
    assert bs["total_liabilities"] + bs["total_equity"] == 1_372_139.65
    # cash with drivers, stock on vehicles and goods-received-not-billed all cleared to zero; cash and unassigned flagged
    assert {a["code"] for a in bs["alarms"]} == {"negative_money", "unassigned_money"}
    lines = _rows(bs["table"])
    assert lines["Check: assets - (liabilities + equity)"] == 0.0 and lines["Cash in hand (galla)"] == -210_999.75

    inc = repo.income_statement(*SEP)
    assert (inc["net_revenue"], inc["cost_of_goods"], inc["gross_profit"]) == (477_580.0, 463_429.10, 14_150.90)
    assert inc["expenses_by_category"] == {"fuel": 5_500.0, "cash_shortage": 500.0}
    assert inc["stock_adjustments"] == 11_121.25 and inc["net_profit"] == -2_970.35
    assert inc["reconciliation"] == {"operating_profit": 8_150.90, "other": 0.0, "net_profit": -2_970.35}
    rec = _rows(inc["tables"][1])
    assert rec == {"Operating profit (profit summary)": 8_150.90, "Stock write-offs and adjustments": -11_121.25, "Net profit (income statement)": -2_970.35}
    assert _rows(inc["table"])["Net profit"] == -2_970.35

    tb = repo.trial_balance("2026-09-30")
    assert tb["balanced"] and tb["debits"] == tb["credits"]
    # the two paths agree account by account: row-by-row postings and the grouped balances the statements use
    assert repo._aggregate(repo.postings(None, "2026-09-30")) == repo._balances("2026-09-30")
    assert sum(p.net_paisa for p in repo.postings()) == 0


# ====================================================================== Part B
def _payroll_as_stream_a_posts_it(repo: MunshiRepository, monkeypatch) -> list[Posting]:
    """What Stream A's approve_payroll + pay_salaries + give_staff_advance leave behind for September (plan §10.2):
    the PAYROLL_METHOD expense rows (inserted directly, as A must) and the postings _payroll_postings() returns."""
    book: list[Posting] = []
    monkeypatch.setattr(MunshiRepository, "_payroll_postings",
                        lambda self, start=None, end=None: [p for p in book if (not start or p.on >= start) and (not end or p.on <= end)])
    return book


def _approve_and_pay(repo: MunshiRepository, book: list[Posting]) -> None:
    on = "2026-09-30"
    staff = {"staff_salaries": 23_692_308, "staff_wages": 6_400_000, "staff_allowances": 150_000, "staff_commission": 319_080, "employer_contributions": 740_000}
    for cat, p in staff.items():
        repo._conn.execute("INSERT INTO expenses (expense_id, category, amount, note, method, paid_by, expense_date, created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (f"EXP-PAY-{cat[:8]}", cat, p, "PAY-2026-000001 September payroll", A.PAYROLL_METHOD, "payroll", on, "2026-09-30T13:00:00+00:00"))
        book.append(Posting(on, A.PAYROLL_CATEGORY_CODE[cat], debit_paisa=p, source="payroll_run", source_id="PAY-2026-000001"))
    for code, p, extra in ((A.SALARIES_PAYABLE, 29_643_388, {}), (A.INCOME_TAX_WITHHELD, 270_000, {}), (A.EOBI_PAYABLE, 888_000, {}),
                           (A.STAFF_ADVANCES, 500_000, {"party_kind": "employee", "party_id": "EMP-IMRAN"})):
        book.append(Posting(on, code, credit_paisa=p, source="payroll_run", source_id="PAY-2026-000001", **extra))
    for sid, acct, p in (("SPM-2026-000001", "ACC-HBL", 11_693_000), ("SPM-2026-000002", "ACC-JAZZCASH", 3_782_080),
                         ("SPM-2026-000003", "CASH", 4_113_000), ("SPM-2026-000004", "CASH", 3_655_308), ("SPM-2026-000005", "CASH", 3_520_000)):
        book += A.signed(on, A.SALARIES_PAYABLE, A.MONEY, p, source="salary_payment", source_id=sid, credit_money_account_id=acct, memo="salary")


def _full_month(repo, clock, monkeypatch):  # noqa: F811
    book = _payroll_as_stream_a_posts_it(repo, monkeypatch)
    ids = {}

    def sep1():
        hbl = repo.add_money_account("bank", "HBL current", provider="HBL", number_last4="0042", actor="owner", approved_by="owner")
        jazz = repo.add_money_account("wallet", "JazzCash business", provider="JazzCash", actor="owner", approved_by="owner")
        ids.update(hbl=hbl["account_id"], jazz=jazz["account_id"])
        repo.record_opening_balances("2026-09-01", money=[{"account_id": "CASH", "amount": 400_000}, {"account_id": ids["hbl"], "amount": 500_000}],
                                     assets=[{"name": "Shehzore LEU-1234", "category": "vehicle", "cost": 2_400_000, "life_months": 60}], loans=[],
                                     actor="owner", approved_by="owner")
        for m in ("bank", "cheque"): repo.set_method_route(m, ids["hbl"], "2026-09-01", "owner", "owner")
        repo.set_method_route("jazzcash", ids["jazz"], "2026-09-01", "owner", "owner")

    def sep5():
        ids["loan"] = repo.add_loan("Haji Rasheed", "informal", 200_000, "bank", on_date="2026-09-05", terms="no markup; return by Eid",
                                    actor="owner", approved_by="owner")["loan_id"]
        repo.transfer(ids["hbl"], ids["jazz"], 60_000, on_date="2026-09-05", ref="IBFT 91", actor="owner")

    def sep10():     # Imran's advance via JazzCash (Stream A's give_staff_advance)
        book.extend(A.signed("2026-09-10", A.STAFF_ADVANCES, A.MONEY, 1_000_000, source="staff_advance", source_id="ADV-2026-000001",
                             debit_party_kind="employee", debit_party_id="EMP-IMRAN", credit_money_account_id=ids["jazz"]))

    def sep25():
        repo.record_drawing(30_000, "cash", on_date="2026-09-25", note="household", actor="owner", approved_by="owner")

    _month(repo, clock, {1: sep1, 5: sep5, 10: sep10, 25: sep25})
    _approve_and_pay(repo, book)
    repo.run_depreciation("2026-09", "owner", "owner")
    return ids


def test_full_month_with_payroll_and_finance(clock, monkeypatch):  # noqa: F811
    repo = MunshiRepository()
    ids = _full_month(repo, clock, monkeypatch)
    assert (ids["hbl"], ids["jazz"]) == ("ACC-HBL", "ACC-JAZZCASH")
    # money accounts at Sep 30 (plan §10.3 workings)
    assert repo.account_balance_paisa("CASH", "2026-09-30") == 4_611_717          # 400,000 - 210,999.75 - 112,883.08 - 30,000
    assert repo.account_balance_paisa("ACC-HBL", "2026-09-30") == 37_307_000      # 500,000 + 200,000 - 60,000 + 100,000 - 100,000 - 150,000 - 116,930
    assert repo.account_balance_paisa("ACC-JAZZCASH", "2026-09-30") == 1_217_920  # 60,000 - 10,000 - 37,820.80

    bs = repo.balance_sheet("2026-09-30")
    assert bs["balanced"] and bs["alarms"] == []
    assert bs["total_assets"] == 4_529_505.77 == bs["total_liabilities"] + bs["total_equity"]
    assert bs["money_total"] == 431_366.37
    assert {k: v for k, v in bs["assets"].items() if not k.startswith("money:")} == {
        "receivables": 432_579.75, "staff_advances": 5_000.0, "stock": 1_300_559.65, "fixed_assets": 2_400_000.0, "acc_dep": -40_000.0}
    loan_key = f"loan:{ids['loan']}"
    assert bs["liabilities"] == {"payables": 870_110.0, "c:2100": 28_800.0, "c:2110": 2_700.0, "c:2120": 8_880.0, loan_key: 200_000.0}
    assert bs["equity"] == {"opening_equity": 3_805_000.0, "profit": -355_984.23, "drawings": -30_000.0}
    assert _rows(bs["table"])["Loan: Haji Rasheed"] == 200_000.0

    inc = repo.income_statement(*SEP, redact_payroll=False)
    assert inc["gross_profit"] == 14_150.90 and inc["staff_costs"] == 313_013.88 and inc["depreciation"] == 40_000.0
    assert inc["net_profit"] == -355_984.23
    assert inc["reconciliation"] == {"operating_profit": -304_862.98, "other": 0.0, "net_profit": -355_984.23}
    assert inc["staff_costs_by_code"] == {"Salaries": 236_923.08, "Wages": 64_000.0, "Allowances": 1_500.0, "Commission": 3_190.80, "Employer contributions": 7_400.0}

    cf = repo.cash_flow(*SEP)
    assert (cf["opening"], cf["net_change"], cf["closing"], cf["accounts_total"], cf["ties"]) == (900_000.0, -468_633.63, 431_366.37, 431_366.37, True)
    assert cf["operating"] == {"total": -638_633.63, "lines": {"Received from customers": 94_500.25, "Paid to suppliers": -450_000.0, "Expenses paid": -5_500.0,
                                                              "Salaries paid": -267_633.88, "Staff advances (net)": -10_000.0}}
    assert cf["financing"] == {"total": 170_000.0, "lines": {"Loans received": 200_000.0, "Drawings": -30_000.0}}
    assert cf["investing"]["total"] == 0.0 and cf["transfers_memo"] == 60_000.0

    # HBL reconciliation: everything but Bilal's salary transfer is on the statement
    book = repo._book_items("ACC-HBL", None, "2026-09-30")
    tick = [{"source": s, "source_id": sid} for (_, s, sid) in book if sid != "SPM-2026-000001"]
    repo.mark_cleared("ACC-HBL", tick, "2026-09-30", True, "owner")
    rec = repo.save_reconciliation("ACC-HBL", "2026-09-30", 490_000, "owner")
    assert (rec["book"], rec["uncleared_out_total"], rec["adjusted"], rec["difference"]) == (373_070.0, 116_930.0, 490_000.0, 0.0)
    assert [x["source_id"] for x in rec["uncleared_out"]] == ["SPM-2026-000001"]
    assert repo.list_money_accounts()["table"]["totals"]["balance"] == 431_366.37

    assert repo.verify_books("2026-09-30")["alarms"] == []
    assert repo.trial_balance("2026-09-30")["balanced"]
    assert repo._aggregate(repo.postings(None, "2026-09-30")) == repo._balances("2026-09-30")

    # ---- the period close (plan §10.3): October 2
    clock.utc = _real_datetime(2026, 10, 2, 6, 0, tzinfo=UTC)
    with pytest.raises(StateError, match="before today"):
        repo.close_period("2026-10-02", actor="owner", approved_by="owner")
    closed = repo.close_period("2026-09-30", note="September books", actor="owner", approved_by="owner")
    assert closed["locked_through"] == "2026-09-30"
    with pytest.raises(StateError, match="closed through 2026-09-30"):
        repo.record_expense("fuel", 100, "late slip", "cash", "Bilal", "hisaab", expense_date="2026-09-30")
    with pytest.raises(sqlite3.IntegrityError):                           # the database refuses it even behind the repository's back
        repo._conn.execute("INSERT INTO expenses (expense_id, category, amount, note, method, paid_by, expense_date, created_at) "
                           "VALUES ('EXP-X','fuel',100,'x','cash','x','2026-09-15','2026-10-02T06:00:00+00:00')")
    with pytest.raises(StateError):
        repo.post_journal("2026-09-30", "bank_charge", "HBL charges", [{"code": "7000", "debit": 500}, {"code": "1000", "account_id": "ACC-HBL", "credit": 500}],
                          actor="owner", approved_by="owner")
    with pytest.raises(StateError):
        repo.assert_period_open("2026-09-10")                             # what Stream A's September advance calls
    oct_ = repo.post_journal("2026-10-01", "bank_charge", "HBL charges", [{"code": "7000", "debit": 500}, {"code": "1000", "account_id": "ACC-HBL", "credit": 500}],
                             actor="owner", approved_by="owner")
    assert oct_["entry_date"] == "2026-10-01"
    assert repo.verify_books()["alarms"] == []                             # an October entry never drifts September
    reopened = repo.reopen_period(closed["close_id"], "late supplier bill found", "owner", "owner")
    assert reopened["locked_through"] is None and reopened["closes"][0]["reopen_reason"] == "late supplier bill found"
    assert repo._one("SELECT snapshot FROM period_closes WHERE close_id=?", (closed["close_id"],))["snapshot"]
    assert [a["action"] for a in repo.audit_log(5) if a["action"] in ("close_period", "reopen_period")] == ["reopen_period", "close_period"]
    repo.record_expense("fuel", 100, "late slip", "cash", "Bilal", "hisaab", expense_date="2026-09-30")    # open again


def test_clerk_sees_one_staff_costs_line_and_the_owner_the_breakdown(clock, monkeypatch):  # noqa: F811
    repo = MunshiRepository()
    _full_month(repo, clock, monkeypatch)
    clerk = repo.profit_summary(*SEP)                                     # the default is the safe one
    owner = repo.profit_summary(*SEP, redact_payroll=False)
    assert clerk["expenses_by_category"] == {"fuel": 5_500.0, "cash_shortage": 500.0, "staff_costs": 313_013.88}
    assert not set(clerk["expenses_by_category"]) & set(A.SYSTEM_EXPENSE_CATEGORIES)
    assert owner["expenses_by_category"] == {"fuel": 5_500.0, "cash_shortage": 500.0, "staff_salaries": 236_923.08, "staff_wages": 64_000.0,
                                             "staff_allowances": 1_500.0, "staff_commission": 3_190.80, "employer_contributions": 7_400.0}
    assert clerk["net"] == owner["net"] == -304_862.98 and clerk["expenses"] == owner["expenses"]
    masked = repo.income_statement(*SEP, redact_payroll=True)
    assert "staff_costs_by_code" not in masked and masked["staff_costs"] == 313_013.88
    staff_lines = [r for r in masked["table"]["rows"] if "Staff" in r["line"]]
    assert [(r["line"], r["amount"]) for r in staff_lines] == [("Staff costs", -313_013.88)]
    full = repo.income_statement(*SEP)
    assert len([r for r in full["table"]["rows"] if r["line"].startswith("Staff costs:")]) == 5
    # the cashbook and the account book: payroll lines summed per day under the redacted label for a clerk
    day = repo.cashbook("2026-09-30")
    assert [(x["kind"], x["who"], x["amount"]) for x in day["cash_out"]] == [("staff payments", A.PAYROLL_REDACTED_LABEL, 112_883.08)]
    owner_day = repo.cashbook("2026-09-30", redact_payroll=False)
    assert sorted(x["ref"] for x in owner_day["cash_out"]) == ["SPM-2026-000003", "SPM-2026-000004", "SPM-2026-000005"]
    book = repo.account_book("CASH", *SEP, redact_payroll=True)
    red = [r for r in book["table"]["rows"] if r["kind"] == "staff payments"]
    assert [(r["narration"], r["money_out"]) for r in red] == [(A.PAYROLL_REDACTED_LABEL, 112_883.08)]
    assert not any(r["doc"].startswith("SPM") for r in book["table"]["rows"])
    assert book["closing"] == 46_117.17
    # the digest's "today's expenses" is money spent, not the month's accrued pay
    assert repo.digest("2026-09-30")["cash"]["expenses"] == 0.0
    assert repo.expenses_paisa(*SEP) - repo.expenses_paisa(*SEP, exclude_payroll=True) == to_paisa(313_013.88)
