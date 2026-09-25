"""Every screen agrees on what "aaj" is, and a reversal corrects the customer exactly once.

Persona round 2 ran all its simulated days in one night. Between 00:00 and 05:00 Karachi time
"aaj ke orders" said NONE right after six drafts were made (A5): the orders tool compared the UTC date
prefix of created_at with the Pakistan date, while the cashbook and digest used the business day. The
demo seed also dated "today's" expenses with the host OS date, a day behind on a UTC server.

Here the clock is frozen at 00:30 PKT on 26 Sep (19:30 UTC on the 25th) and at 23:30 PKT (18:30 UTC, the
same date), on a UTC host and on a PKT host; the orders list, the orders filter by days/sku/customer, the
digest, the cashbook, the collection report, expenses and sales must all file the activity under 26 Sep.

A bounced-cheque reversal sent the customer TWO corrections: the repository queued one and the chat tool
queued a second on top of it (even when the customer had never been messaged)."""
from __future__ import annotations

import importlib
from datetime import UTC, timedelta, timezone
from datetime import date as _real_date
from datetime import datetime as _real_datetime

import pytest

PKT = timezone(timedelta(hours=5), "PKT")
_CLOCK_MODULES = ("munshi.domain.models", "munshi.domain.seed", "munshi.domain.repository.base", "munshi.domain.repository.cash",
                  "munshi.domain.repository.reports", "munshi.domain.repository.collections", "munshi.domain.repository.dispatch",
                  "munshi.domain.repository.orders", "munshi.domain.repository.master", "munshi.tools.core")
HOSTS = {"utc-host": UTC, "pkt-host": PKT}


class Clock:
    def __init__(self, host_tz):
        self.host_tz, self.utc = host_tz, _real_datetime(2000, 1, 1, tzinfo=UTC)

    def set_karachi(self, y, m, d, hh, mm):
        self.utc = _real_datetime(y, m, d, hh, mm, tzinfo=PKT).astimezone(UTC)


@pytest.fixture(params=sorted(HOSTS))
def clock(request, monkeypatch):
    c = Clock(HOSTS[request.param])

    class FakeDateTime(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            return c.utc.astimezone(tz) if tz else c.utc.astimezone(c.host_tz).replace(tzinfo=None)

    class FakeDate(_real_date):
        @classmethod
        def today(cls):
            return c.utc.astimezone(c.host_tz).date()

    for name in _CLOCK_MODULES:
        mod = importlib.import_module(name)
        if getattr(mod, "datetime", None) is _real_datetime: monkeypatch.setattr(mod, "datetime", FakeDateTime)
        if getattr(mod, "date", None) is _real_date: monkeypatch.setattr(mod, "date", FakeDate)
    return c


def _tools():
    from munshi.domain.seed import seeded_repository
    from munshi.tools.core import MunshiTools
    return MunshiTools(seeded_repository())


def _deliver(repo, cust, qty, cash, plan_date):
    o = repo.create_order(cust, [{"sku": "UREA-50", "qty": qty}], "t", "", "order_munshi")
    repo.confirm_order(o.order_id, "order_munshi", "clerk", override_credit=True)
    repo.allocate_order(o.order_id, "WH-MULTAN", "godown_munshi", "clerk")
    p = repo.create_dispatch_plan(plan_date, "R-MULTAN-N", "V-01", [o.order_id], "godown_munshi")
    repo.approve_dispatch_plan(p.plan_id, "godown_munshi", "clerk")
    st = repo.list_stops(p.plan_id)[0]
    return o, repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": qty}], [], cash, st.otp, "delivery_munshi"), p


def _a_business_night(clock, hh, mm):
    """Seed and trade at hh:mm Karachi on 26 Sep: three drafts, a delivered order with cash at the door,
    a counter cash payment, a bank transfer and a cash expense."""
    clock.set_karachi(2026, 9, 26, hh, mm)
    t = _tools(); repo = t.repo
    from munshi.domain.models import today_iso
    today = today_iso()
    assert today == "2026-09-26"
    drafts = [t.create_order(c, [{"sku": sku, "qty": 5}]) for c, sku in (("C-001", "UREA-50"), ("C-005", "DAP-50"), ("C-009", "UREA-50"))]
    delivered, close, _ = _deliver(repo, "C-002", 10, 5_000, today)
    cash = t.record_payment("C-004", 20_000, "cash")
    bank = t.record_payment("C-005", 100_000, "bank", "IBFT")
    exp = t.record_expense("fuel", 3_000, "diesel")
    return t, today, [d["order_id"] for d in drafts], delivered, close, cash, bank, exp


@pytest.mark.parametrize("hh, mm", [(0, 30), (23, 30)], ids=["00:30-PKT", "23:30-PKT"])
def test_every_screen_files_the_night_under_the_same_business_day(clock, hh, mm):
    t, today, drafts, delivered, close, cash, bank, exp = _a_business_night(clock, hh, mm)
    repo = t.repo
    yesterday, tomorrow = "2026-09-25", "2026-09-27"

    # ---- orders: "aaj ke orders", "aaj ke draft orders", last 7 days, by product, by customer
    todays = {o["order_id"] for o in t.list_orders(days=1)}
    assert todays == set(drafts) | {delivered.order_id}
    assert {o["order_id"] for o in t.list_orders(status="draft", days=1)} == set(drafts)
    assert {o["order_id"] for o in t.list_orders(days=7)} == todays
    assert {o["order_id"] for o in t.list_orders(sku="DAP-50", days=1)} == {drafts[1]}
    assert [o["order_id"] for o in t.list_orders(customer_id="C-009", days=1)] == [drafts[2]]
    assert {o.order_id for o in repo.list_orders(day=today)} == todays
    assert repo.list_orders(day=yesterday) == [] and repo.list_orders(day=tomorrow) == []

    # ---- the digest counts the same orders and the same money
    d = t.get_digest()
    assert d["date"] == today
    assert d["orders"]["count"] == len(todays) and d["orders"]["draft"] == len(drafts)
    assert d["orders"]["value"] == sum(o["total"] for o in t.list_orders(days=1))
    assert d["dispatch"]["plans"] == 1 and d["dispatch"]["delivered"] == 1 and d["cash"]["collected"] == 5_000
    assert d["cash"]["office_payments"] == 120_000                    # 20,000 cash + 100,000 bank (not the driver's 5,000)
    assert d["sales"]["invoiced"] == 38_500

    # ---- payments: the collection report and the cashbook agree with the digest
    col = t.collection_report(today, today)
    assert col["collected"] == 125_000 and col["by_method"] == {"cash": 25_000.0, "bank": 100_000.0}
    assert {p["entry_id"] for p in col["payments"]} == {cash["entry_id"], bank["entry_id"], close["receipt_id"]}
    assert {p["day"] for p in col["payments"]} == {today}          # `at` is UTC; `day` is the business day
    cb = t.cashbook()
    assert cb["date"] == today
    assert [i["ref"] for i in cb["cash_in"]] == [cash["entry_id"]] and cb["total_in"] == 20_000
    assert t.collection_report(yesterday, yesterday)["collected"] == 39_000    # only the seed's New Kisan Dost bank payment
    assert repo.cashbook(yesterday)["total_in"] == 0

    # ---- expenses: the new one and the demo seed's "today" diesel are both today's, on every screen
    assert exp["expense_date"] == today
    todays_cash_out = {o["ref"] for o in cb["cash_out"]}
    assert exp["expense_id"] in todays_cash_out and "EXP-SEED00" in todays_cash_out
    assert repo.get_expense("EXP-SEED00").expense_date == today
    assert d["cash"]["expenses"] == cb["total_out"] == 3_000 + 8_500
    assert t.profit_summary(today, today)["expenses"] == 11_500
    assert repo.cashbook(yesterday)["total_out"] == 1_200             # the seed's 1-day-old loading labour only

    # ---- sales
    s = t.sales_report(today, today)
    assert s["revenue"] == 38_500 and s["by_day"] == [{"date": today, "revenue": 38_500.0}]


def test_a_seed_made_after_midnight_karachi_dates_its_history_by_the_business_day(clock):
    """The demo reset is what a tester runs at night: its 'N days ago' must count Karachi days, or the seed's
    today's expense lands on yesterday's cashbook and every due date shifts a day."""
    clock.set_karachi(2026, 9, 26, 0, 30)
    repo = _tools().repo
    assert [x.expense_id for x in repo.expenses_between("2026-09-26", "2026-09-26")] == ["EXP-SEED00"]
    assert repo.get_ledger_entry("INV-SEED00").due_date == (_real_date(2026, 9, 26) - timedelta(days=12) + timedelta(days=30)).isoformat()
    from munshi.domain.models import to_business_date
    assert to_business_date(repo.get_ledger_entry("INV-SEED00").created_at) == _real_date(2026, 9, 14)


def test_orders_from_just_before_karachi_midnight_are_yesterdays(clock):
    clock.set_karachi(2026, 9, 25, 23, 50)                           # 18:50 UTC on the 25th
    t = _tools()
    late = t.create_order("C-001", [{"sku": "UREA-50", "qty": 1}])
    clock.set_karachi(2026, 9, 26, 0, 10)                            # 19:10 UTC, still the 25th in UTC
    early = t.create_order("C-002", [{"sku": "UREA-50", "qty": 1}])
    assert [o["order_id"] for o in t.list_orders(days=1)] == [early["order_id"]]
    assert [o["order_id"] for o in t.list_orders(days=2)] == [early["order_id"], late["order_id"]]
    assert t.get_digest()["orders"]["count"] == 1 and t.repo.digest("2026-09-25")["orders"]["count"] == 1


def test_recent_customer_usage_counts_an_order_made_today():
    """customer_usage compared a stored '...T..+00:00' stamp with SQLite's 'YYYY-MM-DD HH:MM:SS' as text."""
    repo = _tools().repo
    with repo.acting_as("Ayesha"):
        repo.create_order("C-003", [{"sku": "UREA-50", "qty": 1}], "chat", "", "order_munshi")
    assert repo.customer_usage("Ayesha", days=1) == {"C-003": 1}


# ============================================================ a reversal sends exactly one correction
def _corrections(repo, before: set[str]) -> list[dict]:
    return [m for m in repo.outbox(limit=1000) if m["msg_id"] not in before]


def _ids(repo) -> set[str]:
    return {m["msg_id"] for m in repo.outbox(limit=1000)}


def test_a_bounced_cheque_reversed_by_the_chat_tool_corrects_the_customer_once():
    t = _tools(); repo = t.repo
    pay = t.record_payment("C-004", 50_000, "cheque", "HBL 004512")     # the receipt goes out by WhatsApp
    before = _ids(repo)
    rev = t.reverse_ledger_entry(pay["entry_id"], "cheque bounced")
    new = _corrections(repo, before)
    assert len(new) == 1, [m["text"] for m in new]
    m = new[0]
    assert m["ref"] == rev["entry_id"] and m["to_phone"] == "0300-1111004"
    assert pay["entry_id"] in m["text"] and "cheque returned unpaid" in m["text"] and "Balance now Rs 125,000" in m["text"]
    assert "bounced" not in m["text"]                                    # the owner's reason never goes to the customer


def test_a_driver_receipt_reversed_by_the_chat_tool_corrects_the_door_message_once(clock):
    clock.set_karachi(2026, 9, 26, 0, 30)
    t = _tools(); repo = t.repo
    _, r, _ = _deliver(repo, "C-002", 2, 7_700, "2026-09-26")
    repo.queue_message("whatsapp", "0300-1111002", f"delivered. Invoice {r['invoice_id']} Rs 7,700, cash received Rs 7,700.", r["invoice_id"])
    before = _ids(repo)
    rev = t.reverse_ledger_entry(r["receipt_id"], "driver never had the cash")
    new = _corrections(repo, before)
    assert len(new) == 1 and new[0]["ref"] == rev["entry_id"] and r["receipt_id"] in new[0]["text"]
    assert "Balance now Rs 103,700" in new[0]["text"]


def test_a_reversal_of_something_never_sent_sends_nothing_by_any_path():
    t = _tools(); repo = t.repo
    pay = repo.record_payment("C-005", 10_000, "cheque", "HBL 1", "hisaab_munshi", "clerk")   # form path: no receipt went out
    before = _ids(repo)
    t.reverse_ledger_entry(pay.entry_id, "cheque bounced")
    t.reverse_ledger_entry("INV-SEED00", "paper bill entered twice")
    assert _corrections(repo, before) == []


def test_the_owner_chat_bounce_queues_one_correction():
    from munshi.domain.seed import seeded_repository
    from munshi.platform import MunshiPlatform
    p = MunshiPlatform(seeded_repository())
    try:
        r = p.handle_message("o", "owner", "al barakah ne 50000 ka cheque diya", user="Owner")
        p.resolve(r.pending.approval_id, True, "owner", user="Owner")
        r = p.handle_message("o", "owner", "al barakah ka 50000 ka cheque bounce ho gaya he", user="Owner")
        assert r.pending and r.pending.tool == "reverse_ledger_entry"
        before = _ids(p.repo)
        p.resolve(r.pending.approval_id, True, "owner", user="Owner")
        new = [m for m in _corrections(p.repo, before) if m["to_phone"] == "0300-1111004"]
        assert len(new) == 1 and "cheque returned unpaid" in new[0]["text"], [m["text"] for m in new]
    finally:
        p.close()
