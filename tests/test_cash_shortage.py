"""A driver's cash shortfall is booked, not just notified.

The accountant's reproduction: a customer pays Rs 50,000 at the door, the driver hands in Rs 45,000.
The customer's khata is right (he paid in full); the business lost Rs 5,000 and, before this fix, no
report showed it -- profit was overstated by exactly the shortfall, forever.

Now record_deposit books the gap as a `cash_shortage` expense (method "adjustment": the cash never
reached the drawer) dated the day of the deposit, carrying the plan, the vehicle and the suspect
stop(s) in its note. It is reversible like any expense, and a later hand-in on the same plan books a
recovery. Every figure below is worked by hand:

    20 bags x Rs 2,500 = Rs 50,000 invoice;  cost 20 x Rs 2,000 = Rs 40,000;  gross margin Rs 10,000
"""
from __future__ import annotations

import importlib
from datetime import UTC, timedelta, timezone
from datetime import date as _real_date
from datetime import datetime as _real_datetime

import pytest

from munshi.domain.models import Customer, Product, Route, Vehicle, Warehouse
from munshi.domain.repository import MunshiRepository
from munshi.domain.repository.cash import SHORTAGE_CATEGORY

PKT = timezone(timedelta(hours=5), "PKT")
DAY = "2026-09-25"
_CLOCK_MODULES = ("munshi.domain.models", "munshi.domain.repository.base", "munshi.domain.repository.cash",
                  "munshi.domain.repository.reports", "munshi.domain.repository.collections", "munshi.domain.repository.dispatch",
                  "munshi.domain.repository.orders", "munshi.domain.repository.master")


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """25 September 2026, 18:00 Karachi -- one fixed business day, no midnight flakiness."""
    utc = _real_datetime(2026, 9, 25, 18, 0, tzinfo=PKT).astimezone(UTC)

    class FakeDateTime(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            return utc.astimezone(tz) if tz else utc.replace(tzinfo=None)

    class FakeDate(_real_date):
        @classmethod
        def today(cls):
            return utc.date()

    for name in _CLOCK_MODULES:
        mod = importlib.import_module(name)
        if getattr(mod, "datetime", None) is _real_datetime: monkeypatch.setattr(mod, "datetime", FakeDateTime)
        if getattr(mod, "date", None) is _real_date: monkeypatch.setattr(mod, "date", FakeDate)


def _delivered(cash: float = 50_000, qty: int = 20, customers=("C1",)):
    """A fresh business; one loaded plan; each customer takes `qty` bags (Rs 50,000) and pays `cash` at the door."""
    repo = MunshiRepository()
    repo.upsert_product(Product("UREA", "Urea 50kg", 2_500, cost_price=2_000))
    repo.upsert_warehouse(Warehouse("WH-A", "Main godown"))
    for cid in customers:
        repo.upsert_customer(Customer(cid, f"Malik Agro {cid}", "0300-1", credit_limit=1_000_000, route_id="R-A"))
    repo.upsert_route(Route("R-A", "Route A", "WH-A", list(customers)))
    repo.upsert_vehicle(Vehicle("V1", "MNK-1", "large", 500))
    repo.set_stock("WH-A", "UREA", 100)
    oids = []
    for cid in customers:
        o = repo.create_order(cid, [{"sku": "UREA", "qty": qty}], "app", "", "office")
        repo.confirm_order(o.order_id, "office", "clerk"); repo.allocate_order(o.order_id, "WH-A", "godown", "clerk")
        oids.append(o.order_id)
    plan = repo.create_dispatch_plan(DAY, "R-A", "V1", oids, "godown")
    repo.approve_dispatch_plan(plan.plan_id, "godown", "owner")
    for s in repo.list_stops(plan.plan_id):
        repo.close_stop(s.stop_id, [{"sku": "UREA", "qty": qty}], [], cash, s.otp, "driver")
    return repo, plan


def _shortage_rows(repo):
    return [x for x in repo.expenses_between(DAY, DAY) if x.category == SHORTAGE_CATEGORY]


# ------------------------------------------------------------------ the accountant's scenario
def test_short_deposit_books_a_5000_cash_shortage_expense_with_the_attribution_note():
    repo, plan = _delivered(cash=50_000)
    stop = repo.list_stops(plan.plan_id)[0]
    r = repo.record_deposit(plan.plan_id, 45_000, "cashier", "hisaab")
    assert (r["expected"], r["counted"], r["variance"]) == (50_000.0, 45_000.0, -5_000.0)
    assert r["suspect_stops"][0]["stop_id"] == stop.stop_id                     # attribution unchanged
    (x,) = _shortage_rows(repo)
    assert (x.category, x.amount, x.expense_date, x.method, x.reversal_of) == ("cash_shortage", 5_000.0, DAY, "adjustment", None)
    assert x.note.startswith(f"{plan.plan_id} (MNK-1) cash short on DEP-")
    assert f"check {stop.stop_id} Malik Agro C1 Rs 50,000" in x.note           # same investigative context as the notification
    assert r["shortage_entry"] == {"expense_id": x.expense_id, "amount": 5_000.0, "kind": "shortage", "note": x.note}
    assert repo.get_expense(x.expense_id).amount == 5_000.0
    assert any(n["kind"] == "variance" and "Rs 5,000" in n["text"] for n in repo.notifications("owner"))   # owner still told
    audit = repo.audit_log(entity_id=x.expense_id)
    assert audit[0]["action"] == "cash_shortage" and audit[0]["payload"]["deposit"] and audit[0]["payload"]["amount"] == 5_000.0


def test_cashbook_shows_the_5000_shortfall_without_misstating_the_drawer():
    repo, plan = _delivered(cash=50_000)
    repo.record_deposit(plan.plan_id, 45_000, "cashier", "hisaab")
    cb = repo.cashbook(DAY)
    # the drawer really received 45,000 and nothing left it: net stays 45,000 (a cash-out here would say 40,000, wrongly)
    assert (cb["total_in"], cb["total_handins"], cb["total_out"], cb["net"]) == (0.0, 45_000.0, 0.0, 45_000.0)
    assert cb["cash_out"] == []
    # and the 5,000 that should have arrived is on the day's book as a loss
    assert cb["total_shortfall"] == 5_000.0
    assert [(s["kind"], s["amount"]) for s in cb["shortfalls"]] == [("driver shortfall", 5_000.0)]
    assert cb["shortfalls"][0]["who"].startswith(plan.plan_id)


def test_profit_summary_net_is_reduced_by_exactly_the_5000_shortfall():
    control, cplan = _delivered(cash=50_000)
    control.record_deposit(cplan.plan_id, 50_000, "cashier", "hisaab")          # what the books said before the fix, and still say when square
    base = control.profit_summary(DAY, DAY)
    assert (base["revenue"], base["cost_of_goods"], base["gross_margin"], base["expenses"], base["net"]) == (50_000.0, 40_000.0, 10_000.0, 0.0, 10_000.0)

    repo, plan = _delivered(cash=50_000)
    repo.record_deposit(plan.plan_id, 45_000, "cashier", "hisaab")
    p = repo.profit_summary(DAY, DAY)
    assert (p["revenue"], p["cost_of_goods"], p["gross_margin"]) == (50_000.0, 40_000.0, 10_000.0)   # sales untouched
    assert p["expenses"] == 5_000.0 and p["expenses_by_category"] == {"cash_shortage": 5_000.0}
    assert p["net"] == 5_000.0 == base["net"] - 5_000.0
    assert repo.digest(DAY)["cash"]["expenses"] == 5_000.0


def test_customer_khata_is_unaffected_by_the_drivers_shortfall():
    repo, plan = _delivered(cash=50_000)
    before = repo.outstanding("C1")
    assert before == 0.0                                                         # invoice 50,000 - payment 50,000
    repo.record_deposit(plan.plan_id, 45_000, "cashier", "hisaab")
    assert repo.outstanding("C1") == 0.0
    assert [(e.kind, e.amount) for e in repo.ledger_for("C1")] == [("invoice", 50_000.0), ("payment", -50_000.0)]
    assert repo.receivables_paisa() == 0


def test_reversing_the_shortage_restores_the_pre_shortfall_profit():
    repo, plan = _delivered(cash=50_000)
    r = repo.record_deposit(plan.plan_id, 45_000, "cashier", "hisaab")
    assert repo.profit_summary(DAY, DAY)["net"] == 5_000.0
    rev = repo.reverse_expense(r["shortage_entry"]["expense_id"], "recount found the 5,000 in the bag", "owner", "owner")
    assert (rev.category, rev.amount, rev.reversal_of, rev.method) == ("cash_shortage", -5_000.0, r["shortage_entry"]["expense_id"], "adjustment")
    p = repo.profit_summary(DAY, DAY)
    assert (p["expenses"], p["net"]) == (0.0, 10_000.0)
    cb = repo.cashbook(DAY)
    assert cb["total_shortfall"] == 0.0 and cb["net"] == 45_000.0 and cb["total_out"] == 0.0
    # a later hand-in on the plan does not re-book a loss the owner has cancelled, nor "recover" it into a gain
    later = repo.record_deposit(plan.plan_id, 1_000, "cashier", "hisaab")
    assert later["shortage_entry"] is None and repo.profit_summary(DAY, DAY)["net"] == 10_000.0


# ------------------------------------------------------------------ the non-shortfall path is unchanged
@pytest.mark.parametrize("counted, variance", [(50_000, 0.0), (50_500, 500.0)])
def test_exact_or_over_deposit_posts_nothing_extra(counted, variance):
    repo, plan = _delivered(cash=50_000)
    r = repo.record_deposit(plan.plan_id, counted, "cashier", "hisaab")
    assert (r["variance"], r["suspect_stops"], r["shortage_entry"]) == (variance, [], None)
    assert repo.expenses_between(DAY, DAY) == []
    assert repo.profit_summary(DAY, DAY)["net"] == 10_000.0
    cb = repo.cashbook(DAY)
    assert (cb["total_handins"], cb["net"], cb["shortfalls"], cb["total_shortfall"]) == (float(counted), float(counted), [], 0.0)
    assert not any(n["kind"] == "variance" for n in repo.notifications("owner"))


# ------------------------------------------------------------------ hand-ins in instalments, and making it good
def test_second_instalment_recovers_the_booked_shortage_and_profit_is_whole_again():
    repo, plan = _delivered(cash=50_000)
    d1 = repo.record_deposit(plan.plan_id, 30_000, "cashier", "hisaab")
    assert d1["shortage_entry"]["amount"] == 20_000.0 and repo.profit_summary(DAY, DAY)["net"] == -10_000.0
    d2 = repo.record_deposit(plan.plan_id, 15_000, "cashier", "hisaab")         # still 5,000 short in total
    assert d2["variance"] == -5_000.0 and d2["shortage_entry"]["kind"] == "recovery" and d2["shortage_entry"]["amount"] == -15_000.0
    assert d2["shortage_entry"]["note"].startswith(f"{plan.plan_id} (MNK-1) shortage made good by DEP-")
    assert repo.profit_summary(DAY, DAY)["net"] == 5_000.0
    d3 = repo.record_deposit(plan.plan_id, 5_000, "cashier", "hisaab")          # the driver makes it good
    assert d3["variance"] == 0.0 and d3["shortage_entry"]["amount"] == -5_000.0
    assert [x.amount for x in _shortage_rows(repo)] == [20_000.0, -15_000.0, -5_000.0]
    p = repo.profit_summary(DAY, DAY)
    assert (p["expenses"], p["net"]) == (0.0, 10_000.0)
    cb = repo.cashbook(DAY)
    assert (cb["total_handins"], cb["net"], cb["total_shortfall"]) == (50_000.0, 50_000.0, 0.0)
    assert [s["kind"] for s in cb["shortfalls"]] == ["driver shortfall", "driver shortfall recovered", "driver shortfall recovered"]


def test_shortages_on_two_plans_are_booked_and_recovered_independently():
    a, pa = _delivered(cash=50_000)
    # a second plan in the same business
    o = a.create_order("C1", [{"sku": "UREA", "qty": 10}], "app", "", "office")
    a.confirm_order(o.order_id, "office", "clerk"); a.allocate_order(o.order_id, "WH-A", "godown", "clerk")
    pb = a.create_dispatch_plan(DAY, "R-A", "V1", [o.order_id], "godown"); a.approve_dispatch_plan(pb.plan_id, "godown", "owner")
    sb = a.list_stops(pb.plan_id)[0]; a.close_stop(sb.stop_id, [{"sku": "UREA", "qty": 10}], [], 25_000, sb.otp, "driver")
    a.record_deposit(pa.plan_id, 45_000, "cashier", "hisaab")                    # plan A 5,000 short
    a.record_deposit(pb.plan_id, 24_000, "cashier", "hisaab")                    # plan B 1,000 short
    assert sorted(x.amount for x in _shortage_rows(a)) == [1_000.0, 5_000.0]
    r = a.record_deposit(pb.plan_id, 1_000, "cashier", "hisaab")                 # B made good: only B's 1,000 comes back
    assert r["shortage_entry"]["amount"] == -1_000.0
    assert a.profit_summary(DAY, DAY)["expenses"] == 5_000.0


def test_nobody_can_key_a_cash_shortage_by_hand():
    repo, _ = _delivered(cash=50_000)
    x = repo.record_expense("cash_shortage", 5_000, "fake", "cash", "Bilal", "hisaab")
    assert x.category == "misc"                                                  # coerced: the category is system-only
    from munshi.domain.repository import EXPENSE_CATEGORIES
    assert SHORTAGE_CATEGORY not in EXPENSE_CATEGORIES                           # so the web form's pattern refuses it too


def test_a_failed_deposit_books_no_shortage():
    repo, plan = _delivered(cash=50_000)
    with pytest.raises(ValueError):
        repo.record_deposit(plan.plan_id, -1, "cashier", "hisaab")
    assert repo.expenses_between(DAY, DAY) == [] and repo.deposits(plan.plan_id) == []
