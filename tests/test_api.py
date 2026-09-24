"""HTTP layer: sign-in, permissions per role, tenant isolation, the whole day through the API, documents."""
import pytest
from fastapi.testclient import TestClient

from munshi.web.app import build_app

DEMO = {"owner": ("0300-0000001", "1111"), "clerk": ("0300-0000002", "2222"), "driver": ("0300-0000003", "3333"), "salesman": ("0300-0000004", "4444")}


@pytest.fixture
def c():
    return TestClient(build_app(in_memory=True, demo=True, scheduler=False))


def H(c, role):
    phone, pin = DEMO[role]
    r = c.post("/api/session", json={"phone": phone, "pin": pin})
    assert r.status_code == 200, r.text
    return {"X-Session": r.json()["token"]}


def _run_order_to_delivery(c, K):
    r = c.post("/api/chat", json={"thread_id": "m", "text": "Chaudhry Farms ko 20 urea bhej do"}, headers=K).json()
    # four-eyes: the clerk who asked can't clear it; the demo has one clerk, so the owner does
    assert c.post(f"/api/approvals/{r['pending']['approval_id']}", json={"approve": True}, headers=H(c, "owner")).status_code == 200
    oid = c.get("/api/orders?status=draft", headers=K).json()[0]["order_id"]
    assert c.post(f"/api/orders/{oid}/confirm", headers=K).json()["status"] == "confirmed"
    assert c.post(f"/api/orders/{oid}/allocate", json={}, headers=K).json()["status"] == "allocated"
    p = c.post("/api/plans", json={"route_id": "R-MULTAN-N", "vehicle_id": "V-01", "order_ids": [oid]}, headers=K).json()
    # four-eyes: the clerk who planned it can't also load it; the demo's second person is the owner
    p = c.post(f"/api/plans/{p['plan_id']}/approve", headers=H(c, "owner")).json()
    return oid, p


def test_signin_and_me(c):
    assert c.post("/api/session", json={"phone": "0300-0000001", "pin": "0000"}).status_code == 401
    assert c.get("/api/me").status_code == 401
    me = c.get("/api/me", headers=H(c, "owner")).json()
    assert me["role"] == "owner" and me["business"]["name"] == "Sultan Traders" and "staff:manage" in me["permissions"]
    assert c.get("/api/me", headers={"Authorization": "Bearer " + H(c, "clerk")["X-Session"]}).json()["role"] == "clerk"


def test_lockout_after_five_wrong_pins(c):
    for _ in range(5):
        assert c.post("/api/session", json={"phone": "0300-0000002", "pin": "9999"}).status_code == 401
    assert c.post("/api/session", json={"phone": "0300-0000002", "pin": "2222"}).status_code == 423


def test_logout_ends_the_session(c):
    K = H(c, "clerk")
    assert c.post("/api/session/logout", headers=K).status_code == 200
    assert c.get("/api/me", headers=K).status_code == 401


def test_role_permissions_on_routes(c):
    D, S, K, O = H(c, "driver"), H(c, "salesman"), H(c, "clerk"), H(c, "owner")
    assert c.get("/api/audit", headers=D).status_code == 403 and c.get("/api/audit", headers=K).status_code == 200
    assert c.get("/api/khata", headers=D).status_code == 403 and c.get("/api/khata", headers=S).status_code == 200
    assert c.get("/api/export.xlsx", headers=K).status_code == 403 and c.get("/api/export.xlsx", headers=O).status_code == 200
    assert c.get("/api/staff", headers=K).status_code == 403 and c.get("/api/staff", headers=O).status_code == 200
    assert c.post("/api/payments", json={"customer_id": "C-001", "amount": 100}, headers=S).status_code == 403
    assert c.post("/api/credit-notes", json={"customer_id": "C-001", "amount": 100, "reason": "goodwill"}, headers=K).status_code == 403
    assert c.get("/api/reports/profit", headers=S).status_code == 403
    # a driver's customer list carries no balances
    assert "outstanding" not in c.get("/api/customers", headers=D).json()[0]
    # a salesman's product list carries no cost prices
    assert "cost_price" not in c.get("/api/products", headers=S).json()[0]


def test_chat_approval_roundtrip_with_user_attribution(c):
    K, O = H(c, "clerk"), H(c, "owner")
    r = c.post("/api/chat", json={"thread_id": "m", "text": "Chaudhry Farms ko 20 urea bhej do"}, headers=K).json()
    aid = r["pending"]["approval_id"]
    assert r["pending"]["requested_by"] == "Bilal Hussain"
    # the clerk who asked for it cannot clear it themselves
    own = c.post(f"/api/approvals/{aid}", json={"approve": True}, headers=K)
    assert own.status_code == 403 and "someone else" in own.json()["detail"]
    assert [a["approval_id"] for a in c.get("/api/approvals", headers=K).json()] == [aid]
    d = c.post(f"/api/approvals/{aid}", json={"approve": True}, headers=O).json()
    assert "ORD-" in d["text"] and c.get("/api/approvals", headers=K).json() == []
    audit = c.get("/api/audit", headers=K).json()
    assert audit[0]["action"] == "create_order" and audit[0]["user"] == "Sultan Ahmed"
    hist = c.get("/api/approvals/history", headers=K).json()[0]
    assert hist["requested_by"] == "Bilal Hussain" and hist["resolved_by"] == "Sultan Ahmed"


def test_salesman_books_and_clerk_approves(c):
    S, K = H(c, "salesman"), H(c, "clerk")
    r = c.post("/api/chat", json={"thread_id": "s", "text": "Rana Brothers ko 5 dap bhej do"}, headers=S).json()
    assert r["pending"] and r["pending"]["needs_role"] == "clerk"
    assert c.get("/api/approvals", headers=S).status_code == 403           # salesmen never approve
    assert c.post(f"/api/approvals/{r['pending']['approval_id']}", json={"approve": True}, headers=K).status_code == 200
    o = c.post("/api/orders", json={"customer_id": "C-005", "items": [{"sku": "UREA-50", "qty": 3}]}, headers=S).json()
    assert o["status"] == "draft" and o["created_by"] == "Imran Khan"
    assert c.post(f"/api/orders/{o['order_id']}/confirm", headers=S).status_code == 403


def test_big_orders_escalate_to_the_owner(c):
    K, O = H(c, "clerk"), H(c, "owner")
    c.patch("/api/settings", json={"big_order_limit": 50000}, headers=O)
    r = c.post("/api/chat", json={"thread_id": "m", "text": "Chaudhry Farms ko 20 urea bhej do"}, headers=K).json()
    assert r["pending"]["needs_role"] == "owner" and r["pending"]["can_approve"] is False
    assert c.post(f"/api/approvals/{r['pending']['approval_id']}", json={"approve": True}, headers=K).status_code == 403
    assert c.post(f"/api/approvals/{r['pending']['approval_id']}", json={"approve": True}, headers=O).status_code == 200


def test_driver_never_sees_the_otp_and_wrong_otp_is_refused(c):
    K, D = H(c, "clerk"), H(c, "driver")
    oid, p = _run_order_to_delivery(c, K)
    real = p["stops"][0]["otp"]
    stop = c.get("/api/driver/today", headers=D).json()[0]["stops"][0]
    assert stop["otp"] is None and stop["customer_name"] == "Chaudhry Farms"
    assert c.post(f"/api/stops/{stop['stop_id']}/close", json={"delivered_items": [], "otp": "0000"}, headers=D).status_code == 403
    ok = c.post(f"/api/stops/{stop['stop_id']}/close", json={"delivered_items": [{"sku": "UREA-50", "qty": 20}], "cash_collected": 1000, "otp": real, "client_ref": "q1"}, headers=D).json()
    assert ok["status"] == "delivered" and ok["invoice_id"].startswith("INV-")
    # an offline replay of the same close is idempotent
    again = c.post(f"/api/stops/{stop['stop_id']}/close", json={"delivered_items": [{"sku": "UREA-50", "qty": 20}], "cash_collected": 1000, "otp": real, "client_ref": "q1"}, headers=D).json()
    assert again["replayed"] is True and again["invoice_id"] == ok["invoice_id"]
    # the customer got a delivery code message and an invoice message in the outbox
    kinds = [m["ref"] for m in c.get("/api/outbox", headers=K).json()["rows"]]
    assert stop["stop_id"] in kinds and ok["invoice_id"] in kinds


def test_deposit_payment_expense_purchase_and_reports(c):
    K, O, D = H(c, "clerk"), H(c, "owner"), H(c, "driver")
    oid, p = _run_order_to_delivery(c, K)
    st = p["stops"][0]
    c.post(f"/api/stops/{st['stop_id']}/close", json={"delivered_items": [{"sku": "UREA-50", "qty": 20}], "cash_collected": 50000, "otp": st["otp"]}, headers=D)
    dep = c.post(f"/api/plans/{p['plan_id']}/deposit", json={"amount_counted": 45000}, headers=K).json()
    assert dep["variance"] == -5000 and dep["suspect_stops"][0]["customer_name"] == "Chaudhry Farms"
    assert c.get("/api/notifications", headers=O).json()[0]["kind"] == "variance"
    pay = c.post("/api/payments", json={"customer_id": "C-001", "amount": 50000, "method": "jazzcash", "ref": "tx1"}, headers=K).json()
    assert pay["entry_id"].startswith("RCP-") and pay["outstanding"] == 335000
    assert c.post("/api/expenses", json={"category": "fuel", "amount": 4000, "note": "diesel"}, headers=K).status_code == 201
    pur = c.post("/api/purchases", json={"supplier_id": "S-001", "items": [{"sku": "UREA-50", "qty": 100, "unit_cost": 3600}], "invoice_ref": "FF1"}, headers=K).json()
    assert pur["total"] == 360000 and pur["balance"] == 900000
    assert c.post("/api/suppliers/S-001/pay", json={"amount": 100000, "method": "bank"}, headers=K).status_code == 403
    assert c.post("/api/suppliers/S-001/pay", json={"amount": 100000, "method": "bank"}, headers=O).json()["balance"] == 800000
    prof = c.get("/api/reports/profit", headers=O).json()
    assert prof["revenue"] > 0 and prof["expenses"] >= 4000
    cb = c.get("/api/reports/cashbook", headers=O).json()
    assert cb["total_handins"] == 45000 and cb["total_out"] >= 4000
    assert c.get("/api/reports/stock-ledger/UREA-50", headers=K).json()["moves"][0]["kind"] == "purchase"
    assert c.get("/api/khata", headers=K).json()["summary"]["total"] > 0


def test_documents_and_public_invoice_link(c):
    K, D = H(c, "clerk"), H(c, "driver")
    oid, p = _run_order_to_delivery(c, K)
    st = p["stops"][0]
    r = c.post(f"/api/stops/{st['stop_id']}/close", json={"delivered_items": [{"sku": "UREA-50", "qty": 20}], "cash_collected": 0, "otp": st["otp"]}, headers=D).json()
    d = c.get(f"/api/documents/{r['invoice_id']}", headers=K).json()
    assert d["kind"] == "Invoice" and d["lines"][0]["qty"] == 20 and d["wa_link"].startswith("https://wa.me/92")
    assert c.get(f"/api/documents/{r['invoice_id']}/pdf", headers=K).headers["content-type"] == "application/pdf"
    pub = d["public_url"].replace("http://testserver", "")
    assert c.get(pub).status_code == 200 and "Invoice" in c.get(pub).text
    assert c.get(pub[:-2] + "zz").status_code == 404          # tampered signature
    assert c.get("/api/statements/C-002/html", headers=K).status_code == 200


def test_tenant_isolation_and_signup(c):
    K = H(c, "clerk")
    oid, _ = _run_order_to_delivery(c, K)
    s = c.post("/api/signup", json={"business_name": "Karachi Traders", "city": "Karachi", "owner_name": "Asif", "phone": "0321-5556667", "pin": "7391"}).json()
    N = {"X-Session": s["token"]}
    assert s["me"]["role"] == "owner" and s["me"]["setup"]["customers"] == 0 and s["me"]["setup"]["godowns"] == 1
    assert c.get("/api/orders", headers=N).json() == [] and c.get(f"/api/orders/{oid}", headers=N).status_code == 404
    assert c.get("/api/customers", headers=N).json() == []
    # weak PIN and duplicate phone are refused
    assert c.post("/api/signup", json={"business_name": "X Traders", "owner_name": "Ali", "phone": "0321-0000000", "pin": "1234"}).status_code == 400
    # the new owner can build the business
    assert c.post("/api/products", json={"sku": "urea-50", "name": "Urea 50kg", "unit_price": 3850}, headers=N).json()["sku"] == "UREA-50"
    cu = c.post("/api/customers", json={"name": "Test Store", "phone": "0300-7777777", "opening_balance": 5000}, headers=N).json()
    assert cu["customer_id"] == "C-001" and cu["outstanding"] == 5000
    assert c.post("/api/staff", json={"name": "Driver One", "phone": "0300-8888888", "role": "driver", "pin": "2580"}, headers=N).status_code == 201


def test_staff_management(c):
    O = H(c, "owner")
    u = c.post("/api/staff", json={"name": "New Clerk", "phone": "0301-2223334", "role": "clerk", "pin": "2580"}, headers=O).json()
    T = {"X-Session": c.post("/api/session", json={"phone": "03012223334", "pin": "2580"}).json()["token"]}
    assert c.get("/api/me", headers=T).json()["role"] == "clerk"
    assert c.post(f"/api/staff/{u['user_id']}/signout", headers=O).json()["sessions_revoked"] == 1
    assert c.get("/api/me", headers=T).status_code == 401
    assert c.patch(f"/api/staff/{u['user_id']}", json={"active": False}, headers=O).json()["active"] is False
    assert c.post("/api/session", json={"phone": "03012223334", "pin": "2580"}).status_code == 401
    me = c.get("/api/me", headers=O).json()
    assert c.patch(f"/api/staff/{me['user_id']}", json={"active": False}, headers=O).status_code == 409   # last owner


def test_import_template_roundtrip(c):
    O = H(c, "owner")
    tpl = c.get("/api/import/template.xlsx", headers=O).content
    r = c.post("/api/import", files={"file": ("t.xlsx", tpl, "application/octet-stream")}, headers=O).json()
    assert r["products"] == 1 and r["customers"] == 1 and r["errors"] == []


def test_security_headers_and_shell(c):
    r = c.get("/")
    assert r.status_code == 200 and "Content-Security-Policy" in r.headers and r.headers["X-Frame-Options"] == "DENY"
    assert c.get("/manifest.webmanifest").status_code == 200 and c.get("/sw.js").status_code == 200
    assert c.get("/healthz").json()["ok"] is True
    assert c.get("/api/config").json()["demo"] is True


def test_loading_sheet_and_public_statement(c):
    K = H(c, "clerk")
    oid, p = _run_order_to_delivery(c, K)
    sheet = c.get(f"/api/plans/{p['plan_id']}/loading-sheet", headers=K)
    assert sheet.status_code == 200 and "Chaudhry Farms" in sheet.text and p["stops"][0]["otp"] not in sheet.text
    link = c.get("/api/statements/C-002/link", headers=K).json()["url"].replace("http://testserver", "")
    assert c.get(link).status_code == 200 and "Statement of account" in c.get(link).text
    assert c.get(link[:-3] + "xyz").status_code == 404


def test_edit_draft_price_override_and_credit_confirm(c):
    K, S, O = H(c, "clerk"), H(c, "salesman"), H(c, "owner")
    o = c.post("/api/orders", json={"customer_id": "C-002", "items": [{"sku": "UREA-50", "qty": 5, "unit_price": 3700}]}, headers=K).json()
    assert o["items"][0]["unit_price"] == 3700 and o["total"] == 18500
    # a salesman's negotiated price is ignored
    s = c.post("/api/orders", json={"customer_id": "C-002", "items": [{"sku": "UREA-50", "qty": 1, "unit_price": 10}]}, headers=S).json()
    assert s["items"][0]["unit_price"] == 3850
    # edit the draft
    e = c.patch(f"/api/orders/{o['order_id']}", json={"items": [{"sku": "UREA-50", "qty": 8}], "notes": "urgent"}, headers=K).json()
    assert e["total"] == 8 * 3850 and e["notes"] == "urgent"
    # four-eyes: the clerk drafted and edited it, so the clerk can't confirm it; the owner can
    assert c.post(f"/api/orders/{o['order_id']}/confirm", headers=K).status_code == 403
    assert c.post(f"/api/orders/{o['order_id']}/confirm", headers=O).json()["status"] == "confirmed"
    assert c.patch(f"/api/orders/{o['order_id']}", json={"notes": "late"}, headers=K).status_code == 409     # not a draft any more
    # over-limit confirmation needs the owner (C-010 limit 150k, seed balance 0)
    big = c.post("/api/orders", json={"customer_id": "C-010", "items": [{"sku": "SEED-MAIZE", "qty": 30}]}, headers=K).json()
    assert big["credit_hold"]
    r = c.post(f"/api/orders/{big['order_id']}/confirm", headers=K)
    assert r.status_code == 403 and "owner" in r.json()["detail"]
    assert c.post(f"/api/orders/{big['order_id']}/confirm", headers=O).json()["status"] == "confirmed"
    assert any(a["action"] == "credit_override" for a in c.get(f"/api/orders/{big['order_id']}", headers=O).json()["audit"])
    # via chat, the same escalation happens in the approval card
    hold = c.post("/api/orders", json={"customer_id": "C-010", "items": [{"sku": "SEED-MAIZE", "qty": 30}]}, headers=K).json()
    r = c.post("/api/chat", json={"thread_id": "cc", "text": f"confirm {hold['order_id']}"}, headers=K).json()
    assert r["pending"]["needs_role"] == "owner" and "over credit" in r["pending"]["summary"]


def test_stop_note_is_kept(c):
    K, D = H(c, "clerk"), H(c, "driver")
    oid, p = _run_order_to_delivery(c, K)
    st = p["stops"][0]
    r = c.post(f"/api/stops/{st['stop_id']}/close", json={"delivered_items": [{"sku": "UREA-50", "qty": 15}], "cash_collected": 0, "otp": st["otp"], "note": "5 bags refused, torn"}, headers=D).json()
    assert r["status"] == "short"
    plan = c.get(f"/api/plans/{p['plan_id']}", headers=K).json()
    assert plan["stops"][0]["note"] == "5 bags refused, torn"
    assert any("torn" in n["text"] for n in c.get("/api/notifications", headers=K).json())
