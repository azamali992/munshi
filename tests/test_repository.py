import pytest
from munshi.domain.repository import CapacityError, CreditHoldError, InsufficientStockError, OtpError, StateError
from munshi.domain.seed import seeded_repository


@pytest.fixture
def repo():
    return seeded_repository()


def _order(repo, cust="C-002", items=({"sku": "UREA-50", "qty": 20},)):
    o = repo.create_order(cust, list(items), "test", "", "order_munshi")
    repo.confirm_order(o.order_id, "order_munshi", "clerk")
    return o


def test_credit_hold_saves_draft_but_raises(repo):
    # C-010 limit 150k, seeded clear; a 7.8k*30 order blows the limit
    with pytest.raises(CreditHoldError):
        repo.create_order("C-010", [{"sku": "SEED-MAIZE", "qty": 30}], "chat", "", "order_munshi")
    assert repo.list_orders("draft")[0].customer_id == "C-010"


def test_allocation_reserves_and_refuses_short_stock(repo):
    o = _order(repo)
    repo.allocate_order(o.order_id, "WH-MULTAN", "godown_munshi")
    assert repo.get_stock("WH-MULTAN", "UREA-50").reserved == 20
    big = _order(repo, "C-001", ({"sku": "CYPER-1L", "qty": 500},))
    with pytest.raises(InsufficientStockError):
        repo.allocate_order(big.order_id, "WH-MULTAN", "godown_munshi")


def test_dispatch_respects_vehicle_capacity(repo):
    o = _order(repo, "C-001", ({"sku": "UREA-50", "qty": 200},))
    repo.allocate_order(o.order_id, "WH-MULTAN", "godown_munshi")
    with pytest.raises(CapacityError):
        repo.create_dispatch_plan("2026-09-08", "R-MULTAN-N", "V-01", [o.order_id], "godown_munshi")   # 120 cap
    repo.create_dispatch_plan("2026-09-08", "R-MULTAN-N", "V-02", [o.order_id], "godown_munshi")       # 260 cap


def test_only_allocated_orders_can_be_dispatched(repo):
    o = _order(repo)
    with pytest.raises(StateError):
        repo.create_dispatch_plan("2026-09-08", "R-MULTAN-N", "V-01", [o.order_id], "godown_munshi")


def test_approving_plan_moves_stock_out_and_issues_otps(repo):
    o = _order(repo); repo.allocate_order(o.order_id, "WH-MULTAN", "godown_munshi")
    p = repo.create_dispatch_plan("2026-09-08", "R-MULTAN-N", "V-01", [o.order_id], "godown_munshi")
    before = repo.get_stock("WH-MULTAN", "UREA-50")
    repo.approve_dispatch_plan(p.plan_id, "godown_munshi", "clerk")
    after = repo.get_stock("WH-MULTAN", "UREA-50")
    assert after.on_hand == before.on_hand - 20 and after.reserved == 0
    assert all(len(s.otp) == 4 for s in repo.list_stops(p.plan_id))


def test_wrong_otp_blocks_close_and_leaves_khata_untouched(repo):
    o = _order(repo); repo.allocate_order(o.order_id, "WH-MULTAN", "godown_munshi")
    p = repo.create_dispatch_plan("2026-09-08", "R-MULTAN-N", "V-01", [o.order_id], "godown_munshi")
    repo.approve_dispatch_plan(p.plan_id, "godown_munshi", "clerk")
    s = repo.list_stops(p.plan_id)[0]
    before = repo.outstanding("C-002")
    with pytest.raises(OtpError):
        repo.close_stop(s.stop_id, [{"sku": "UREA-50", "qty": 20}], [], 1000, "0000", "delivery_munshi")
    assert repo.outstanding("C-002") == before


def test_close_posts_invoice_and_payment_and_restocks_returns(repo):
    o = _order(repo); repo.allocate_order(o.order_id, "WH-MULTAN", "godown_munshi")
    p = repo.create_dispatch_plan("2026-09-08", "R-MULTAN-N", "V-01", [o.order_id], "godown_munshi")
    repo.approve_dispatch_plan(p.plan_id, "godown_munshi", "clerk")
    s = repo.list_stops(p.plan_id)[0]; before = repo.outstanding("C-002"); on_hand = repo.get_stock("WH-MULTAN", "UREA-50").on_hand
    r = repo.close_stop(s.stop_id, [{"sku": "UREA-50", "qty": 18}], [{"sku": "UREA-50", "qty": 2}], 30000, s.otp, "delivery_munshi")
    assert r["status"] == "short" and r["invoiced"] == 18 * 3850
    assert repo.outstanding("C-002") == before + 18 * 3850 - 30000
    assert repo.get_stock("WH-MULTAN", "UREA-50").on_hand == on_hand + 2


def test_deposit_variance_points_at_a_stop(repo):
    o = _order(repo); repo.allocate_order(o.order_id, "WH-MULTAN", "godown_munshi")
    p = repo.create_dispatch_plan("2026-09-08", "R-MULTAN-N", "V-01", [o.order_id], "godown_munshi")
    repo.approve_dispatch_plan(p.plan_id, "godown_munshi", "clerk")
    s = repo.list_stops(p.plan_id)[0]
    repo.close_stop(s.stop_id, [{"sku": "UREA-50", "qty": 20}], [], 50000, s.otp, "delivery_munshi")
    r = repo.record_deposit(p.plan_id, 45000, "cashier", "hisaab_munshi")
    assert r["variance"] == -5000 and r["suspect_stops"][0]["customer_id"] == "C-002"


def test_aging_applies_payments_oldest_first(repo):
    ag = {a["customer_id"]: a for a in repo.aging()}
    assert ag["C-009"]["bucket"] == "60+"
    assert "C-003" not in ag            # fully paid
    assert ag["C-004"]["balance"] == 125000   # 145k invoice, 20k paid
