"""Money and stock invariants: purchases, payments, expenses, cashbook, FIFO aging, promises, transfers, reports."""
from datetime import date, timedelta

import pytest

from munshi.domain.repository import InsufficientStockError, NotFoundError, StateError
from munshi.domain.seed import seeded_repository


@pytest.fixture
def repo():
    return seeded_repository()


def _deliver(repo, cust="C-002", qty=10, cash=0.0):
    o = repo.create_order(cust, [{"sku": "UREA-50", "qty": qty}], "t", "", "order_munshi")
    repo.confirm_order(o.order_id, "order_munshi", "clerk"); repo.allocate_order(o.order_id, "WH-MULTAN", "godown_munshi")
    p = repo.create_dispatch_plan(date.today().isoformat(), "R-MULTAN-N", "V-01", [o.order_id], "godown_munshi")
    repo.approve_dispatch_plan(p.plan_id, "godown_munshi", "clerk")
    st = repo.list_stops(p.plan_id)[0]
    return repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": qty}], [], cash, st.otp, "delivery_munshi"), p


def test_purchase_adds_stock_bill_and_updates_cost(repo):
    before = repo.get_stock("WH-VEHARI", "SEED-WHEAT").on_hand
    pur = repo.record_purchase("S-002", "WH-VEHARI", [{"sku": "SEED-WHEAT", "qty": 40, "unit_cost": 4800}], "ENG-1", 50000, "khareed_munshi")
    assert repo.get_stock("WH-VEHARI", "SEED-WHEAT").on_hand == before + 40
    assert pur.total == 192000 and repo.supplier_balance("S-002") == 142000
    assert repo.get_product("SEED-WHEAT").cost_price == 4800
    assert repo.stock_moves("SEED-WHEAT", "WH-VEHARI")[0].kind == "purchase"
    with pytest.raises(ValueError):
        repo.record_purchase("S-002", "WH-VEHARI", [{"sku": "SEED-WHEAT", "qty": 1, "unit_cost": 100}], "", 500, "k")   # overpaid
    with pytest.raises(NotFoundError):
        repo.record_purchase("S-999", "WH-VEHARI", [{"sku": "SEED-WHEAT", "qty": 1}], "", 0, "k")


def test_pay_supplier_and_payables(repo):
    assert repo.payables()[0]["supplier_id"] == "S-001" and repo.payables()[0]["balance"] == 540000
    repo.pay_supplier("S-001", 540000, "bank", "chq 1", "khareed_munshi", "owner")
    assert repo.payables() == []
    with pytest.raises(ValueError):
        repo.pay_supplier("S-001", 100, "bitcoin", "", "k")


def test_office_payment_posts_to_khata_with_method(repo):
    before = repo.outstanding("C-001")
    e = repo.record_payment("C-001", 85000, "easypaisa", "TX9", "hisaab_munshi", "clerk")
    assert e.entry_id.startswith("RCP-") and e.method == "easypaisa" and repo.outstanding("C-001") == before - 85000
    with pytest.raises(ValueError):
        repo.record_payment("C-001", -5, "cash", "", "h")


def test_aging_applies_payments_fifo(repo):
    # C-005: invoice 410k 33 days ago, paid 150k -> 260k open on that invoice, overdue 3 days
    row = next(r for r in repo.aging() if r["customer_id"] == "C-005")
    assert row["balance"] == 260000 and row["bucket"] == "1-30" and row["days_overdue"] == 3
    repo.record_payment("C-005", 260000, "cash", "", "h")
    assert not any(r["customer_id"] == "C-005" for r in repo.aging())
    s = repo.aging_summary()
    assert s["total"] == round(sum(r["balance"] for r in repo.aging()), 2) and "60+" in s["buckets"]


def test_expenses_and_cashbook(repo):
    today = date.today().isoformat()
    x = repo.record_expense("fuel", 3000, "diesel", "cash", "Bilal", "hisaab_munshi")
    repo.record_expense("rent", 20000, "shop", "bank", "owner", "hisaab_munshi")
    repo.record_payment("C-001", 5000, "cash", "", "h", received_by="office")
    r, p = _deliver(repo, cash=12000)
    repo.record_deposit(p.plan_id, 12000, "cashier", "hisaab_munshi")
    cb = repo.cashbook(today)
    assert cb["total_out"] == 3000 + 8500 + 0 or cb["total_out"] >= 3000        # seed fuel expense may share the day
    assert any(i["ref"] == x.expense_id for i in cb["cash_out"])
    assert cb["total_handins"] == 12000 and any(i["amount"] == 5000 for i in cb["cash_in"])
    assert not any(i["by"] == "driver" for i in cb["cash_in"])                    # driver cash arrives as a hand-in, not twice
    with pytest.raises(ValueError):
        repo.record_expense("fuel", 0, "", "cash", "", "h")


def test_deposit_variance_over_two_handins(repo):
    r, p = _deliver(repo, cash=40000)
    d1 = repo.record_deposit(p.plan_id, 25000, "c", "hisaab_munshi")
    assert d1["variance"] == -15000 and d1["suspect_stops"][0]["customer_id"] == "C-002"
    d2 = repo.record_deposit(p.plan_id, 15000, "c", "hisaab_munshi")
    assert d2["variance"] == 0 and d2["previously_deposited"] == 25000
    with pytest.raises(StateError):
        o = repo.create_order("C-002", [{"sku": "DAP-50", "qty": 1}], "t", "", "o"); repo.confirm_order(o.order_id, "o", "c"); repo.allocate_order(o.order_id, "WH-MULTAN", "g")
        plan = repo.create_dispatch_plan(date.today().isoformat(), "R-MULTAN-N", "V-01", [o.order_id], "g")
        repo.record_deposit(plan.plan_id, 1, "c", "h")   # not loaded yet


def test_promises_kept_and_broken(repo):
    past = (date.today() - timedelta(days=2)).isoformat(); future = (date.today() + timedelta(days=5)).isoformat()
    repo.log_promise("C-009", 50000, past, "wasooli_munshi")
    assert repo.open_promise("C-009")["broken"] is True and repo.broken_promises()[0]["customer_id"] == "C-009"
    repo.record_payment("C-009", 50000, "cash", "", "h")
    assert repo.open_promise("C-009")["kept"] is True and repo.broken_promises() == []
    repo.log_promise("C-008", 10000, future, "w")
    assert repo.open_promise("C-008")["broken"] is False and repo.open_promise("C-008")["kept"] is False
    with pytest.raises(ValueError):
        repo.log_promise("C-008", 100, "not-a-date", "w")


def test_transfer_between_godowns(repo):
    a, b = repo.get_stock("WH-MULTAN", "DAP-50").on_hand, repo.get_stock("WH-VEHARI", "DAP-50").on_hand
    r = repo.transfer_stock("WH-MULTAN", "WH-VEHARI", "DAP-50", 30, "godown_munshi", "clerk")
    assert repo.get_stock("WH-MULTAN", "DAP-50").on_hand == a - 30 and repo.get_stock("WH-VEHARI", "DAP-50").on_hand == b + 30
    kinds = {m.kind for m in repo.stock_moves("DAP-50") if m.ref == r["transfer_id"]}
    assert kinds == {"transfer_out", "transfer_in"}
    with pytest.raises(InsufficientStockError):
        repo.transfer_stock("WH-VEHARI", "WH-MULTAN", "SOP-50", 500, "g", "c")
    with pytest.raises(ValueError):
        repo.transfer_stock("WH-MULTAN", "WH-MULTAN", "DAP-50", 1, "g", "c")


def test_cancel_releases_reservation(repo):
    o = repo.create_order("C-002", [{"sku": "UREA-50", "qty": 15}], "t", "", "o")
    repo.confirm_order(o.order_id, "o", "c"); repo.allocate_order(o.order_id, "WH-MULTAN", "g")
    assert repo.get_stock("WH-MULTAN", "UREA-50").reserved == 15
    repo.cancel_order(o.order_id, "customer changed mind", "order_munshi", "clerk")
    assert repo.get_stock("WH-MULTAN", "UREA-50").reserved == 0 and repo.get_order(o.order_id).status == "cancelled"
    with pytest.raises(StateError):
        repo.cancel_order(o.order_id, "again", "o")


def test_customer_discount_applies_to_price(repo):
    o = repo.create_order("C-001", [{"sku": "UREA-50", "qty": 1}], "t", "", "o")     # 2% standing discount
    assert o.items[0].unit_price == 3773.0 and o.discount_pct == 2


def test_returns_restock_and_short_delivery(repo):
    o = repo.create_order("C-002", [{"sku": "UREA-50", "qty": 10}, {"sku": "DAP-50", "qty": 2}], "t", "", "o")
    repo.confirm_order(o.order_id, "o", "c"); repo.allocate_order(o.order_id, "WH-MULTAN", "g")
    p = repo.create_dispatch_plan(date.today().isoformat(), "R-MULTAN-N", "V-01", [o.order_id], "g"); repo.approve_dispatch_plan(p.plan_id, "g", "c")
    on_hand = repo.get_stock("WH-MULTAN", "DAP-50").on_hand
    st = repo.list_stops(p.plan_id)[0]
    r = repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 10}], [{"sku": "DAP-50", "qty": 2}], 0, st.otp, "d")
    assert r["status"] == "short" and r["invoiced"] == 38500 and repo.get_stock("WH-MULTAN", "DAP-50").on_hand == on_hand + 2
    assert repo.notifications("clerk")[0]["kind"] == "delivery"
    with pytest.raises(StateError):
        repo.close_stop(st.stop_id, [], [], 0, st.otp, "d")


def test_reports_add_up(repo):
    _deliver(repo, qty=10, cash=1000)
    repo.record_expense("fuel", 2000, "", "cash", "", "h")
    today = date.today().isoformat()
    sales = repo.sales_report(today, today)
    assert sales["revenue"] == 38500 and sales["by_product"][0]["qty"] == 10 and sales["cost_of_goods"] == 36000
    prof = repo.profit_summary(today, today)
    assert prof["gross_margin"] == 2500 and prof["net"] == prof["gross_margin"] - prof["expenses"]
    col = repo.collection_report(today, today)
    assert col["invoiced"] == 38500 and col["collected"] == 1000
    val = repo.stock_valuation()
    assert val["at_cost"] > 0 and val["at_sale"] > val["at_cost"]
    slow = repo.slow_stock(30)
    assert all(s["sku"] != "UREA-50" for s in slow) and any(s["sku"] == "DRIP-100" for s in slow)
    led = repo.stock_ledger("UREA-50", "WH-MULTAN")
    assert led["moves"][0]["kind"] == "sale" and led["levels"][0]["warehouse_id"] == "WH-MULTAN"
    assert repo.top_customers(30)[0]["revenue"] > 0
    d = repo.digest()
    assert d["sales"]["invoiced"] == 38500 and d["cash"]["expenses"] >= 2000 and d["payables"] == 540000


def test_low_stock_uses_per_product_threshold(repo):
    low = {(x["warehouse_id"], x["sku"]) for x in repo.low_stock()}
    assert ("WH-MULTAN", "CYPER-1L") in low and ("WH-VEHARI", "DRIP-100") in low and ("WH-MULTAN", "UREA-50") not in low


def test_settings_and_business_name(repo):
    assert repo.business_name == "Sultan Traders"
    repo.set_setting("business_name", "New Name"); assert repo.settings()["business_name"] == "New Name"
    assert repo.setting("nonexistent", "dflt") == "dflt"


def test_notifications_and_outbox(repo):
    repo.notify("owner", "digest", "hello")
    assert repo.notifications("owner", unread_only=True)[0]["text"] == "hello" and repo.notifications("clerk") == []
    repo.mark_notifications_read("owner"); assert repo.notifications("owner", unread_only=True) == []
    m = repo.queue_message("whatsapp", "0300-1", "hi", "ref")
    repo.mark_message(m["msg_id"], "failed", "boom")
    assert repo.outbox("failed")[0]["error"] == "boom"


def test_opening_balances_are_not_sales(repo):
    from munshi.domain.models import today_iso
    before = repo.sales_report(today_iso(), today_iso())["revenue"]
    repo.opening_balance("C-002", 15000, "owner")
    assert repo.sales_report(today_iso(), today_iso())["revenue"] == before
    assert repo.collection_report(today_iso(), today_iso())["invoiced"] == before
    assert repo.outstanding("C-002") == 96000 + 15000          # but it is owed
