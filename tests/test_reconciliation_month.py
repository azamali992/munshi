"""Acceptance: one realistic month of a distributor's books, then everything must reconcile to the paisa.

September 2026, a fresh business with two godowns: opening stock and balances, purchases (one of them
mis-keyed and reversed), sales on credit with partial cash, a short delivery with a return, a bounced
cheque (reversal), a mis-keyed expense (reversal + re-entry), a supplier payment, a stock write-off, a
transfer, a credit note, a product cost-price edit and a dearer purchase partway through. Every expected
figure below is worked by hand in the comments, not read back from the code under test.

Then, separately: real threads on two connections to one file prove document numbers never collide or
skip, and a failed transaction never burns a number.
"""
from __future__ import annotations

import importlib
import threading
from datetime import UTC, timedelta, timezone
from datetime import date as _real_date
from datetime import datetime as _real_datetime

import pytest

from munshi.domain.models import Customer, Product, Route, Supplier, Vehicle, Warehouse, mul_div, now_iso, to_business_date, to_paisa, to_rupees
from munshi.domain.repository import InsufficientStockError, MunshiRepository
from munshi.domain.repository.guarded import immediate_tx
from munshi.domain.repository.numbering import next_doc_no

PKT = timezone(timedelta(hours=5), "PKT")
_CLOCK_MODULES = ("munshi.domain.models", "munshi.domain.seed", "munshi.domain.repository.base", "munshi.domain.repository.cash",
                  "munshi.domain.repository.reports", "munshi.domain.repository.collections", "munshi.domain.repository.dispatch",
                  "munshi.domain.repository.orders", "munshi.domain.repository.master")


class Clock:
    utc = _real_datetime(2026, 9, 1, tzinfo=UTC)

    def at(self, day: int, hh: int, mm: int = 0) -> None:
        """Set the wall clock to <day> September 2026, hh:mm Karachi time."""
        self.utc = _real_datetime(2026, 9, day, hh, mm, tzinfo=PKT).astimezone(UTC)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()

    class FakeDateTime(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            return c.utc.astimezone(tz) if tz else c.utc.replace(tzinfo=None)

    class FakeDate(_real_date):
        @classmethod
        def today(cls):
            return c.utc.date()

    for name in _CLOCK_MODULES:
        mod = importlib.import_module(name)
        if getattr(mod, "datetime", None) is _real_datetime: monkeypatch.setattr(mod, "datetime", FakeDateTime)
        if getattr(mod, "date", None) is _real_date: monkeypatch.setattr(mod, "date", FakeDate)
    return c


def _business(repo: MunshiRepository) -> None:
    repo.set_setting("business_name", "Reconciliation Traders")
    repo.upsert_product(Product("UREA", "Urea 50kg", 3850, cost_price=3600))
    repo.upsert_product(Product("DAP", "DAP 50kg", 6250, cost_price=5900))
    repo.upsert_warehouse(Warehouse("WH-A", "Main godown")); repo.upsert_warehouse(Warehouse("WH-B", "Branch godown"))
    repo.upsert_customer(Customer("C1", "Malik Agro", "0300-1", credit_limit=2_000_000, route_id="R-A"))
    repo.upsert_customer(Customer("C2", "Chaudhry Farms", "0300-2", credit_limit=2_000_000, route_id="R-A", discount_pct=2.5))
    repo.upsert_customer(Customer("C3", "Green Valley", "0300-3", credit_limit=1_000_000, route_id="R-A"))
    repo.upsert_supplier(Supplier("S1", "Fauji depot")); repo.upsert_supplier(Supplier("S2", "Engro depot"))
    repo.upsert_route(Route("R-A", "Route A", "WH-A", ["C1", "C2", "C3"])); repo.upsert_route(Route("R-B", "Route B", "WH-B", ["C3"]))
    repo.upsert_vehicle(Vehicle("V1", "MNK-1", "large", 500))


def _load(repo, route, orders_lines, wh="WH-A"):
    """Book, confirm, allocate, plan and load (approve) one order per (customer, lines). Returns stops by customer."""
    oids = []
    for cust, lines in orders_lines:
        o = repo.create_order(cust, lines, "app", "", "office")
        repo.confirm_order(o.order_id, "office", "clerk"); repo.allocate_order(o.order_id, wh, "godown", "clerk")
        oids.append(o.order_id)
    p = repo.create_dispatch_plan("2026-09-01", route, "V1", oids, "godown")
    repo.approve_dispatch_plan(p.plan_id, "godown", "owner")
    return p, {s.customer_id: s for s in repo.list_stops(p.plan_id)}


def test_a_month_of_books_reconciles_to_the_paisa(clock, capsys):
    repo = MunshiRepository()
    clock.at(1, 9)
    _business(repo)
    # ------------------------------------------------ Sep 1: opening position
    repo.set_stock("WH-A", "UREA", 100)                        # 100 x 3,600.00 = 360,000.00  -> UREA pool 36,000,000 p
    repo.set_stock("WH-A", "DAP", 50)                          # 50 x 5,900.00  = 295,000.00  -> DAP pool 29,500,000 p
    repo.opening_balance("C1", 50_000, "owner")
    repo.supplier_opening_balance("S1", 200_000, "owner")
    # ------------------------------------------------ Sep 2: purchase, part paid in cash
    clock.at(2, 10)
    repo.record_purchase("S1", "WH-A", [{"sku": "UREA", "qty": 200, "unit_cost": 3700.55}], "FF-881", 300_000, "khareed")
    #   200 x 3,700.55 = 740,110.00;  UREA pool 36,000,000 + 74,011,000 = 110,011,000 p for 300 (avg 3,667.0333...)
    # ------------------------------------------------ Sep 3: two deliveries on credit + cash
    clock.at(3, 11)
    plan1, stops = _load(repo, "R-A", [("C1", [{"sku": "UREA", "qty": 30}]),
                                       ("C2", [{"sku": "UREA", "qty": 40}, {"sku": "DAP", "qty": 10}])])
    #   UREA out 30: 110,011,000 x 30/300 = 11,001,100 -> pool 99,009,900 / 270
    #   UREA out 40: 99,009,900 x 40/270 = 14,668,133.33 -> 14,668,133 -> pool 84,341,767 / 230
    #   DAP out 10: 29,500,000 x 10/50 = 5,900,000 -> pool 23,600,000 / 40
    clock.at(3, 15)
    s1 = stops["C1"]; r1 = repo.close_stop(s1.stop_id, [{"sku": "UREA", "qty": 30}], [], 50_000, s1.otp, "driver")
    s2 = stops["C2"]; r2 = repo.close_stop(s2.stop_id, [{"sku": "UREA", "qty": 38}, {"sku": "DAP", "qty": 10}], [{"sku": "UREA", "qty": 2}], 0, s2.otp, "driver")
    #   C1 invoice 30 x 3,850 = 115,500.00.  C2 (2.5% off): UREA 3,753.75 x 38 = 142,642.50 + DAP 6,093.75 x 10 = 60,937.50 -> 203,580.00
    #   C2's 2 returned UREA come back at their load cost: 14,668,133 x 2/40 = 733,406.65 -> 733,407; delivered 38 cost 13,934,726
    #   UREA pool 84,341,767 + 733,407 = 85,075,174 / 232
    assert (r1["invoiced"], r2["invoiced"], r2["status"]) == (115_500.0, 203_580.0, "short")
    clock.at(3, 18)
    dep = repo.record_deposit(plan1.plan_id, 49_500, "cashier", "hisaab")
    assert (dep["expected"], dep["variance"]) == (50_000.0, -500.0)
    sep3 = repo.sales_report("2026-09-03", "2026-09-03")
    #   revenue 319,080.00; COGS 11,001,100 + 13,934,726 + 5,900,000 = 30,835,826 p = 308,358.26; margin 10,721.74
    assert (sep3["revenue"], sep3["cost_of_goods"], sep3["gross_margin"]) == (319_080.0, 308_358.26, 10_721.74)
    # ------------------------------------------------ Sep 5: cost price edited, and a dearer purchase
    clock.at(5, 10)
    urea = repo.get_product("UREA"); urea.cost_price = 9_999; repo.upsert_product(urea)       # a careless edit
    repo.record_purchase("S2", "WH-A", [{"sku": "UREA", "qty": 100, "unit_cost": 3800}], "EN-12", 0, "khareed")
    #   UREA pool 85,075,174 + 38,000,000 = 123,075,174 / 332
    assert repo.sales_report("2026-09-03", "2026-09-03") == sep3          # a past sale's margin never moves
    # ------------------------------------------------ Sep 8: cheque;  Sep 10: mis-keyed expense;  Sep 12: cheque bounces
    clock.at(8, 12); chq = repo.record_payment("C2", 100_000, "cheque", "HBL 0042", "hisaab", "clerk")
    clock.at(10, 9); wrong = repo.record_expense("fuel", 55_000, "diesel", "cash", "Bilal", "hisaab")
    clock.at(10, 9, 30); repo.reverse_expense(wrong.expense_id, "typed 55,000 for 5,500", "owner", "owner")
    repo.record_expense("fuel", 5_500, "diesel", "cash", "Bilal", "hisaab")
    clock.at(12, 16); bounced = repo.reverse_ledger_entry(chq.entry_id, "cheque bounced: insufficient funds", "owner", "owner")
    # ------------------------------------------------ Sep 14: supplier paid by bank;  Sep 15: DAP sale with paisa-level cash
    clock.at(14, 11); repo.pay_supplier("S1", 150_000, "bank", "IBFT 77", "khareed", "owner")
    clock.at(15, 10)
    plan2, stops = _load(repo, "R-A", [("C3", [{"sku": "DAP", "qty": 20}])])          # DAP out 20: 23,600,000 x 20/40 = 11,800,000
    s3 = stops["C3"]; repo.close_stop(s3.stop_id, [{"sku": "DAP", "qty": 20}], [], 25_000.25, s3.otp, "driver")    # invoice 125,000.00
    clock.at(15, 19); assert repo.record_deposit(plan2.plan_id, 25_000.25, "cashier", "hisaab")["variance"] == 0.0
    # ------------------------------------------------ Sep 18: a mis-keyed purchase, reversed the same day
    clock.at(18, 10)
    bad = repo.record_purchase("S2", "WH-A", [{"sku": "DAP", "qty": 10, "unit_cost": 6100}], "EN-13?", 10_000, "khareed")
    clock.at(18, 10, 20); repo.reverse_purchase(bad.purchase_id, "wrong supplier bill", "owner", "owner")
    #   DAP pool 11,800,000 + 6,100,000 - 6,100,000 = 11,800,000 / 20;  S2 net 0 from it;  cash 10,000 out and back
    # ------------------------------------------------ Sep 20: damage write-off; Sep 22: transfer; Sep 25: counter cash; Sep 28: branch sale
    clock.at(20, 10); repo.adjust_stock("WH-A", "UREA", -3, "torn bags", "owner", "owner")   # 123,075,174 x 3/332 = 1,112,125.07 -> 1,112,125
    clock.at(22, 10); repo.transfer_stock("WH-A", "WH-B", "UREA", 20, "godown", "clerk")     # units move, value stays in the pool
    clock.at(25, 17); repo.record_payment("C1", 20_000, "cash", "", "hisaab", "clerk", received_by="office")
    clock.at(28, 9)
    plan3, stops = _load(repo, "R-B", [("C3", [{"sku": "UREA", "qty": 10}])], wh="WH-B")      # 121,963,049 x 10/329 = 3,707,083.56 -> 3,707,084
    s4 = stops["C3"]; repo.close_stop(s4.stop_id, [{"sku": "UREA", "qty": 10}], [], 0, s4.otp, "driver")    # invoice 38,500.00
    clock.at(28, 12); repo.add_ledger("C1", "credit_note", -5_000, "2 bags damaged in transit", None, "owner", "owner", "adjustment")
    with pytest.raises(InsufficientStockError):                                               # and nobody can oversell the branch
        repo.adjust_stock("WH-B", "UREA", -11, "count", "owner", "owner")

    # ================================================ reconcile
    customers = ["C1", "C2", "C3"]
    # receivables:  C1 50,000 + 115,500 - 50,000 - 20,000 - 5,000 = 90,500
    #               C2 203,580 - 100,000 + 100,000 (bounced) = 203,580;   C3 125,000 - 25,000.25 + 38,500 = 138,499.75
    balances = {c: repo.outstanding(c) for c in customers}
    assert balances == {"C1": 90_500.0, "C2": 203_580.0, "C3": 138_499.75}
    assert repo.receivables_paisa() == sum(to_paisa(b) for b in balances.values()) == 43_257_975
    aging = repo.aging_summary()
    assert aging["total"] == 432_579.75 and aging["total"] == to_rupees(repo.receivables_paisa())
    # payables:  S1 200,000 + 740,110 - 300,000 - 150,000 = 490,110;   S2 380,000 + (61,000 - 10,000 - 61,000 + 10,000) = 380,000
    assert {s: repo.supplier_balance(s) for s in ("S1", "S2")} == {"S1": 490_110.0, "S2": 380_000.0}
    assert repo.payables_paisa() == to_paisa(490_110) + to_paisa(380_000) == sum(to_paisa(p["balance"]) for p in repo.payables())
    # cashbook, day by day, against the cash that physically moved:
    #   in:  C1 counter 20,000;  hand-ins 49,500 + 25,000.25;   out: S1 at purchase 300,000, S2 10,000 - 10,000, fuel 55,000 - 55,000 + 5,500
    days = [(_real_date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(30)]
    books = [repo.cashbook(d) for d in days]
    tot = {k: sum(to_paisa(b[k]) for b in books) for k in ("total_in", "total_handins", "total_out", "net")}
    assert tot == {"total_in": 2_000_000, "total_handins": 7_450_025, "total_out": 30_550_000, "net": 2_000_000 + 7_450_025 - 30_550_000}
    assert all(to_paisa(b["net"]) == to_paisa(b["total_in"]) + to_paisa(b["total_handins"]) - to_paisa(b["total_out"]) for b in books)
    assert {b["date"]: b["total_out"] for b in books if b["cash_out"]} == {"2026-09-02": 300_000.0, "2026-09-10": 5_500.0, "2026-09-18": 0.0}
    # stock: the ledger replays to the stock table, and nothing is negative
    replay = repo.replay_stock_ledger()
    levels = {(s.warehouse_id, s.sku): s.on_hand for s in repo.list_stock()}
    assert replay == levels == {("WH-A", "UREA"): 309, ("WH-B", "UREA"): 10, ("WH-A", "DAP"): 20}
    # valuation = units x moving average: UREA pool 121,963,049 - 3,707,084 = 118,255,965 p (319 units); DAP 11,800,000 p (20 units)
    val = repo.stock_valuation()
    assert val["at_cost"] == 1_182_559.65 + 118_000.0 == 1_300_559.65
    rows = {(r["warehouse_id"], r["sku"]): r["at_cost"] for r in val["rows"]}
    assert rows == {("WH-A", "UREA"): to_rupees(mul_div(118_255_965, 309, 319)), ("WH-B", "UREA"): to_rupees(118_255_965 - mul_div(118_255_965, 309, 319)),
                    ("WH-A", "DAP"): 118_000.0}
    # value is conserved: opening + purchases - purchase return - cost of goods sold - write-off = closing, to the paisa
    opening, purchases, returned_purchase, write_off = 65_500_000, 74_011_000 + 38_000_000 + 6_100_000, 6_100_000, 1_112_125
    cogs = 11_001_100 + 13_934_726 + 5_900_000 + 11_800_000 + 3_707_084
    assert repo.cost_of_goods_sold_paisa() == cogs == 46_342_910
    assert opening + purchases - returned_purchase - cogs - write_off == to_paisa(val["at_cost"]) == 130_055_965
    assert repo._one("SELECT SUM(value_paisa) v FROM stock_moves")["v"] == to_paisa(val["at_cost"])     # the value ledger replays too
    # profit for the month: invoiced 115,500 + 203,580 + 125,000 + 38,500 = 482,580.00 (opening balance is not a sale),
    # less the Sep 28 credit note 5,000 = revenue 477,580.00.  COGS 463,429.10 is untouched (the damaged bags weren't
    # returned to stock), so margin 477,580 - 463,429.10 = 14,150.90.  Expenses: fuel 55,000 - 55,000 + 5,500, plus the
    # Sep 3 driver shortfall (collected 50,000, handed in 49,500) booked as a 500 cash_shortage = 6,000;
    # net 14,150.90 - 6,000 = 8,150.90.  (The shortfall is not a cash-out: the cashbook totals above are unchanged.)
    month = repo.profit_summary("2026-09-01", "2026-09-30")
    assert (month["revenue"], month["credit_notes"], month["cost_of_goods"], month["gross_margin"], month["expenses"], month["net"]) == \
           (477_580.0, 5_000.0, 463_429.10, 14_150.90, 6_000.0, 8_150.90)
    assert month["expenses_by_category"] == {"fuel": 5_500.0, "cash_shortage": 500.0}
    assert repo.cashbook("2026-09-03")["total_shortfall"] == 500.0
    assert repo.collection_report("2026-09-01", "2026-09-30")["credit_notes"] == month["credit_notes"]
    assert repo.sales_report("2026-09-03", "2026-09-03") == sep3
    # gapless, per series, in the order the documents were raised
    ids = lambda kind: [r["entry_id"] for r in repo._all("SELECT entry_id FROM ledger WHERE doc_no IS NOT NULL AND entry_id LIKE ? ORDER BY rowid", (kind + "-%",))]  # noqa: E731
    assert ids("INV") == [f"INV-2026-{n:06d}" for n in range(1, 5)]
    assert ids("RCP") == [f"RCP-2026-{n:06d}" for n in range(1, 5)]
    assert ids("REV") == [bounced.entry_id] == ["REV-2026-000001"] and ids("CRN") == ["CRN-2026-000001"] and ids("OPB") == ["OPB-2026-000001"]
    assert [p.purchase_id for p in reversed(repo.list_purchases())] == ["PUR-2026-000001", "PUR-2026-000002", "PUR-2026-000003", "PRN-2026-000001"]
    # history is intact: every original is still there, next to its reversal
    assert repo.get_ledger_entry(chq.entry_id).amount == -100_000.0 and repo.get_expense(wrong.expense_id).amount == 55_000.0

    with capsys.disabled():
        print("\n  month reconciliation (Sep 2026):"
              f"\n    receivables {repo.receivables_paisa() / 100:,.2f} = C1 {balances['C1']:,.2f} + C2 {balances['C2']:,.2f} + C3 {balances['C3']:,.2f}"
              f"\n    payables    {repo.payables_paisa() / 100:,.2f} = S1 490,110.00 + S2 380,000.00"
              f"\n    cash        in {tot['total_in'] / 100:,.2f} + hand-ins {tot['total_handins'] / 100:,.2f} - out {tot['total_out'] / 100:,.2f} = {tot['net'] / 100:,.2f}"
              f"\n    stock       {levels}; at cost {val['at_cost']:,.2f} (moving average)"
              f"\n    P&L         revenue {month['revenue']:,.2f} - COGS {month['cost_of_goods']:,.2f} = {month['gross_margin']:,.2f}; - expenses {month['expenses']:,.2f} = net {month['net']:,.2f}"
              f"\n    Sep 3 margin {sep3['gross_margin']:,.2f} before and after the cost edit + dearer purchase")


# ==================================================================== concurrency: gapless under real threads
def _fire(n, fn):
    gate = threading.Barrier(n)
    out, errs, lock = [], [], threading.Lock()

    def worker(i):
        gate.wait()
        try:
            r = fn(i)
            with lock: out.append(r)
        except Exception as e:      # noqa: BLE001 — collected and asserted on
            with lock: errs.append(e)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in ts: t.start()
    for t in ts: t.join(60)
    return out, errs


def test_concurrent_documents_on_two_connections_never_collide_or_skip(tmp_path):
    from munshi.domain.seed import seed
    path = str(tmp_path / "biz.db")
    a = seed(MunshiRepository(path)); b = MunshiRepository(path)      # two connections to one file, as two workers would have
    year = to_business_date(now_iso()).year
    # 8 stops to close at once: every close raises an invoice and a receipt
    oids = []
    for cid in ("C-001", "C-002", "C-003", "C-004", "C-005", "C-006", "C-007", "C-008"):
        o = a.create_order(cid, [{"sku": "UREA-50", "qty": 1}], "t", "", "o"); a.confirm_order(o.order_id, "o", "c"); a.allocate_order(o.order_id, "WH-MULTAN", "g")
        oids.append(o.order_id)
    plan = a.create_dispatch_plan("2026-09-25", "R-MULTAN-N", "V-02", oids, "g"); a.approve_dispatch_plan(plan.plan_id, "g", "c")
    stops = a.list_stops(plan.plan_id)

    def job(i):
        repo = a if i % 2 else b
        if i < 8:                                   # close a stop: INV + RCP in one transaction
            st = stops[i]
            return repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 1}], [], 100 + i, st.otp, "d")
        if i < 20:                                  # an office receipt
            return repo.record_payment("C-00" + str(1 + i % 9), 10 + i, "cash", "", "h")
        with immediate_tx(repo) as c:              # takes a number, then its transaction fails
            next_doc_no(repo, c, "receipt", now_iso()); next_doc_no(repo, c, "invoice", now_iso())
            raise RuntimeError("insert failed after numbering")

    results, errors = _fire(24, job)
    assert len(results) == 20 and len(errors) == 4 and all(isinstance(e, RuntimeError) for e in errors), errors
    inv = sorted(r["entry_id"] for r in a._all("SELECT entry_id FROM ledger WHERE kind='invoice' AND doc_no IS NOT NULL"))
    rcp = sorted(r["entry_id"] for r in a._all("SELECT entry_id FROM ledger WHERE kind='payment' AND doc_no IS NOT NULL"))
    assert inv == [f"INV-{year}-{n:06d}" for n in range(1, 9)]                 # 8 closes -> exactly 1..8
    assert rcp == [f"RCP-{year}-{n:06d}" for n in range(1, 21)]                # 8 driver + 12 office receipts -> exactly 1..20, none burnt
    assert a._one("SELECT last_value FROM document_counters WHERE kind='receipt'")["last_value"] == 20
    b.close(); a.close()


def test_concurrent_creates_on_one_connection_are_serialised(tmp_path):
    from munshi.domain.seed import seed
    repo = seed(MunshiRepository(str(tmp_path / "one.db")))
    year = to_business_date(now_iso()).year
    results, errors = _fire(16, lambda i: repo.record_purchase("S-001", "WH-MULTAN", [{"sku": "UREA-50", "qty": 1, "unit_cost": 3600}], "", 0, "k"))
    assert errors == [] and sorted(p.purchase_id for p in results) == [f"PUR-{year}-{n:06d}" for n in range(1, 17)]
    assert repo.get_stock("WH-MULTAN", "UREA-50").on_hand == 420 + 16


def test_concurrent_allocations_cannot_oversell(tmp_path):
    """Two clerks allocate the last units at the same moment, from two connections: one wins, one is refused."""
    from munshi.domain.seed import seed
    path = str(tmp_path / "race.db")
    a = seed(MunshiRepository(path)); b = MunshiRepository(path)
    orders = []
    for cid in ("C-001", "C-003", "C-005", "C-008"):
        o = a.create_order(cid, [{"sku": "CYPER-1L", "qty": 5}], "t", "", "o"); a.confirm_order(o.order_id, "o", "c"); orders.append(o.order_id)
    results, errors = _fire(4, lambda i: (a if i % 2 else b).allocate_order(orders[i], "WH-MULTAN", "g"))    # 8 on hand, 4 x 5 wanted
    assert len(results) == 1 and len(errors) == 3 and all(isinstance(e, InsufficientStockError) for e in errors), errors
    s = a.get_stock("WH-MULTAN", "CYPER-1L")
    assert (s.on_hand, s.reserved) == (8, 5)
    b.close(); a.close()
