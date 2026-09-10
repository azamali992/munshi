"""Security controls at the HTTP layer: rate limits, production hardening, tenant scoping of public links, input validation."""

import pytest
from fastapi.testclient import TestClient

from munshi.web.app import build_app


@pytest.fixture
def c():
    return TestClient(build_app(in_memory=True, demo=True, scheduler=False))


def test_signin_rate_limit(c):
    codes = [c.post("/api/session", json={"phone": "0300-0000009", "pin": "9999"}).status_code for _ in range(12)]
    assert 429 in codes and codes[0] == 401


def test_signup_rate_limit(c):
    codes = [c.post("/api/signup", json={"business_name": f"B{i}", "owner_name": "A B", "phone": f"0300-11122{i:02d}", "pin": "2580"}).status_code for i in range(5)]
    assert codes[:3] == [201, 201, 201] and 429 in codes


def test_production_mode_requires_secret_and_closes_signup(monkeypatch):
    monkeypatch.setenv("MUNSHI_ENV", "production"); monkeypatch.delenv("MUNSHI_SECRET", raising=False); monkeypatch.delenv("MUNSHI_SIGNUP", raising=False)
    with pytest.raises(RuntimeError):
        build_app(in_memory=True, demo=False, scheduler=False)
    monkeypatch.setenv("MUNSHI_SECRET", "x" * 64)
    app = build_app(in_memory=True, demo=True, scheduler=False)
    t = TestClient(app)
    assert t.get("/api/config").json()["signup_open"] is False
    assert t.post("/api/signup", json={"business_name": "Biz", "owner_name": "A B", "phone": "0300-1112233", "pin": "2580"}).status_code == 403
    assert t.get("/api/docs").status_code == 404                     # no API explorer in production
    assert "Strict-Transport-Security" in t.get("/").headers


def test_validation_rejects_garbage(c):
    K = {"X-Session": c.post("/api/session", json={"phone": "0300-0000002", "pin": "2222"}).json()["token"]}
    assert c.post("/api/chat", json={"thread_id": "../etc", "text": "hi"}, headers=K).status_code == 422
    assert c.post("/api/chat", json={"thread_id": "m", "text": "x" * 2000}, headers=K).status_code == 422
    assert c.post("/api/payments", json={"customer_id": "C-001", "amount": -5}, headers=K).status_code == 422
    assert c.post("/api/payments", json={"customer_id": "C-001", "amount": 5, "method": "gold"}, headers=K).status_code == 422
    assert c.post("/api/expenses", json={"category": "bribe", "amount": 5}, headers=K).status_code == 422
    assert c.post("/api/stops/STP-X/close", json={"delivered_items": [], "otp": "12"}, headers=K).status_code == 422
    assert c.post("/api/orders", json={"customer_id": "C-001", "items": []}, headers=K).status_code == 422
    assert c.post("/api/orders", json={"customer_id": "C-999", "items": [{"sku": "UREA-50", "qty": 1}]}, headers=K).status_code == 404


def test_errors_never_leak_stack_traces(c):
    K = {"X-Session": c.post("/api/session", json={"phone": "0300-0000002", "pin": "2222"}).json()["token"]}
    r = c.get("/api/orders/ORD-NOPE", headers=K)
    assert r.status_code == 404 and "Traceback" not in r.text and r.json()["detail"] == "no such order: ORD-NOPE"


def test_public_invoice_link_is_scoped_to_its_business(c):
    from munshi.documents.invoice import sign
    secret = c.app.state.secret
    s = c.post("/api/signup", json={"business_name": "Other", "owner_name": "A B", "phone": "0300-4445556", "pin": "2580"}).json()
    # a validly signed link for the demo business's seed invoice
    good = f"/i/demo.INV-SEED00.{sign(secret, 'demo:INV-SEED00')}"
    assert c.get(good).status_code == 200
    # same signature, other business id -> 404 (signature covers the business)
    bad = f"/i/{s['me']['business']['id']}.INV-SEED00.{sign(secret, 'demo:INV-SEED00')}"
    assert c.get(bad).status_code == 404
    assert c.get("/i/demo.INV-SEED00.0000000000000000").status_code == 404


def test_session_token_of_one_business_never_reads_another(c):
    K = {"X-Session": c.post("/api/session", json={"phone": "0300-0000002", "pin": "2222"}).json()["token"]}
    s = c.post("/api/signup", json={"business_name": "Other", "owner_name": "A B", "phone": "0300-4445557", "pin": "2580"}).json()
    N = {"X-Session": s["token"]}
    for path in ("/api/customers/C-001", "/api/khata/C-001", "/api/orders/ORD-X", "/api/plans/DSP-X", "/api/suppliers/S-001/khata", "/api/documents/INV-SEED00"):
        r = c.get(path, headers=N) if not path.endswith("C-001") or "khata" in path else c.patch(path, json={"name": "Hijack", "phone": ""}, headers=N)
        assert r.status_code in (404, 405), path
    assert c.get("/api/khata/C-001", headers=K).status_code == 200


def test_only_the_intended_routes_are_public():
    """Walk every route; anything without a permission dependency must be on the allow-list."""
    import inspect
    import re

    from fastapi.routing import APIRoute

    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute): yield r
            elif hasattr(r, "routes"): yield from walk(r.routes)
            elif hasattr(r, "original_router"): yield from walk(r.original_router.routes)

    app = build_app(in_memory=True, demo=False, scheduler=False)
    public = set()
    for r in walk(app.routes):
        if not r.path.startswith(("/api", "/i/", "/s/", "/healthz")) or r.path in ("/api/docs", "/api/openapi.json"): continue
        src = inspect.getsource(r.endpoint)
        if not re.search(r'context\("[^"]+"\)|require\("[^"]+"\)|Depends\(current\)', src):
            public.add(r.path)
    assert public == {"/api/config", "/api/session", "/api/signup", "/healthz", "/i/{token}", "/s/{token}"}


def test_healthz_is_cors_open_and_nothing_else_is(c):
    assert c.get("/healthz").headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-origin" not in c.get("/api/config").headers
