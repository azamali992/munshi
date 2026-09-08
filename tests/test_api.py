import pytest
from fastapi.testclient import TestClient
from munshi.web.app import build_app


@pytest.fixture
def c():
    return TestClient(build_app(":memory:"))


def _h(c, pin):
    return {"X-Session": c.post("/api/session", json={"pin": pin}).json()["token"]}


def test_pin_login_and_roles(c):
    assert c.post("/api/session", json={"pin": "0000"}).status_code == 401
    assert c.get("/api/me", headers=_h(c, "1111")).json()["role"] == "owner"
    assert c.get("/api/me").status_code == 401


def test_chat_approval_roundtrip(c):
    H = _h(c, "2222")
    r = c.post("/api/chat", json={"thread_id": "m", "text": "Chaudhry Farms ko 20 urea bhej do"}, headers=H).json()
    aid = r["pending"]["approval_id"]
    assert c.get("/api/approvals", headers=H).json()[0]["can_approve"] is True
    d = c.post(f"/api/approvals/{aid}", json={"approve": True}, headers=H).json()
    assert "ORD-" in d["text"] and c.get("/api/approvals", headers=H).json() == []


def test_driver_never_sees_the_otp_and_wrong_otp_is_refused(c):
    H = _h(c, "2222")
    r = c.post("/api/chat", json={"thread_id": "m", "text": "Chaudhry Farms ko 20 urea bhej do"}, headers=H).json()
    c.post(f"/api/approvals/{r['pending']['approval_id']}", json={"approve": True}, headers=H)
    oid = c.get("/api/orders", headers=H).json()[0]["order_id"]
    for txt in (f"confirm {oid}", f"allocate {oid} at WH-MULTAN", f"dispatch plan R-MULTAN-N V-01 {oid}"):
        r = c.post("/api/chat", json={"thread_id": "m", "text": txt}, headers=H).json()
        c.post(f"/api/approvals/{r['pending']['approval_id']}", json={"approve": True}, headers=H)
    did = c.get("/api/plans", headers=H).json()[0]["plan_id"]
    r = c.post("/api/chat", json={"thread_id": "m", "text": f"approve {did}"}, headers=H).json()
    c.post(f"/api/approvals/{r['pending']['approval_id']}", json={"approve": True}, headers=H)
    D = _h(c, "3333")
    stop = c.get("/api/plans", headers=D).json()[0]["stops"][0]
    assert stop["otp"] is None
    assert c.post(f"/api/stops/{stop['stop_id']}/close", json={"delivered_items": [], "otp": "0000"}, headers=D).status_code == 403
    real = c.get("/api/plans", headers=H).json()[0]["stops"][0]["otp"]
    ok = c.post(f"/api/stops/{stop['stop_id']}/close", json={"delivered_items": [{"sku": "UREA-50", "qty": 20}], "cash_collected": 1000, "otp": real}, headers=D).json()
    assert ok["status"] == "delivered"


def test_driver_cannot_export_or_read_audit(c):
    D = _h(c, "3333")
    assert c.get("/api/audit", headers=D).status_code == 403
    assert c.get("/api/export.xlsx", headers=D).status_code == 403
    assert c.get("/api/export.xlsx", headers=_h(c, "1111")).status_code == 200


def test_pwa_shell_is_served(c):
    assert c.get("/").status_code == 200 and c.get("/manifest.webmanifest").status_code == 200 and c.get("/sw.js").status_code == 200
