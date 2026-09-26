"""The derived ledger (plan §2, §4.4): every posting rule, reversals, account resolution by dated method routes, the two
computation paths agreeing, write-offs reaching the P&L, and the V8 schema's own guards."""
from __future__ import annotations

import sqlite3

import pytest

from munshi.domain import accounts as A
from munshi.domain.models import Customer, Product, Route, Supplier, Vehicle, Warehouse, to_paisa
from munshi.domain.repository import MunshiRepository, StateError
from munshi.domain.seed import seeded_repository
from tests.test_reconciliation_month import _load, clock  # noqa: F401  (clock is the fixture)


def _biz(repo: MunshiRepository) -> MunshiRepository:
    repo.upsert_product(Product("UREA", "Urea 50kg", 4000, cost_price=3600))
    repo.upsert_warehouse(Warehouse("WH-A", "Main godown"))
    repo.upsert_customer(Customer("C1", "Malik Agro", "0300-1", credit_limit=5_000_000, route_id="R-A"))
    repo.upsert_supplier(Supplier("S1", "Fauji depot"))
    repo.upsert_route(Route("R-A", "Route A", "WH-A", ["C1"]))
    repo.upsert_vehicle(Vehicle("V1", "MNK-1", "large", 500))
    return repo


def _net(repo, code, acct=None, party=None) -> int:
    return sum(v for (c, a, _, p), v in repo._balances().items() if c == code and (acct is None or a == acct) and (party is None or p == party))


def _src(repo, source):
    return [(p.code, p.money_account_id, p.net_paisa) for p in repo.postings() if p.source == source]


def test_v8_seeds_the_cash_drawer_and_its_route():
    r = MunshiRepository()
    acct = r._one("SELECT * FROM money_accounts WHERE account_id='CASH'")
    assert (acct["kind"], acct["name"], acct["is_default"], acct["active"]) == ("cash", "Cash in hand (galla)", 1, 1)
    assert r.resolve_account("cash") == A.CASH_ACCOUNT_ID
    assert {m: r.resolve_account(m) for m in ("bank", "cheque", "jazzcash", "easypaisa")} == dict.fromkeys(("bank", "cheque", "jazzcash", "easypaisa"), A.UNASSIGNED_ACCOUNT_ID)
    assert r.resolve_account("bank", "ACC-X") == "ACC-X"
    assert r.period_status()["locked_through"] is None


def test_every_operational_row_posts_by_the_rules():
    r = _biz(MunshiRepository())
    r.set_stock("WH-A", "UREA", 100)                                   # opening count: 1200 / 3010 (not a gain)
    assert _src(r, "stock_move") == [("1200", None, 36_000_000), ("3010", None, -36_000_000)]
    r.opening_balance("C1", 10_000, "owner")                           # OPB: 1100 / 3010, not a sale
    r.supplier_opening_balance("S1", 50_000, "owner")                  # 3010 / 2000
    p = r.record_purchase("S1", "WH-A", [{"sku": "UREA", "qty": 10, "unit_cost": 3700}], "B-1", 5_000, "k")   # 1200/2050, 2050/2000, 2000/1000
    pay = r.record_payment("C1", 2_000, "cash", "", "h")               # 1000 CASH / 1100
    chq = r.record_payment("C1", 3_000, "cheque", "HBL", "h")          # no bank account yet: 1900 unassigned
    r.record_expense("fuel", 700, "diesel", "cash", "B", "h")          # 6000:fuel / 1000
    r.add_ledger("C1", "credit_note", -500, "damaged", None, "owner", "owner", "adjustment")   # 4010 / 1100
    r.adjust_stock("WH-A", "UREA", -1, "torn bag", "owner", "owner")   # 5100 / 1200 at the moving average: 39,700,000 x 1/110
    by = r._by_code(r._balances())
    assert by[A.OPENING_BALANCE_EQUITY] == -(36_000_000 + 1_000_000 - 5_000_000)
    assert A.GOODS_RECEIVED_NOT_BILLED not in by and by[A.TRADE_PAYABLES] == -(5_000_000 + 3_700_000 - 500_000)
    assert _net(r, A.MONEY, "CASH") == 200_000 - 500_000 - 70_000
    assert by[A.UNASSIGNED_MONEY] == 300_000
    assert by[A.expense_code("fuel")] == 70_000 and by[A.SALES_RETURNS] == 50_000
    assert by[A.STOCK_ADJUSTMENTS] == 360_909 and by[A.STOCK_GODOWNS] == int(r._one("SELECT SUM(value_paisa) v FROM inventory_value")["v"])
    assert by[A.TRADE_RECEIVABLES] == r.receivables_paisa() and -by[A.TRADE_PAYABLES] == r.payables_paisa()
    assert {(p.party_kind, p.party_id) for p in r.postings() if p.code == A.TRADE_RECEIVABLES} == {("customer", "C1")}
    assert p.purchase_id and pay.entry_id and chq.entry_id
    assert sum(p.net_paisa for p in r.postings()) == 0 and r.trial_balance()["balanced"]


def test_delivery_clears_stock_on_vehicles_and_driver_cash(clock):  # noqa: F811
    r = _biz(MunshiRepository())
    clock.at(3, 9)
    r.set_stock("WH-A", "UREA", 50)
    plan, stops = _load(r, "R-A", [("C1", [{"sku": "UREA", "qty": 10}])])
    by = r._by_code(r._balances())
    assert by[A.STOCK_ON_VEHICLES] == 3_600_000                        # loaded: 1210 / 1200
    st = stops["C1"]
    r.close_stop(st.stop_id, [{"sku": "UREA", "qty": 8}], [{"sku": "UREA", "qty": 2}], 20_000, st.otp, "driver")
    by = r._by_code(r._balances())
    assert A.STOCK_ON_VEHICLES not in by                               # delivered 5000/1210 + returned 1200/1210 = loaded
    assert by[A.DRIVER_CASH_IN_TRANSIT] == 2_000_000 and by[A.COST_OF_GOODS_SOLD] == 2_880_000
    r.record_deposit(plan.plan_id, 19_000, "cashier", "h")             # hand-in 1000/1050; the 1,000 short: 6000:cash_shortage/1050
    by = r._by_code(r._balances())
    assert A.DRIVER_CASH_IN_TRANSIT not in by and by[A.expense_code("cash_shortage")] == 100_000
    assert r.verify_books()["alarms"] == []


def test_payroll_expense_rows_are_skipped_and_their_postings_come_from_payroll(monkeypatch):
    r = _biz(MunshiRepository())
    r._conn.execute("INSERT INTO expenses (expense_id, category, amount, note, method, paid_by, expense_date, created_at) VALUES "
                    "('EXP-P1','staff_salaries',5000000,'PAY-2026-000001 September payroll','payroll','payroll','2026-09-30','2026-09-30T10:00:00+00:00')")
    assert _src(r, "expense") == []
    alarms = {a["code"] for a in r.verify_books("2026-09-30")["alarms"]}
    assert "payroll_expense" in alarms                                 # rows without matching postings: flagged
    posts = A.signed("2026-09-30", A.SALARIES, A.SALARIES_PAYABLE, 5_000_000, source="payroll_run", source_id="PAY-2026-000001")
    monkeypatch.setattr(MunshiRepository, "_payroll_postings", lambda self, s=None, e=None: posts)
    assert r.verify_books("2026-09-30")["alarms"] == [] and _net(r, A.SALARIES_PAYABLE) == -5_000_000


def test_a_remap_never_rewrites_history_and_a_reversal_follows_its_original(clock):  # noqa: F811
    r = _biz(MunshiRepository())
    clock.at(1, 9)
    hbl = r.add_money_account("bank", "HBL", "HBL", actor="owner", approved_by="owner")["account_id"]
    meezan = r.add_money_account("bank", "Meezan", "Meezan", actor="owner", approved_by="owner")["account_id"]
    r.set_method_route("bank", hbl, "2026-09-01", "owner", "owner")
    clock.at(2, 10); first = r.record_payment("C1", 1_000, "bank", "IBFT", "h")
    r.set_method_route("bank", meezan, "2026-09-05", "owner", "owner")    # the owner moves bank receipts to Meezan from the 5th
    clock.at(6, 10); r.record_payment("C1", 2_000, "bank", "IBFT", "h")
    clock.at(7, 10); r.reverse_ledger_entry(first.entry_id, "IBFT recalled", "owner", "owner")   # dated the 7th, but it undoes an HBL receipt
    assert _net(r, A.MONEY, hbl) == 0 and _net(r, A.MONEY, meezan) == 200_000
    assert r.method_routes("2026-09-03")["routes"][1]["account_id"] == hbl and r.resolve_account("bank", on_date="2026-09-06") == meezan
    # an explicit account wins over the route, and must suit the method
    clock.at(8, 10); r.record_payment("C1", 500, "bank", "", "h", account_id=hbl)
    assert _net(r, A.MONEY, hbl) == 50_000
    with pytest.raises(ValueError, match="cannot land in"):
        r.record_payment("C1", 500, "cash", "", "h", account_id=hbl)
    assert r._one("SELECT account_id FROM ledger WHERE reversal_of=?", (first.entry_id,))["account_id"] is None   # history row had none


def test_row_postings_and_grouped_balances_agree_on_the_demo_business():
    r = seeded_repository()
    assert r._aggregate(r.postings()) == r._balances()
    assert r.trial_balance()["balanced"] and r.balance_sheet()["balanced"]
    bs = r.balance_sheet()
    assert bs["assets"]["receivables"] == r.aging_summary()["total"]


def test_the_v5_pool_difference_is_one_opening_line():
    r = _biz(MunshiRepository())
    r.set_stock("WH-A", "UREA", 10)
    r._conn.execute("UPDATE inventory_value SET value_paisa = value_paisa + 12345 WHERE sku='UREA'")   # a pre-V5 pool that never had move values
    lines = [p for p in r.postings() if p.source == "stock_pool"]
    assert [(p.code, p.net_paisa) for p in lines] == [("1200", 12_345), ("3010", -12_345)]
    assert r.balance_sheet()["balanced"] and r._aggregate(r.postings()) == r._balances()


def test_stock_write_offs_reach_the_income_statement_and_profit_summary():
    r = _biz(MunshiRepository())
    r.set_stock("WH-A", "UREA", 10)                                    # opening: not a write-off
    r.adjust_stock("WH-A", "UREA", -2, "rain damage", "owner", "owner")  # 2 x 3,600
    r.adjust_stock("WH-A", "UREA", 1, "found one", "owner", "owner")      # a gain at the average
    today = r.digest()["date"]
    inc = r.income_statement(today, today)
    assert inc["stock_adjustments"] == 3_600.0 and inc["net_profit"] == -3_600.0
    ps = r.profit_summary(today, today)
    assert (ps["net"], ps["stock_adjustments"], ps["net_after_stock_adjustments"]) == (0.0, 3_600.0, -3_600.0)
    assert inc["reconciliation"]["operating_profit"] == ps["net"]


def test_the_database_itself_guards_the_journal_and_the_books():
    r = MunshiRepository()
    c = r._conn
    c.execute("INSERT INTO journal_lines (je_id, account_code, money_account_id, debit_paisa) VALUES ('JV-T1','1000','CASH',100)")
    c.execute("INSERT INTO journal_lines (je_id, account_code, credit_paisa) VALUES ('JV-T1','3000',90)")
    with pytest.raises(sqlite3.IntegrityError, match="does not balance"):
        c.execute("INSERT INTO journal_entries (je_id, entry_date, kind, memo, created_at) VALUES ('JV-T1','2026-09-01','capital','test entry','x')")
    c.execute("INSERT INTO journal_lines (je_id, account_code, credit_paisa) VALUES ('JV-T1','3000',10)")
    c.execute("INSERT INTO journal_entries (je_id, entry_date, kind, memo, created_at) VALUES ('JV-T1','2026-09-01','capital','test entry','x')")
    with pytest.raises(sqlite3.IntegrityError, match="already posted"):
        c.execute("INSERT INTO journal_lines (je_id, account_code, credit_paisa) VALUES ('JV-T1','3000',10)")
    for sql in ("UPDATE journal_lines SET debit_paisa=5 WHERE je_id='JV-T1'", "DELETE FROM journal_entries", "DELETE FROM method_routes",
                "UPDATE money_accounts SET kind='bank' WHERE account_id='CASH'", "DELETE FROM money_accounts WHERE account_id='CASH'"):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):                          # a money line names its account
        c.execute("INSERT INTO journal_lines (je_id, account_code, debit_paisa) VALUES ('JV-T2','1000',100)")
    with pytest.raises(sqlite3.IntegrityError):                          # paisa are integers
        c.execute("INSERT INTO journal_lines (je_id, account_code, debit_paisa) VALUES ('JV-T2','3000',1.5)")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO account_transfers (transfer_id, from_account, to_account, amount_paisa, transfer_date, created_at) VALUES ('X','CASH','CASH',5,'2026-09-01','x')")


def test_postings_of_a_business_with_no_v8_rows_are_just_its_operations():
    r = _biz(MunshiRepository())
    assert r.postings() == [] and r.trial_balance()["balanced"]
    with pytest.raises(StateError):
        r.record_expense("staff_salaries", 100, "x", "cash", "B", "h")   # the payroll categories are payroll's alone
    assert to_paisa(r.balance_sheet()["total_assets"]) == 0
