"""Four-eyes on the direct (form-tap) API routes, end to end through HTTP.

The chat approval cards refuse to let the person who asked for a gated action
clear it (safety.risk.approval_refusal). A draft order and a planned dispatch
are requests with a named author too, so confirming, allocating or loading
them through the form routes is held to the same rule: whoever drafted or
edited the request can't clear it; another clerk or the owner can; the owner
may clear their own. And a clerk can't lift a credit limit to make a held
order confirmable.

Demo staff: owner Sultan Ahmed, clerk Bilal Hussain, salesman Imran Khan;
each test adds a second clerk, Sana Malik.
"""
import pytest
from fastapi.testclient import TestClient

from munshi.web.app import build_app

DEMO = {"owner": ("0300-0000001", "1111"), "clerk": ("0300-0000002", "2222"), "salesman": ("0300-0000004", "4444")}


def _login(c, phone, pin):
    r = c.post("/api/session", json={"phone": phone, "pin": pin})
    assert r.status_code == 200, r.text
    return {"X-Session": r.json()["token"]}


def _add_clerk(c, owner, name, phone):
    assert c.post("/api/staff", json={"name": name, "phone": phone, "role": "clerk", "pin": "2580"}, headers=owner).status_code == 201
    return _login(c, phone, "2580")


@pytest.fixture
def api():
    c = TestClient(build_app(in_memory=True, demo=True, scheduler=False))
    h = {role: _login(c, *cred) for role, cred in DEMO.items()}
    h["clerk2"] = _add_clerk(c, h["owner"], "Sana Malik", "0301-2223334")
    return c, h


def _draft(c, who, customer="C-002", sku="UREA-50", qty=20):
    r = c.post("/api/orders", json={"customer_id": customer, "items": [{"sku": sku, "qty": qty}]}, headers=who)
    assert r.status_code == 201, r.text
    return r.json()


def _status(c, h, oid):
    return c.get(f"/api/orders/{oid}", headers=h["owner"]).json()["status"]


def _customer_body(c, h, cid, **changes):
    cust = next(x for x in c.get("/api/customers", headers=h["owner"]).json() if x["customer_id"] == cid)
    keys = ("name", "phone", "address", "route_id", "tier", "credit_limit", "credit_days", "discount_pct", "language", "active")
    return {k: cust[k] for k in keys} | changes


def _limit(c, h, cid):
    return _customer_body(c, h, cid)["credit_limit"]


# ---------------------------------------------------------------- confirm
def test_clerk_cannot_confirm_an_order_they_drafted_but_another_clerk_can(api):
    c, h = api
    o = _draft(c, h["clerk"])
    own = c.post(f"/api/orders/{o['order_id']}/confirm", headers=h["clerk"])
    assert own.status_code == 403 and "someone else" in own.json()["detail"]
    assert _status(c, h, o["order_id"]) == "draft"                        # refused means nothing changed
    assert not [a for a in c.get(f"/api/orders/{o['order_id']}", headers=h["owner"]).json()["audit"] if a["action"] == "order_confirmed"]
    ok = c.post(f"/api/orders/{o['order_id']}/confirm", headers=h["clerk2"])
    assert ok.status_code == 200 and ok.json()["status"] == "confirmed"
    audit = c.get(f"/api/orders/{o['order_id']}", headers=h["owner"]).json()["audit"]
    assert next(a for a in audit if a["action"] == "order_confirmed")["approved_by"] == "clerk:Sana Malik"
    # a double-tap is a state conflict, not a second confirmation
    assert c.post(f"/api/orders/{o['order_id']}/confirm", headers=h["clerk2"]).status_code == 409


def test_editing_a_draft_makes_the_editor_a_requester_too(api):
    """Otherwise: a salesman books 1 bag, a clerk edits it to 500 at a negotiated price, and confirms it alone."""
    c, h = api
    o = _draft(c, h["salesman"], qty=1)
    e = c.patch(f"/api/orders/{o['order_id']}", json={"items": [{"sku": "UREA-50", "qty": 50, "unit_price": 3000}]}, headers=h["clerk"])
    assert e.status_code == 200
    assert c.post(f"/api/orders/{o['order_id']}/confirm", headers=h["clerk"]).status_code == 403
    assert c.post(f"/api/orders/{o['order_id']}/confirm", headers=h["clerk2"]).json()["status"] == "confirmed"


def test_salesman_draft_is_still_confirmed_by_a_clerk_in_one_tap(api):
    """The everyday flow is unchanged: the salesman asks, the clerk clears."""
    c, h = api
    o = _draft(c, h["salesman"])
    assert o["created_by"] == "Imran Khan"
    assert c.post(f"/api/orders/{o['order_id']}/confirm", headers=h["salesman"]).status_code == 403     # role, as before
    assert c.post(f"/api/orders/{o['order_id']}/confirm", headers=h["clerk"]).json()["status"] == "confirmed"
    assert c.post(f"/api/orders/{o['order_id']}/allocate", json={}, headers=h["clerk"]).json()["status"] == "allocated"


def test_self_check_ignores_case_and_spacing_in_names(api):
    c, h = api
    twin = _add_clerk(c, h["owner"], "bilal  HUSSAIN", "0301-2223335")
    o = _draft(c, h["clerk"])                                              # drafted by "Bilal Hussain"
    r = c.post(f"/api/orders/{o['order_id']}/confirm", headers=twin)
    assert r.status_code == 403 and "someone else" in r.json()["detail"]


def test_confirm_needs_a_signed_in_eligible_role(api):
    c, h = api
    o = _draft(c, h["salesman"])
    assert c.post(f"/api/orders/{o['order_id']}/confirm").status_code == 401
    assert c.post(f"/api/orders/{o['order_id']}/confirm", headers={"X-Session": "forged"}).status_code == 401
    assert _status(c, h, o["order_id"]) == "draft"


# ---------------------------------------------------------------- allocate
def test_clerk_cannot_allocate_an_order_they_drafted_but_another_clerk_can(api):
    c, h = api
    o = _draft(c, h["clerk"])
    assert c.post(f"/api/orders/{o['order_id']}/confirm", headers=h["clerk2"]).status_code == 200
    own = c.post(f"/api/orders/{o['order_id']}/allocate", json={}, headers=h["clerk"])
    assert own.status_code == 403 and "someone else" in own.json()["detail"]
    assert _status(c, h, o["order_id"]) == "confirmed"
    assert c.post(f"/api/orders/{o['order_id']}/allocate", json={}, headers=h["clerk2"]).json()["status"] == "allocated"


# ---------------------------------------------------------------- dispatch plans
def _allocated(c, h):
    o = _draft(c, h["salesman"])
    c.post(f"/api/orders/{o['order_id']}/confirm", headers=h["clerk"]); c.post(f"/api/orders/{o['order_id']}/allocate", json={}, headers=h["clerk"])
    assert _status(c, h, o["order_id"]) == "allocated"
    return o["order_id"]


def test_clerk_cannot_load_a_dispatch_plan_they_made_but_another_clerk_can(api):
    c, h = api
    oid = _allocated(c, h)
    p = c.post("/api/plans", json={"route_id": "R-MULTAN-N", "vehicle_id": "V-01", "order_ids": [oid]}, headers=h["clerk"]).json()
    own = c.post(f"/api/plans/{p['plan_id']}/approve", headers=h["clerk"])
    assert own.status_code == 403 and "someone else" in own.json()["detail"]
    plan = c.get(f"/api/plans/{p['plan_id']}", headers=h["owner"]).json()
    assert plan["status"] == "planned" and all(s["otp"] is None for s in plan["stops"])     # nothing left the godown, no codes sent
    assert _status(c, h, oid) == "allocated"
    ok = c.post(f"/api/plans/{p['plan_id']}/approve", headers=h["clerk2"])
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert _status(c, h, oid) == "dispatched"


def test_unknown_plan_is_404_not_403(api):
    c, h = api
    assert c.post("/api/plans/DSP-NOSUCH/approve", headers=h["clerk"]).status_code == 404


# ---------------------------------------------------------------- the owner exception
def test_owner_may_confirm_allocate_and_load_their_own(api):
    """A solo owner has nobody above them; the chat path lets them clear their own requests too."""
    c, h = api
    o = _draft(c, h["owner"])
    assert c.post(f"/api/orders/{o['order_id']}/confirm", headers=h["owner"]).json()["status"] == "confirmed"
    assert c.post(f"/api/orders/{o['order_id']}/allocate", json={}, headers=h["owner"]).json()["status"] == "allocated"
    p = c.post("/api/plans", json={"route_id": "R-MULTAN-N", "vehicle_id": "V-01", "order_ids": [o["order_id"]]}, headers=h["owner"]).json()
    assert c.post(f"/api/plans/{p['plan_id']}/approve", headers=h["owner"]).json()["status"] == "approved"


# ---------------------------------------------------------------- the credit-limit loophole, end to end
def test_clerk_cannot_raise_a_limit_to_confirm_their_own_held_order(api):
    """The reported loophole: draft an over-limit order (held for the owner), raise the
    customer's limit (customers:write is a clerk permission), confirm the now-legal order."""
    c, h = api
    before = _limit(c, h, "C-010")
    o = _draft(c, h["clerk"], "C-010", "SEED-MAIZE", 30)
    assert o["credit_hold"]
    raise_ = c.patch("/api/customers/C-010", json=_customer_body(c, h, "C-010", credit_limit=10_000_000), headers=h["clerk"])
    assert raise_.status_code == 403 and "owner" in raise_.json()["detail"]
    assert _limit(c, h, "C-010") == before
    r = c.post(f"/api/orders/{o['order_id']}/confirm", headers=h["clerk"])
    assert r.status_code == 403 and "owner" in r.json()["detail"]
    assert _status(c, h, o["order_id"]) == "draft"
    # and removing the limit altogether (0 = no limit) is a raise too
    assert c.patch("/api/customers/C-010", json=_customer_body(c, h, "C-010", credit_limit=0), headers=h["clerk"]).status_code == 403
    assert c.post(f"/api/orders/{o['order_id']}/confirm", headers=h["clerk"]).status_code == 403
    assert _status(c, h, o["order_id"]) == "draft"


def test_a_raised_limit_does_not_downgrade_a_held_order_to_a_clerk(api):
    """Mirror of the chat path's 'only ever stricter' re-check: an order that went on credit hold
    when it was drafted stays the owner's to confirm, whoever raised the limit (here, the owner)
    and whoever drafted it (here, the salesman -- so the clerk isn't blocked by authorship)."""
    c, h = api
    o = _draft(c, h["salesman"], "C-010", "SEED-MAIZE", 30)
    assert o["credit_hold"]
    assert c.patch("/api/customers/C-010", json=_customer_body(c, h, "C-010", credit_limit=10_000_000), headers=h["owner"]).status_code == 200
    for clerk in ("clerk", "clerk2"):
        r = c.post(f"/api/orders/{o['order_id']}/confirm", headers=h[clerk])
        assert r.status_code == 403 and "credit hold" in r.json()["detail"]
    assert _status(c, h, o["order_id"]) == "draft"
    assert c.post(f"/api/orders/{o['order_id']}/confirm", headers=h["owner"]).json()["status"] == "confirmed"


def test_limit_set_directly_in_the_database_cannot_launder_a_hold_either(api):
    """Defence in depth: even a limit change that bypasses the customer route (an older build,
    an import, a hand edit) doesn't let a clerk confirm an order that went on hold."""
    from dataclasses import replace
    c, h = api
    o = _draft(c, h["salesman"], "C-010", "SEED-MAIZE", 30)
    repo = next(p.repo for p in c.app.state.hub._platforms.values() if p.repo._one("SELECT 1 FROM orders WHERE order_id=?", (o["order_id"],)))
    repo.upsert_customer(replace(repo.get_customer("C-010"), credit_limit=10_000_000))
    assert c.post(f"/api/orders/{o['order_id']}/confirm", headers=h["clerk2"]).status_code == 403
    assert _status(c, h, o["order_id"]) == "draft"


def test_clerk_may_still_tighten_a_limit_and_edit_other_fields(api):
    c, h = api
    before = _limit(c, h, "C-010")
    assert c.patch("/api/customers/C-010", json=_customer_body(c, h, "C-010", phone="0300-1234567"), headers=h["clerk"]).status_code == 200
    lower = c.patch("/api/customers/C-010", json=_customer_body(c, h, "C-010", credit_limit=before / 2), headers=h["clerk"])
    assert lower.status_code == 200 and lower.json()["credit_limit"] == before / 2
    # putting it back up is a raise: the owner's call
    assert c.patch("/api/customers/C-010", json=_customer_body(c, h, "C-010", credit_limit=before), headers=h["clerk"]).status_code == 403
    assert c.patch("/api/customers/C-010", json=_customer_body(c, h, "C-010", credit_limit=before), headers=h["owner"]).json()["credit_limit"] == before
