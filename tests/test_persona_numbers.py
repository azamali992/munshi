"""Wrong numbers a persona tester saw shown as fact (209-turn run on the demo business), fixed in the domain.

Every scenario runs on the demo seed at a frozen clock, 25 September 2026 18:00 Karachi, and every
expected figure is worked by hand in the comments.

  S1-10  the digest counted a CANCELLED order: "7 orders, Rs 421,646" included Chaudhry's Rs 108,250.
  S1-8   profit showed a 98-100% gross margin: the seeded invoices had no cost snapshot (sale_lines),
         so cost of goods was Rs 0 against Rs 11 lakh of sales, with no caveat.
  S1-9   "aaj ka cashbook": the reply left out the Rs 2,000 driver shortfall. The domain exposes it
         (shortfalls / total_shortfall) and the totals tie out; the chat formatter drops it.
  S1-11  a bounced cheque was reversed but the customer kept the WhatsApp receipt saying "Balance now
         Rs 75,000" and no correction was queued.
  G11    "aaj 5 payments aayin": a reversal was counted as a payment.
"""
from __future__ import annotations

import importlib
from datetime import UTC, timedelta, timezone
from datetime import date as _real_date
from datetime import datetime as _real_datetime

import pytest

from munshi.domain.models import to_paisa

PKT = timezone(timedelta(hours=5), "PKT")
TODAY = "2026-09-25"
_CLOCK_MODULES = ("munshi.domain.models", "munshi.domain.seed", "munshi.domain.repository.base", "munshi.domain.repository.cash",
                  "munshi.domain.repository.reports", "munshi.domain.repository.collections", "munshi.domain.repository.dispatch",
                  "munshi.domain.repository.orders", "munshi.domain.repository.master")


@pytest.fixture(autouse=True)
def clock(monkeypatch):
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


@pytest.fixture
def repo():
    from munshi.domain.seed import seeded_repository
    return seeded_repository()


def _order(repo, cust, lines, *, confirm=True, allocate_at=None):
    o = repo.create_order(cust, lines, "chat", "", "order_munshi")
    if confirm: repo.confirm_order(o.order_id, "order_munshi", "clerk", override_credit=True)
    if allocate_at: repo.allocate_order(o.order_id, allocate_at, "godown_munshi", "clerk")
    return o


def _deliver(repo, cust, lines, wh, route, cash):
    o = _order(repo, cust, lines, allocate_at=wh)
    p = repo.create_dispatch_plan(TODAY, route, "V-01", [o.order_id], "godown_munshi")
    repo.approve_dispatch_plan(p.plan_id, "godown_munshi", "clerk")
    st = repo.list_stops(p.plan_id)[0]
    return repo.close_stop(st.stop_id, lines, [], cash, st.otp, "delivery_munshi"), p


# ============================================================ S1-10: cancelled orders inflate the digest
def test_digest_does_not_count_a_cancelled_order(repo):
    # Chaudhry Farms 20 urea + 5 DAP = 20 x 3,850 + 5 x 6,250 = 77,000 + 31,250 = 108,250 -- then cancelled
    chaudhry = _order(repo, "C-002", [{"sku": "UREA-50", "qty": 20}, {"sku": "DAP-50", "qty": 5}])
    assert chaudhry.total == 108_250
    repo.cancel_order(chaudhry.order_id, "customer changed his mind", "order_munshi", "clerk")
    # Haji Sons 10 urea = 38,500 (draft) and Rana Brothers 5 DAP at 2% off = 5 x 6,125 = 30,625 (confirmed)
    _order(repo, "C-009", [{"sku": "UREA-50", "qty": 10}], confirm=False)
    _order(repo, "C-005", [{"sku": "DAP-50", "qty": 5}])
    d = repo.digest()
    # before the fix: count 3, value 108,250 + 38,500 + 30,625 = 177,375
    assert (d["orders"]["count"], d["orders"]["value"]) == (2, 69_125.0)
    assert d["orders"]["draft"] == 1 and d["orders"]["confirmed"] == 1
    assert d["orders"]["cancelled"] == 1            # shown separately, never added into the activity


# ============================================================ S1-8: the demo's margin, and a caveat when cost is unknown
def test_demo_sales_carry_a_cost_snapshot_and_a_believable_margin(repo):
    start = (_real_date(2026, 9, 25) - timedelta(days=30)).isoformat()
    s = repo.sales_report(start, TODAY)
    # seeded sales in the last 30 days (days ago): Malik 385,000 (12), Green Valley 620,000 (20), Bhatti 58,000 (9),
    # New Kisan Dost 39,000 (5) = 1,102,000.  Cost at the moving average (= cost price, nothing else has moved the pool):
    #   Malik 100 urea x 3,600 = 360,000;  Green Valley 75 urea x 3,600 + 53 DAP x 5,900 = 270,000 + 312,700 = 582,700
    #   Bhatti 12 urea x 3,600 + 2 SOP x 5,500 = 43,200 + 11,000 = 54,200;  New Kisan Dost 4 urea + 4 SOP = 14,400 + 22,000 = 36,400
    #   COGS 1,033,300;  margin 68,700 = 6.2% (before the fix: COGS 0, margin 1,102,000 = 100%)
    assert (s["revenue"], s["cost_of_goods"], s["gross_margin"], s["margin_pct"]) == (1_102_000.0, 1_033_300.0, 68_700.0, 6.2)
    assert s["cost_missing"] == {"count": 0, "revenue": 0.0} and s["margin_reliable"] is True and s["caveat"] is None
    p = repo.profit_summary(start, TODAY)
    assert (p["revenue"], p["cost_of_goods"], p["gross_margin"]) == (1_102_000.0, 1_033_300.0, 68_700.0)
    assert p["margin_pct"] == 6.2 and p["cost_missing"]["count"] == 0 and p["margin_reliable"] is True
    # every seeded sale's product mix is in the sale record, so best sellers and slow stock are real
    assert {r["sku"] for r in s["by_product"]} == {"UREA-50", "DAP-50", "SOP-50"}
    slow = {r["sku"] for r in repo.slow_stock(30)}
    assert "UREA-50" not in slow and "DRIP-100" in slow        # urea is the best seller, drip line really hasn't moved


def test_demo_seed_keeps_stock_value_and_ledgers_consistent(repo):
    """The historical sales left the godowns at the moving average: the stock ledger replays to the stock table,
    the value ledger replays to the pools, and what is on the shelf is exactly the demo's opening stock."""
    from munshi.domain.seed import STOCK
    assert {(s.warehouse_id, s.sku): s.on_hand for s in repo.list_stock()} == {(w, k): q for w, lv in STOCK.items() for k, q in lv.items()}
    replay = repo.replay_stock_ledger()
    assert all(replay.get((s.warehouse_id, s.sku), 0) == s.on_hand for s in repo.list_stock())
    pools = {r["sku"]: int(r["value_paisa"]) for r in repo._all("SELECT sku, value_paisa FROM inventory_value")}
    moved = {r["sku"]: int(r["v"]) for r in repo._all("SELECT sku, SUM(value_paisa) v FROM stock_moves GROUP BY sku")}
    assert pools == moved
    cost = {p.sku: to_paisa(p.cost_price) for p in repo.list_products()}
    for sku, pool in pools.items():                            # every unit still on the shelf is valued at its cost price
        assert pool == sum(q for lv in STOCK.values() for k, q in lv.items() if k == sku) * cost[sku]
    # every seeded (paper) sales invoice has its product lines, summing to the invoice exactly
    for e in repo._all("SELECT entry_id, amount FROM ledger WHERE kind='invoice' AND entry_id LIKE 'INV-SEED%'"):
        lines = repo._all("SELECT revenue_paisa, cost_paisa, cost_basis FROM sale_lines WHERE invoice_id=?", (e["entry_id"],))
        assert lines and sum(int(r["revenue_paisa"]) for r in lines) == int(e["amount"])
        assert all(int(r["cost_paisa"]) > 0 and r["cost_basis"] == "moving_average" for r in lines)


def test_sales_with_unknown_cost_are_flagged_not_hidden_in_the_margin(repo):
    # a hand-keyed invoice (no delivery, so no cost snapshot) -- the shape of the old demo data and of any manual invoice
    repo.add_ledger("C-006", "invoice", 50_000, "manual bill 17", None, "clerk")
    s = repo.sales_report(TODAY, TODAY)
    # revenue 50,000, cost 0: the "margin" would be 100%.  It is flagged instead.
    assert (s["revenue"], s["cost_of_goods"]) == (50_000.0, 0.0)
    assert s["cost_missing"] == {"count": 1, "revenue": 50_000.0}
    assert s["margin_reliable"] is False and s["costed_margin_pct"] is None
    assert "50,000" in s["caveat"] and "1 invoice" in s["caveat"]
    # with a real delivery alongside: margin on the costed sales alone is still reported
    _deliver(repo, "C-002", [{"sku": "UREA-50", "qty": 10}], "WH-MULTAN", "R-MULTAN-N", 0)    # 38,500 revenue, 36,000 cost
    p = repo.profit_summary(TODAY, TODAY)
    assert (p["revenue"], p["cost_of_goods"]) == (88_500.0, 36_000.0)
    assert p["cost_missing"] == {"count": 1, "revenue": 50_000.0} and p["margin_reliable"] is False
    assert p["costed_margin_pct"] == 6.5                       # 2,500 / 38,500
    # an opening balance is not a sale: never "cost missing"
    repo.opening_balance("C-010", 10_000, "owner")
    assert repo.sales_report(TODAY, TODAY)["cost_missing"]["count"] == 1


def test_a_reversed_manual_invoice_is_not_reported_as_missing_cost(repo):
    e = repo.add_ledger("C-006", "invoice", 50_000, "keyed twice", None, "clerk")
    repo.reverse_ledger_entry(e.entry_id, "duplicate", "owner", "owner")
    s = repo.sales_report(TODAY, TODAY)
    assert s["revenue"] == 0.0 and s["cost_missing"] == {"count": 0, "revenue": 0.0} and s["margin_reliable"] is True


# ============================================================ S1-9: the cashbook carries the driver shortfall
def test_cashbook_exposes_the_driver_shortfall_and_ties_out(repo):
    """The persona's day: Haji Sons pays 20,000 cash at the counter; Bhatti Kisan Store pays the driver 20,000
    at the door and the driver hands in 18,000; the seed's 8,500 diesel is today's cash out."""
    repo.record_payment("C-009", 20_000, "cash", "", "hisaab_munshi", "clerk", received_by="office")
    _, plan = _deliver(repo, "C-007", [{"sku": "UREA-50", "qty": 6}], "WH-VEHARI", "R-VEHARI", 20_000)
    repo.record_deposit(plan.plan_id, 18_000, "cashier", "hisaab_munshi")
    cb = repo.cashbook(TODAY)
    # drawer: in 20,000 + hand-ins 18,000 - out 8,500 = 29,500.  Memo: driver short 20,000 - 18,000 = 2,000.
    assert (cb["total_in"], cb["total_handins"], cb["total_out"], cb["net"]) == (20_000.0, 18_000.0, 8_500.0, 29_500.0)
    assert cb["total_shortfall"] == 2_000.0 and len(cb["shortfalls"]) == 1
    assert "Bhatti Kisan Store" in cb["shortfalls"][0]["who"]
    # what the driver collected = what reached the drawer + the shortfall memo
    d = repo.digest()
    assert d["cash"]["collected"] == cb["total_handins"] + cb["total_shortfall"] == 20_000.0


# ============================================================ S1-11: a reversed receipt corrects the customer's WhatsApp
def test_reversing_a_messaged_receipt_queues_a_correction_with_the_corrected_balance(repo):
    from munshi.tools.core import MunshiTools
    tools = MunshiTools(repo)
    # Al-Barakah owes 145,000 - 20,000 = 125,000; a 50,000 cheque comes in and the receipt goes out by WhatsApp
    assert repo.outstanding("C-004") == 125_000
    pay = tools.record_payment("C-004", 50_000, "cheque", "HBL 004512")
    receipt = [m for m in repo.outbox() if m["ref"] == pay["entry_id"]]
    assert len(receipt) == 1 and "Balance now Rs 75,000" in receipt[0]["text"]
    # the cheque bounces
    rev = repo.reverse_ledger_entry(pay["entry_id"], "cheque bounced", "hisaab_munshi", "owner")
    assert repo.outstanding("C-004") == 125_000
    fix = [m for m in repo.outbox() if m["ref"] == rev.entry_id]
    assert len(fix) == 1, "no correction was queued"
    m = fix[0]
    assert m["channel"] == "whatsapp" and m["to_phone"] == "0300-1111004" and m["status"] == "queued"
    assert pay["entry_id"] in m["text"] and "Rs 50,000" in m["text"] and "Balance now Rs 125,000" in m["text"]
    assert "cheque bounced" not in m["text"]                  # the owner's free-text reason never goes to a customer


def test_reversing_an_unmessaged_entry_sends_nothing(repo):
    pay = repo.record_payment("C-005", 10_000, "bank", "IBFT", "hisaab_munshi", "clerk")     # form path: no receipt was sent
    before = len(repo.outbox())
    repo.reverse_ledger_entry(pay.entry_id, "keyed to the wrong customer", "owner", "owner")
    assert len(repo.outbox()) == before
    repo.reverse_ledger_entry("INV-SEED00", "paper bill entered twice", "owner", "owner")     # seed invoice, never messaged
    assert len(repo.outbox()) == before


def test_a_failed_receipt_message_needs_no_correction(repo):
    pay = repo.record_payment("C-005", 10_000, "cash", "", "hisaab_munshi", "clerk", received_by="office")
    m = repo.queue_message("whatsapp", "0300-1111005", "receipt", pay.entry_id)
    repo.mark_message(m["msg_id"], "failed", "number not on WhatsApp")
    before = len(repo.outbox())
    repo.reverse_ledger_entry(pay.entry_id, "wrong customer", "owner", "owner")
    assert len(repo.outbox()) == before


def test_reversing_a_driver_receipt_corrects_the_delivery_message(repo):
    """At the door the customer gets one message (ref = the invoice) that states the cash received and the balance.
    Reversing that cash receipt makes the stated balance wrong, so it is corrected too."""
    r, _ = _deliver(repo, "C-002", [{"sku": "UREA-50", "qty": 2}], "WH-MULTAN", "R-MULTAN-N", 7_700)
    repo.queue_message("whatsapp", "0300-1111002", f"delivered. Invoice {r['invoice_id']} Rs 7,700, cash received Rs 7,700.", r["invoice_id"])
    rev = repo.reverse_ledger_entry(r["receipt_id"], "driver never had the cash", "owner", "owner")
    fix = [m for m in repo.outbox() if m["ref"] == rev.entry_id]
    # Chaudhry: 96,000 seeded + 7,700 invoice - 7,700 cash + 7,700 reversed = 103,700
    assert len(fix) == 1 and r["receipt_id"] in fix[0]["text"] and "Balance now Rs 103,700" in fix[0]["text"]


# ============================================================ G11: a reversal is not a payment
def test_collection_report_counts_payments_net_of_reversals(repo):
    repo.record_payment("C-009", 20_000, "cash", "", "h", "clerk", received_by="office")
    repo.record_payment("C-009", 10_000, "jazzcash", "", "h", "clerk")
    chq = repo.record_payment("C-004", 50_000, "cheque", "HBL", "h", "clerk")
    repo.reverse_ledger_entry(chq.entry_id, "cheque bounced", "owner", "owner")
    col = repo.collection_report(TODAY, TODAY)
    # four ledger rows (20,000, 10,000, 50,000, -50,000) but two payments that stand: 30,000
    assert col["collected"] == 30_000.0 and col["payment_count"] == 2
    assert col["reversals"] == {"count": 1, "amount": 50_000.0}
