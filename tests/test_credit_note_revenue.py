"""Regression: a credit note reduces reported revenue, gross margin and net profit.

Before the fix, sales_report / profit_summary summed only kind='invoice' khata entries: a credit note
("5,000 for damaged bags") correctly cut the customer's balance but never touched revenue, so the owner's
P&L was overstated by every credit note ever issued. Reproduced in the accounting review: 384,000 of
invoiced sales still read 384,000 after a 5,000 credit note; it must read 379,000.

Treatment (see ReportsMixin._sales_paisa): a credit note is a sales allowance. It is a revenue reduction
only, in the period it is POSTED (not the invoice's period), and cost of goods sold is unchanged because
no goods came back to stock. Every expected figure is worked by hand in the comments.
"""
from __future__ import annotations

import importlib
from datetime import UTC, timedelta, timezone
from datetime import date as _real_date
from datetime import datetime as _real_datetime

import pytest

from munshi.domain.models import Customer, Product, Route, Vehicle, Warehouse
from munshi.domain.repository import MunshiRepository
from munshi.domain.repository.base import StateError

PKT = timezone(timedelta(hours=5), "PKT")
_CLOCK_MODULES = ("munshi.domain.models", "munshi.domain.seed", "munshi.domain.repository.base", "munshi.domain.repository.cash",
                  "munshi.domain.repository.reports", "munshi.domain.repository.collections", "munshi.domain.repository.dispatch",
                  "munshi.domain.repository.orders", "munshi.domain.repository.master")


class Clock:
    utc = _real_datetime(2026, 9, 1, tzinfo=UTC)

    def at(self, month: int, day: int, hh: int, mm: int = 0) -> None:
        """Set the wall clock to <day>/<month>/2026, hh:mm Karachi time."""
        self.utc = _real_datetime(2026, month, day, hh, mm, tzinfo=PKT).astimezone(UTC)


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


@pytest.fixture
def repo(clock):
    clock.at(9, 1, 9)
    r = MunshiRepository()
    r.upsert_product(Product("UREA", "Urea 50kg", 3840, cost_price=3600))
    r.upsert_warehouse(Warehouse("WH-A", "Main godown"))
    r.upsert_customer(Customer("C1", "Malik Agro", "0300-1", credit_limit=2_000_000, route_id="R-A"))
    r.upsert_customer(Customer("C2", "Chaudhry Farms", "0300-2", credit_limit=2_000_000, route_id="R-A"))
    r.upsert_route(Route("R-A", "Route A", "WH-A", ["C1", "C2"]))
    r.upsert_vehicle(Vehicle("V1", "MNK-1", "large", 500))
    r.set_stock("WH-A", "UREA", 200)                     # 200 x 3,600.00 at cost
    yield r
    r.close()


def _deliver(repo, cust: str, qty: int, day: str) -> dict:
    """Book, confirm, allocate, load and deliver one UREA order in full; returns close_stop's result."""
    o = repo.create_order(cust, [{"sku": "UREA", "qty": qty}], "app", "", "office")
    repo.confirm_order(o.order_id, "office", "clerk"); repo.allocate_order(o.order_id, "WH-A", "godown", "clerk")
    p = repo.create_dispatch_plan(day, "R-A", "V1", [o.order_id], "godown")
    repo.approve_dispatch_plan(p.plan_id, "godown", "owner")
    s = repo.list_stops(p.plan_id)[0]
    return repo.close_stop(s.stop_id, [{"sku": "UREA", "qty": qty}], [], 0, s.otp, "driver")


def _credit_note(repo, cust: str, amount: float, reason: str):
    # exactly what OperationsAPI.credit_note posts
    return repo.add_ledger(cust, "credit_note", -abs(amount), reason, None, "hisaab_munshi", "owner", "adjustment")


def test_credit_note_reduces_revenue_gross_margin_and_net(repo, clock):
    clock.at(9, 10, 11)
    assert _deliver(repo, "C1", 100, "2026-09-10")["invoiced"] == 384_000.0        # 100 x 3,840.00
    repo.record_expense("fuel", 4_000, "diesel", "cash", "Bilal", "hisaab")
    d = "2026-09-10"
    before_s, before_p = repo.sales_report(d, d), repo.profit_summary(d, d)
    #   revenue 384,000;  COGS 100 x 3,600 = 360,000;  margin 24,000;  net 24,000 - 4,000 = 20,000
    assert (before_s["revenue"], before_s["cost_of_goods"], before_s["gross_margin"]) == (384_000.0, 360_000.0, 24_000.0)
    assert (before_p["revenue"], before_p["gross_margin"], before_p["net"]) == (384_000.0, 24_000.0, 20_000.0)

    clock.at(9, 10, 16)
    _credit_note(repo, "C1", 5_000, "damaged bags")
    s, p = repo.sales_report(d, d), repo.profit_summary(d, d)
    #   revenue 384,000 - 5,000 = 379,000;  COGS unchanged 360,000;  margin 19,000;  net 15,000
    assert (s["revenue"], s["gross_invoiced"], s["credit_notes"]) == (379_000.0, 384_000.0, 5_000.0)
    assert (s["cost_of_goods"], s["gross_margin"], s["margin_pct"]) == (360_000.0, 19_000.0, 5.0)     # 19,000 / 379,000 = 5.01%
    assert (p["revenue"], p["credit_notes"], p["cost_of_goods"], p["gross_margin"], p["expenses"], p["net"]) == \
           (379_000.0, 5_000.0, 360_000.0, 19_000.0, 4_000.0, 15_000.0)
    assert s["invoices"] == 1                                                      # a credit note is not an invoice
    # netted into the day and the customer it was posted against; by_day always adds up to revenue
    assert s["by_day"] == [{"date": d, "revenue": 379_000.0}]
    assert [(r["customer_id"], r["revenue"]) for r in s["by_customer"]] == [("C1", 379_000.0)]
    # product mix and booker stay gross: a credit note carries no SKU and no order
    assert [(r["sku"], r["qty"], r["revenue"], r["cost"]) for r in s["by_product"]] == [("UREA", 100, 384_000.0, 360_000.0)]
    assert [(r["name"], r["revenue"]) for r in s["by_booker"]] == [("office", 384_000.0)]
    # one definition of "credit notes this period" across reports, and the khata agrees
    assert repo.collection_report(d, d)["credit_notes"] == s["credit_notes"] == 5_000.0
    assert repo.outstanding("C1") == 379_000.0


def test_credit_note_counts_in_the_period_it_is_posted_not_the_invoices(repo, clock):
    clock.at(8, 20, 11)
    _deliver(repo, "C1", 100, "2026-08-20")                                        # August invoice 384,000
    aug = repo.profit_summary("2026-08-01", "2026-08-31")
    assert (aug["revenue"], aug["gross_margin"]) == (384_000.0, 24_000.0)
    clock.at(9, 1, 0, 30)                                                          # 00:30 PKT Sep 1 = 19:30 UTC Aug 31
    _credit_note(repo, "C1", 5_000, "damaged bags from the August load")
    # August is closed: the credit note is not retroactive
    assert repo.profit_summary("2026-08-01", "2026-08-31") == aug
    # September carries it: no sales, revenue -5,000, margin -5,000, and no nonsense margin % on negative revenue
    sep_s, sep_p = repo.sales_report("2026-09-01", "2026-09-30"), repo.profit_summary("2026-09-01", "2026-09-30")
    assert (sep_s["revenue"], sep_s["gross_invoiced"], sep_s["credit_notes"], sep_s["gross_margin"], sep_s["margin_pct"]) == \
           (-5_000.0, 0.0, 5_000.0, -5_000.0, 0.0)
    assert sep_s["by_day"] == [{"date": "2026-09-01", "revenue": -5_000.0}]         # the Karachi day, not the UTC one
    assert (sep_p["revenue"], sep_p["cost_of_goods"], sep_p["gross_margin"], sep_p["net"]) == (-5_000.0, 0.0, -5_000.0, -5_000.0)
    # across both months it is the full story: 384,000 - 5,000
    both = repo.profit_summary("2026-08-01", "2026-09-30")
    assert (both["revenue"], both["gross_margin"]) == (379_000.0, 19_000.0)


def test_reversing_a_credit_note_restores_revenue(repo, clock):
    clock.at(9, 10, 11)
    _deliver(repo, "C1", 100, "2026-09-10")
    _deliver(repo, "C2", 50, "2026-09-10")                                         # 50 x 3,840 = 192,000; cost 180,000
    clock.at(9, 10, 16)
    crn = _credit_note(repo, "C1", 5_000, "damaged bags")
    assert repo.profit_summary("2026-09-10", "2026-09-10")["revenue"] == 571_000.0  # 384,000 + 192,000 - 5,000
    clock.at(9, 12, 10)
    rev = repo.reverse_ledger_entry(crn.entry_id, "credit note keyed to the wrong customer", "owner", "owner")
    assert (rev.kind, rev.amount, rev.reversal_of) == ("credit_note", 5_000.0, crn.entry_id)
    # the original day is history and keeps its credit note; the reversal lands on the day it is posted
    assert repo.sales_report("2026-09-10", "2026-09-10")["revenue"] == 571_000.0
    day12 = repo.sales_report("2026-09-12", "2026-09-12")
    assert (day12["revenue"], day12["credit_notes"], day12["by_day"]) == (5_000.0, -5_000.0, [{"date": "2026-09-12", "revenue": 5_000.0}])
    # over the month the pair nets to zero: revenue, margin and net are as if it never happened
    month_s, month_p = repo.sales_report("2026-09-01", "2026-09-30"), repo.profit_summary("2026-09-01", "2026-09-30")
    assert (month_s["revenue"], month_s["credit_notes"], month_s["gross_margin"]) == (576_000.0, 0.0, 36_000.0)
    assert (month_p["revenue"], month_p["gross_margin"], month_p["net"]) == (576_000.0, 36_000.0, 36_000.0)
    assert {r["customer_id"]: r["revenue"] for r in month_s["by_customer"]} == {"C1": 384_000.0, "C2": 192_000.0}
    assert repo.collection_report("2026-09-01", "2026-09-30")["credit_notes"] == 0.0
    assert repo.outstanding("C1") == 384_000.0
    with pytest.raises(StateError):                                                # a reversal is final
        repo.reverse_ledger_entry(rev.entry_id, "undo the undo", "owner", "owner")
