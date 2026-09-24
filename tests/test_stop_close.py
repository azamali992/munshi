"""Regression tests for the delivery-stop close path.

Bug A: delivered/returned lines were untyped and never reconciled against what
was loaded for the stop — a repeated line was invoiced twice, returns of
products never loaded put phantom stock on the shelf, and delivered + returned
could exceed the load.

Bug B: the status check ran outside the write transaction and nothing recorded
the client's idempotency key, so parallel (or retried) closes each posted
their own invoice and cash entry.
"""
from __future__ import annotations

import threading
from datetime import date

import pytest
from fastapi.testclient import TestClient

from munshi.domain.repository import StateError
from munshi.domain.seed import seeded_repository
from munshi.web.app import build_app

UREA_PRICE_C002 = 3850          # C-002 has no standing discount
DAP_PRICE_C002 = 6250


@pytest.fixture
def repo():
    return seeded_repository()


def _stop(repo, items=None, cust="C-002"):
    """An approved plan with one stop; returns (stop, plan, order)."""
    items = items or [{"sku": "UREA-50", "qty": 20}]
    o = repo.create_order(cust, items, "t", "", "order_munshi")
    repo.confirm_order(o.order_id, "order_munshi", "clerk")
    repo.allocate_order(o.order_id, "WH-MULTAN", "godown_munshi")
    p = repo.create_dispatch_plan(date.today().isoformat(), "R-MULTAN-N", "V-01", [o.order_id], "godown_munshi")
    repo.approve_dispatch_plan(p.plan_id, "godown_munshi", "clerk")
    return repo.list_stops(p.plan_id)[0], p, repo.get_order(o.order_id)


def _ledger(repo, kind, ref):
    return [dict(r) for r in repo._all("SELECT * FROM ledger WHERE kind=? AND ref=?", (kind, ref))]


def _nothing_posted(repo, st, order, stock_before: dict):
    assert repo.get_stop(st.stop_id).status == "pending"
    assert _ledger(repo, "invoice", order.order_id) == []
    assert _ledger(repo, "payment", st.stop_id) == []
    for sku, qty in stock_before.items():
        assert repo.get_stock("WH-MULTAN", sku).on_hand == qty, sku


# ---------------------------------------------------------------- Bug A: line items
def test_duplicate_delivered_lines_are_invoiced_once_at_the_combined_quantity(repo):
    """Two lines for the same product are one product: one stored line, one invoice, at the combined quantity."""
    st, _, order = _stop(repo)
    r = repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 12}, {"sku": "UREA-50", "qty": 8}], [], 0, st.otp, "delivery_munshi")
    assert r["status"] == "delivered" and r["invoiced"] == 20 * UREA_PRICE_C002
    inv = _ledger(repo, "invoice", order.order_id)
    assert len(inv) == 1 and inv[0]["amount"] == 20 * UREA_PRICE_C002
    assert repo.get_stop(st.stop_id).delivered_items == [{"sku": "UREA-50", "qty": 20}]


def test_repeated_delivered_line_is_never_multiplied_into_the_invoice(repo):
    """The reviewers' reproduction: repeating a full line billed the product twice. It must be refused, nothing posted."""
    st, _, order = _stop(repo, [{"sku": "UREA-50", "qty": 20}, {"sku": "DAP-50", "qty": 2}])
    before = {"UREA-50": repo.get_stock("WH-MULTAN", "UREA-50").on_hand, "DAP-50": repo.get_stock("WH-MULTAN", "DAP-50").on_hand}
    lines = [{"sku": "UREA-50", "qty": 20}, {"sku": "DAP-50", "qty": 2}, {"sku": "UREA-50", "qty": 20}]
    with pytest.raises(ValueError, match="UREA-50"):
        repo.close_stop(st.stop_id, lines, [], 0, st.otp, "delivery_munshi")
    _nothing_posted(repo, st, order, before)
    # the honest close still works afterwards and bills the order exactly once
    r = repo.close_stop(st.stop_id, lines[:2], [], 0, st.otp, "delivery_munshi")
    assert r["invoiced"] == order.total == 20 * UREA_PRICE_C002 + 2 * DAP_PRICE_C002
    assert [e["amount"] for e in _ledger(repo, "invoice", order.order_id)] == [order.total]


def test_return_of_product_never_loaded_is_rejected(repo):
    st, _, order = _stop(repo)          # only UREA-50 on this stop
    before = {"DAP-50": repo.get_stock("WH-MULTAN", "DAP-50").on_hand, "UREA-50": repo.get_stock("WH-MULTAN", "UREA-50").on_hand}
    with pytest.raises(ValueError, match="DAP-50"):
        repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 20}], [{"sku": "DAP-50", "qty": 100}], 0, st.otp, "delivery_munshi")
    _nothing_posted(repo, st, order, before)


def test_delivered_plus_returned_exceeding_loaded_is_rejected(repo):
    st, _, order = _stop(repo)          # 20 x UREA-50 loaded
    before = {"UREA-50": repo.get_stock("WH-MULTAN", "UREA-50").on_hand}
    for delivered, returned in ((20, 5), (15, 10), (0, 25)):
        with pytest.raises(ValueError, match="loaded"):
            repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": delivered}] if delivered else [],
                            [{"sku": "UREA-50", "qty": returned}], 0, st.otp, "delivery_munshi")
        _nothing_posted(repo, st, order, before)
    # duplicate return lines are aggregated too, so they cannot sneak past the cap
    with pytest.raises(ValueError, match="loaded"):
        repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 18}], [{"sku": "UREA-50", "qty": 2}, {"sku": "UREA-50", "qty": 2}], 0, st.otp, "d")
    _nothing_posted(repo, st, order, before)


@pytest.mark.parametrize("bad", [
    [{"sku": "UREA-50", "qty": -1}],
    [{"sku": "UREA-50", "qty": 1.5}],
    [{"sku": "UREA-50", "qty": True}],
    [{"sku": "UREA-50"}],
    [{"qty": 3}],
    ["UREA-50"],
])
def test_malformed_lines_are_rejected(repo, bad):
    st, _, order = _stop(repo)
    with pytest.raises(ValueError):
        repo.close_stop(st.stop_id, bad, [], 0, st.otp, "d")
    with pytest.raises(ValueError):
        repo.close_stop(st.stop_id, [], bad, 0, st.otp, "d")
    _nothing_posted(repo, st, order, {})


def test_balanced_partial_delivery_with_return_is_accepted(repo):
    st, _, order = _stop(repo)
    on_hand = repo.get_stock("WH-MULTAN", "UREA-50").on_hand
    r = repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 15}], [{"sku": "UREA-50", "qty": 5}], 0, st.otp, "d")
    assert r["status"] == "short" and r["invoiced"] == 15 * UREA_PRICE_C002 and r["unaccounted"] == {}
    assert repo.get_stock("WH-MULTAN", "UREA-50").on_hand == on_hand + 5


def test_short_delivery_without_return_is_allowed_but_leaves_a_trace(repo):
    """delivered + returned < loaded is legal (bags torn / lost), but the gap is reported, not silently dropped."""
    st, _, order = _stop(repo)
    r = repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 15}], [], 0, st.otp, "d", note="5 torn")
    assert r["status"] == "short" and r["unaccounted"] == {"UREA-50": 5}
    assert repo.audit_log(1, st.stop_id)[0]["payload"]["unaccounted"] == {"UREA-50": 5}
    assert "5 x UREA-50 unaccounted" in repo.notifications("clerk")[0]["text"]


# ---------------------------------------------------------------- Bug B: idempotency + concurrency
def _fire(n, fn):
    """Run fn() on n real threads released together; returns (results, errors)."""
    gate = threading.Barrier(n)
    results, errors, lock = [], [], threading.Lock()

    def worker():
        gate.wait()
        try:
            out = fn()
            with lock: results.append(out)
        except Exception as e:       # noqa: BLE001 — collected and asserted on below
            with lock: errors.append(e)

    ts = [threading.Thread(target=worker) for _ in range(n)]
    for t in ts: t.start()
    for t in ts: t.join(30)
    return results, errors


def test_concurrent_close_of_same_stop_posts_exactly_once(repo):
    st, _, order = _stop(repo)
    on_hand = repo.get_stock("WH-MULTAN", "UREA-50").on_hand
    results, errors = _fire(8, lambda: repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 18}], [{"sku": "UREA-50", "qty": 2}],
                                                      5000, st.otp, "delivery_munshi"))
    assert len(results) == 1, (results, errors)
    assert len(errors) == 7 and all(isinstance(e, StateError) and "already" in str(e) for e in errors), errors
    assert len(_ledger(repo, "invoice", order.order_id)) == 1
    assert len(_ledger(repo, "payment", st.stop_id)) == 1
    assert repo.get_stock("WH-MULTAN", "UREA-50").on_hand == on_hand + 2      # returns restocked once, not 8 times
    assert sum(1 for a in repo.audit_log(50, st.stop_id) if a["action"] == "close_stop") == 1


def test_concurrent_retries_with_same_client_ref_replay_the_single_close(repo):
    st, _, order = _stop(repo)
    results, errors = _fire(6, lambda: repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 20}], [], 1000, st.otp, "d", client_ref="phone-1"))
    assert errors == [] and len(results) == 6
    assert len({r["invoice_id"] for r in results}) == 1
    assert sum(1 for r in results if not r["replayed"]) == 1
    assert len(_ledger(repo, "invoice", order.order_id)) == 1 and len(_ledger(repo, "payment", st.stop_id)) == 1


def test_sequential_retry_is_a_noop_and_key_misuse_is_refused(repo):
    st, _, order = _stop(repo)
    first = repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 20}], [], 1000, st.otp, "d", client_ref="k1")
    again = repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 20}], [], 1000, st.otp, "d", client_ref="k1")
    assert again["replayed"] is True and again["invoice_id"] == first["invoice_id"] and again["invoiced"] == first["invoiced"]
    assert len(_ledger(repo, "invoice", order.order_id)) == 1 and len(_ledger(repo, "payment", st.stop_id)) == 1
    with pytest.raises(StateError, match="different"):          # same key, different body
        repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 19}], [], 1000, st.otp, "d", client_ref="k1")
    with pytest.raises(StateError, match="already"):            # new key on a closed stop
        repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 20}], [], 1000, st.otp, "d", client_ref="k2")
    with pytest.raises(StateError, match="already"):            # no key on a closed stop
        repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 20}], [], 1000, st.otp, "d")
    st2, _, _ = _stop(repo, [{"sku": "UREA-50", "qty": 5}])
    with pytest.raises(StateError, match="k1"):                 # a key belongs to one stop
        repo.close_stop(st2.stop_id, [{"sku": "UREA-50", "qty": 5}], [], 0, st2.otp, "d", client_ref="k1")
    assert repo.get_stop(st2.stop_id).status == "pending"


# ---------------------------------------------------------------- HTTP route
DRIVER, CLERK, OWNER = ("0300-0000003", "3333"), ("0300-0000002", "2222"), ("0300-0000001", "1111")


@pytest.fixture
def api():
    c = TestClient(build_app(in_memory=True, demo=True, scheduler=False))
    hdr = {}
    for role, (phone, pin) in (("driver", DRIVER), ("clerk", CLERK), ("owner", OWNER)):
        hdr[role] = {"X-Session": c.post("/api/session", json={"phone": phone, "pin": pin}).json()["token"]}
    return c, hdr


def _api_stop(c, H):
    # four-eyes on direct taps: nobody clears their own draft or plan, so the owner drafts, the clerk
    # confirms, allocates and plans, and the owner loads the plan
    K, O = H["clerk"], H["owner"]
    o = c.post("/api/orders", json={"customer_id": "C-002", "items": [{"sku": "UREA-50", "qty": 20}]}, headers=O).json()
    c.post(f"/api/orders/{o['order_id']}/confirm", headers=K); c.post(f"/api/orders/{o['order_id']}/allocate", json={}, headers=K)
    p = c.post("/api/plans", json={"route_id": "R-MULTAN-N", "vehicle_id": "V-01", "order_ids": [o["order_id"]]}, headers=K).json()
    p = c.post(f"/api/plans/{p['plan_id']}/approve", headers=O).json()
    return p["stops"][0], o["order_id"]


def test_api_rejects_repeated_lines_and_unloaded_returns(api):
    c, H = api
    st, oid = _api_stop(c, H)
    url = f"/api/stops/{st['stop_id']}/close"
    dup = c.post(url, json={"delivered_items": [{"sku": "UREA-50", "qty": 20}] * 2, "otp": st["otp"]}, headers=H["driver"])
    assert dup.status_code == 400 and "loaded" in dup.json()["detail"]
    phantom = c.post(url, json={"delivered_items": [], "returned_items": [{"sku": "DAP-50", "qty": 100}], "otp": st["otp"]}, headers=H["driver"])
    assert phantom.status_code == 400 and "DAP-50" in phantom.json()["detail"]
    bad = c.post(url, json={"delivered_items": [{"sku": "UREA-50", "qty": -3}], "otp": st["otp"]}, headers=H["driver"])
    assert bad.status_code == 422
    ok = c.post(url, json={"delivered_items": [{"sku": "UREA-50", "qty": 20}], "otp": st["otp"]}, headers=H["driver"])
    assert ok.status_code == 200 and ok.json()["invoiced"] == 20 * UREA_PRICE_C002


def test_api_concurrent_close_gives_one_invoice_and_clean_409s(api, monkeypatch):
    c, H = api
    st, oid = _api_stop(c, H)
    # Registry.resolve reads its shared sqlite connection without its lock, so parallel requests can
    # spuriously 401 (a separate bug in munshi/auth/registry.py). Serialise only session lookup here so
    # this test measures the stop-close race and nothing else.
    reg = c.app.state.hub.registry; real_resolve = reg.resolve
    def locked_resolve(token):
        with reg._lock:
            return real_resolve(token)
    monkeypatch.setattr(reg, "resolve", locked_resolve)
    body = {"delivered_items": [{"sku": "UREA-50", "qty": 20}], "cash_collected": 2000, "otp": st["otp"]}
    results, errors = _fire(6, lambda: c.post(f"/api/stops/{st['stop_id']}/close", json=body, headers=H["driver"]))
    assert errors == []
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409, 409, 409, 409, 409], [r.text for r in results]
    assert all("already" in r.json()["detail"] for r in results if r.status_code == 409)
    repo = next(p.repo for p in c.app.state.hub._platforms.values() if p.repo._one("SELECT 1 FROM orders WHERE order_id=?", (oid,)))
    assert len(_ledger(repo, "invoice", oid)) == 1 and len(_ledger(repo, "payment", st["stop_id"])) == 1
