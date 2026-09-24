"""Regression: "today" is the Pakistan business day (Asia/Karachi, UTC+5, no DST),
whatever timezone the server's OS runs in.

Timestamps are stored in UTC. Between 00:00 and 05:00 Karachi time the UTC date
is still *yesterday*, so any report that groups by the raw UTC date prefix, or
asks the host OS for "today", files the entry under the wrong day. Reviewers
reproduced it at 00:28 PKT: a payment taken moments earlier showed Rs 0 in the
cashbook for today.

Every test runs twice: on a UTC host (Docker's default, no TZ set) and on a
host whose OS clock is already in Pakistan time (a dev laptop in Multan).
The answer must be the same on both.
"""
from __future__ import annotations

import importlib
from datetime import UTC, timedelta, timezone
from datetime import date as _real_date
from datetime import datetime as _real_datetime

import pytest

PKT = timezone(timedelta(hours=5), "PKT")

# Modules that read the clock while creating or reporting on data.
_CLOCK_MODULES = (
    "munshi.domain.models",
    "munshi.domain.seed",
    "munshi.domain.repository.base",
    "munshi.domain.repository.cash",
    "munshi.domain.repository.reports",
    "munshi.domain.repository.collections",
    "munshi.domain.repository.dispatch",
    "munshi.domain.repository.orders",
)


class Clock:
    """A frozen wall clock that also knows the host OS's timezone."""

    def __init__(self, host_tz: timezone):
        self.host_tz = host_tz
        self.utc = _real_datetime(2000, 1, 1, tzinfo=UTC)

    def set_karachi(self, y, m, d, hh, mm):
        self.utc = _real_datetime(y, m, d, hh, mm, tzinfo=PKT).astimezone(UTC)


def _fake_classes(clock: Clock):
    class FakeDateTime(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:   # naive local wall clock of the host OS
                return clock.utc.astimezone(clock.host_tz).replace(tzinfo=None)
            return clock.utc.astimezone(tz)

        @classmethod
        def utcnow(cls):
            return clock.utc.replace(tzinfo=None)

        @classmethod
        def today(cls):
            return cls.now()

    class FakeDate(_real_date):
        @classmethod
        def today(cls):
            return clock.utc.astimezone(clock.host_tz).date()

    return FakeDateTime, FakeDate


HOSTS = {"utc-host": UTC, "pkt-host": PKT}


@pytest.fixture(params=sorted(HOSTS))
def clock(request, monkeypatch):
    c = Clock(HOSTS[request.param])
    fdt, fd = _fake_classes(c)
    for name in _CLOCK_MODULES:
        mod = importlib.import_module(name)
        if getattr(mod, "datetime", None) is _real_datetime: monkeypatch.setattr(mod, "datetime", fdt)
        if getattr(mod, "date", None) is _real_date: monkeypatch.setattr(mod, "date", fd)
    return c


@pytest.fixture
def repo(clock):
    from munshi.domain.seed import seeded_repository
    clock.set_karachi(2026, 9, 24, 12, 0)     # seed at midday the day before
    return seeded_repository()


def _deliver(repo, plan_date: str, cust="C-002", qty=10, cash=0.0):
    o = repo.create_order(cust, [{"sku": "UREA-50", "qty": qty}], "t", "", "order_munshi")
    repo.confirm_order(o.order_id, "order_munshi", "clerk"); repo.allocate_order(o.order_id, "WH-MULTAN", "godown_munshi")
    p = repo.create_dispatch_plan(plan_date, "R-MULTAN-N", "V-01", [o.order_id], "godown_munshi")
    repo.approve_dispatch_plan(p.plan_id, "godown_munshi", "clerk")
    st = repo.list_stops(p.plan_id)[0]
    return o, repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": qty}], [], cash, st.otp, "delivery_munshi"), p


# ------------------------------------------------------------------ the helper itself
@pytest.mark.parametrize("stamp, expected", [
    ("2026-09-24T18:59:59+00:00", "2026-09-24"),   # 23:59:59 PKT
    ("2026-09-24T19:00:00+00:00", "2026-09-25"),   # 00:00 PKT: new business day, UTC still on the 24th
    ("2026-09-24T19:28:00+00:00", "2026-09-25"),   # the reviewers' 00:28 PKT
    ("2026-09-25T18:45:00+00:00", "2026-09-25"),   # 23:45 PKT, same UTC date
    ("2026-09-25T00:15:00+00:00", "2026-09-25"),   # 05:15 PKT, UTC just rolled over
    ("2026-09-24T19:28:00Z", "2026-09-25"),
    ("2026-09-24T19:28:00", "2026-09-25"),         # naive stored stamps are UTC by convention
    ("2026-09-25T00:28:00+05:00", "2026-09-25"),   # already-local stamps are respected
])
def test_to_business_date_uses_karachi_day(stamp, expected):
    from munshi.domain.models import to_business_date
    assert to_business_date(stamp).isoformat() == expected
    assert to_business_date(_real_datetime.fromisoformat(stamp.replace("Z", "+00:00"))).isoformat() == expected


def test_today_iso_is_karachi_date_regardless_of_host(clock):
    from munshi.domain.models import business_today, now_iso, today_iso
    clock.set_karachi(2026, 9, 25, 0, 28)
    assert today_iso() == "2026-09-25" and business_today() == _real_date(2026, 9, 25)
    assert now_iso() == "2026-09-24T19:28:00+00:00"            # storage stays UTC
    clock.set_karachi(2026, 9, 25, 23, 45)
    assert today_iso() == "2026-09-25"
    clock.set_karachi(2026, 9, 26, 0, 1)
    assert today_iso() == "2026-09-26"


# ------------------------------------------------------------------ cashbook just after midnight
def test_cash_payment_after_midnight_karachi_is_in_todays_cashbook(repo, clock):
    clock.set_karachi(2026, 9, 25, 0, 28)
    pay = repo.record_payment("C-001", 5000, "cash", "", "hisaab_munshi", received_by="office")
    x = repo.record_expense("fuel", 3000, "diesel", "cash", "Bilal", "hisaab_munshi")
    sup = repo.pay_supplier("S-001", 7000, "cash", "", "owner")
    for cb in (repo.cashbook(), repo.cashbook("2026-09-25")):
        assert cb["date"] == "2026-09-25"
        assert [i["ref"] for i in cb["cash_in"]] == [pay.entry_id] and cb["total_in"] == 5000
        refs_out = {o["ref"] for o in cb["cash_out"]}
        assert x.expense_id in refs_out and sup.entry_id in refs_out
    assert x.expense_date == "2026-09-25"
    assert pay.entry_id not in {i["ref"] for i in repo.cashbook("2026-09-24")["cash_in"]}


def test_driver_handin_after_midnight_karachi_is_in_todays_cashbook(repo, clock):
    clock.set_karachi(2026, 9, 25, 0, 20)
    from munshi.domain.models import today_iso
    _, _, p = _deliver(repo, today_iso(), cash=12000)
    clock.set_karachi(2026, 9, 25, 0, 40)
    repo.record_deposit(p.plan_id, 12000, "cashier", "hisaab_munshi")
    cb = repo.cashbook()
    assert cb["date"] == "2026-09-25" and cb["total_handins"] == 12000
    assert repo.cashbook("2026-09-24")["total_handins"] == 0


def test_entries_either_side_of_karachi_midnight_land_on_their_own_day(repo, clock):
    clock.set_karachi(2026, 9, 24, 23, 45)       # 18:45 UTC on the 24th
    late = repo.record_payment("C-001", 1111, "cash", "", "h", received_by="office")
    clock.set_karachi(2026, 9, 25, 0, 28)        # 19:28 UTC, still the 24th in UTC
    early = repo.record_payment("C-001", 2222, "cash", "", "h", received_by="office")
    assert [i["ref"] for i in repo.cashbook("2026-09-24")["cash_in"]] == [late.entry_id]
    assert [i["ref"] for i in repo.cashbook("2026-09-25")["cash_in"]] == [early.entry_id]
    assert [e.entry_id for e in repo.ledger_between("2026-09-25", "2026-09-25", "payment")] == [early.entry_id]


def test_late_evening_karachi_is_not_pushed_to_tomorrow(repo, clock):
    """The other edge: 23:45 PKT is 18:45 UTC on the same date. Must stay 'today', not shift a day."""
    clock.set_karachi(2026, 9, 25, 23, 45)
    pay = repo.record_payment("C-001", 5000, "cash", "", "h", received_by="office")
    cb = repo.cashbook()
    assert cb["date"] == "2026-09-25" and [i["ref"] for i in cb["cash_in"]] == [pay.entry_id]
    assert repo.cashbook("2026-09-26")["total_in"] == 0
    clock.set_karachi(2026, 9, 26, 5, 15)        # 00:15 UTC on the 26th: UTC and PKT agree on the date again
    pay2 = repo.record_payment("C-001", 700, "cash", "", "h", received_by="office")
    assert [i["ref"] for i in repo.cashbook()["cash_in"]] == [pay2.entry_id]


# ------------------------------------------------------------------ sales just after midnight
def test_sale_after_midnight_karachi_is_in_todays_sales_report(repo, clock):
    clock.set_karachi(2026, 9, 25, 0, 28)
    from munshi.domain.models import today_iso
    today = today_iso()
    o, _, _ = _deliver(repo, today, qty=10, cash=1000)
    sales = repo.sales_report(today, today)
    assert sales["revenue"] == 38500 and sales["invoices"] == 1
    assert sales["by_day"] == [{"date": "2026-09-25", "revenue": 38500}]
    assert sales["by_product"][0]["qty"] == 10 and sales["cost_of_goods"] == 36000
    assert repo.sales_report("2026-09-24", "2026-09-24")["revenue"] == 0
    col = repo.collection_report(today, today)
    assert col["invoiced"] == 38500 and col["collected"] == 1000
    assert repo.profit_summary(today, today)["gross_margin"] == 2500
    top = {r["customer_id"]: r["revenue"] for r in repo.top_customers(30, limit=50)}
    assert top.get("C-002", 0) >= 38500
    assert all(s["sku"] != "UREA-50" for s in repo.slow_stock(30))


def test_digest_after_midnight_karachi_counts_todays_activity(repo, clock):
    clock.set_karachi(2026, 9, 25, 0, 28)
    from munshi.domain.models import today_iso
    o, _, _ = _deliver(repo, today_iso(), qty=10, cash=1000)
    repo.record_payment("C-001", 5000, "cash", "", "h", received_by="office")
    d = repo.digest()
    assert d["date"] == "2026-09-25"
    assert d["orders"]["count"] == 1 and d["orders"]["value"] == 38500
    assert d["dispatch"]["plans"] == 1 and d["dispatch"]["delivered"] == 1
    assert d["sales"]["invoiced"] == 38500 and d["cash"]["office_payments"] == 5000


def test_digest_late_evening_karachi(repo, clock):
    clock.set_karachi(2026, 9, 25, 23, 45)
    from munshi.domain.models import today_iso
    _deliver(repo, today_iso(), qty=10)
    d = repo.digest()
    assert d["date"] == "2026-09-25" and d["orders"]["count"] == 1 and d["sales"]["invoiced"] == 38500


# ------------------------------------------------------------------ aging across the boundary
def test_aging_uses_karachi_day_after_midnight(repo, clock):
    clock.set_karachi(2026, 9, 25, 0, 28)
    # a customer with no seed ledger, so FIFO is unambiguous
    cust = next(c.customer_id for c in repo.list_customers() if not repo.ledger_for(c.customer_id))
    repo.add_ledger(cust, "invoice", 1000, "x", "2026-09-24", "h")      # was due yesterday (Karachi)
    row = repo.aging(customer_id=cust)[0]
    assert row["days_overdue"] == 1 and row["bucket"] == "1-30"


def test_opening_balance_is_dated_karachi_today(repo, clock):
    clock.set_karachi(2026, 9, 25, 0, 28)
    cust = next(c.customer_id for c in repo.list_customers() if not repo.ledger_for(c.customer_id))
    e = repo.opening_balance(cust, 5000, "h")
    assert e.due_date == "2026-09-25"
    row = repo.aging(customer_id=cust)[0]
    assert row["bucket"] == "current" and row["days_overdue"] == 0
    # an opening balance is not a sale, on either side of midnight
    assert repo.sales_report("2026-09-24", "2026-09-25")["revenue"] == 0
    clock.set_karachi(2026, 9, 26, 0, 10)
    assert repo.aging(customer_id=cust)[0]["days_overdue"] == 1
