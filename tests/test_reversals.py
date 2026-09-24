"""Reversals, wired end to end: the four repository reversal methods reachable as owner-only HTTP
routes and as HIGH_RISK chat tools (owner asks, owner approves; a clerk can neither ask nor approve,
exactly like credit_note / pay_supplier). The repository behaviour itself is pinned in
test_money_integrity.py; these tests pin the wiring: routes, permissions, error shapes, gating."""
import pytest
from fastapi.testclient import TestClient

from munshi.platform import MunshiPlatform
from munshi.safety.risk import RiskTier, approver_for, risk_of, role_may_approve, tools_requiring_approval
from munshi.web.app import build_app

DEMO = {"owner": ("0300-0000001", "1111"), "clerk": ("0300-0000002", "2222"), "driver": ("0300-0000003", "3333"), "salesman": ("0300-0000004", "4444")}
REVERSALS = ("reverse_ledger_entry", "reverse_expense", "reverse_purchase", "reverse_supplier_entry")
OWNER_SIG = "owner:Sultan Ahmed"


@pytest.fixture
def c():
    return TestClient(build_app(in_memory=True, demo=True, scheduler=False))


@pytest.fixture
def p():
    return MunshiPlatform()


def H(c, role):
    phone, pin = DEMO[role]
    r = c.post("/api/session", json={"phone": phone, "pin": pin})
    assert r.status_code == 200, r.text
    return {"X-Session": r.json()["token"]}


def _outstanding(c, h, cid):
    return c.get(f"/api/khata/{cid}", headers=h).json()["outstanding"]


def _cashbook(c, h):
    return c.get("/api/reports/cashbook", headers=h).json()


def _on_hand(c, h, wh, sku):
    return next(s["on_hand"] for s in c.get("/api/stock", headers=h).json() if s["warehouse_id"] == wh and s["sku"] == sku)


def _supplier_balance(c, h, sid):
    return c.get(f"/api/suppliers/{sid}/khata", headers=h).json()["balance"]


def _audit(c, h, action):
    return [a for a in c.get("/api/audit", headers=h).json() if a["action"] == action]


def _refused_for_non_owners(c, url, headers_by_role):
    for role, h in headers_by_role.items():
        r = c.post(url, json={"reason": "not mine to reverse"}, headers=h)
        assert r.status_code == 403, (role, r.text)


# ================================================================ registry + binding
def test_every_reversal_is_high_risk_and_owner_approved_only():
    for t in REVERSALS:
        assert risk_of(t) == RiskTier.HIGH_RISK and approver_for(t) == "owner" and t in tools_requiring_approval()
        assert role_may_approve("owner", t)
        assert not any(role_may_approve(r, t) for r in ("clerk", "salesman", "driver"))


def test_reversal_tools_are_bound_to_the_owner_only_on_the_right_munshi(p):
    hisaab, khareed = p.specialists["hisaab"].role_tools, p.specialists["khareed"].role_tools
    for t in ("reverse_ledger_entry", "reverse_expense"):
        assert t in hisaab["owner"] and not any(t in hisaab[r] for r in ("clerk", "salesman", "driver"))
    for t in ("reverse_purchase", "reverse_supplier_entry"):
        assert t in khareed["owner"] and not any(t in khareed[r] for r in ("clerk", "salesman", "driver"))
    others = [b.role_tools[r] for n, b in p.specialists.items() if n not in ("hisaab", "khareed") for r in b.role_tools]
    assert not any(t in tools for tools in others for t in REVERSALS)
    assert not any(t in hisaab["owner"] for t in ("reverse_purchase", "reverse_supplier_entry"))
    assert not any(t in khareed["owner"] for t in ("reverse_ledger_entry", "reverse_expense"))


# ================================================================ HTTP: customer khata entry
def test_api_reverse_ledger_entry_restores_khata_and_cashbook(c):
    O, K, S, D = H(c, "owner"), H(c, "clerk"), H(c, "salesman"), H(c, "driver")
    before, cb0 = _outstanding(c, O, "C-001"), _cashbook(c, O)
    pay = c.post("/api/payments", json={"customer_id": "C-001", "amount": 20000, "method": "cash", "ref": "counter"}, headers=K).json()
    assert pay["entry_id"].startswith("RCP-") and _outstanding(c, O, "C-001") == before - 20000
    assert _cashbook(c, O)["total_in"] == cb0["total_in"] + 20000
    url = f"/api/khata/entries/{pay['entry_id']}/reverse"

    _refused_for_non_owners(c, url, {"clerk": K, "salesman": S, "driver": D})
    assert _outstanding(c, O, "C-001") == before - 20000                     # the refusals changed nothing

    r = c.post(url, json={"reason": "keyed to the wrong customer"}, headers=O)
    assert r.status_code == 201, r.text
    rev = r.json()
    assert rev["entry_id"].startswith("REV-") and rev["reversal_of"] == pay["entry_id"] and rev["amount"] == 20000
    assert rev["customer_name"] and rev["outstanding"] == before
    assert _outstanding(c, O, "C-001") == before
    cb = _cashbook(c, O)
    assert cb["total_in"] == cb0["total_in"]                                 # the pair nets to zero in the cashbook
    assert any(x["ref"] == rev["entry_id"] and x["amount"] == -20000 and "reversal" in x["kind"] for x in cb["cash_in"])
    # the original is still on the khata, next to its reversal
    ledger = {e["entry_id"]: e for e in c.get("/api/khata/C-001", headers=O).json()["ledger"]}
    assert ledger[pay["entry_id"]]["amount"] == -20000 and ledger[rev["entry_id"]]["reversal_of"] == pay["entry_id"]
    a = _audit(c, O, "reverse_ledger_entry")[0]
    assert a["entity_id"] == pay["entry_id"] and a["approved_by"] == OWNER_SIG and a["user"] == "Sultan Ahmed"
    assert a["payload"]["reason"] == "keyed to the wrong customer"


def test_api_reverse_ledger_entry_twice_and_bad_input_are_clean_refusals(c):
    O, K = H(c, "owner"), H(c, "clerk")
    pay = c.post("/api/payments", json={"customer_id": "C-005", "amount": 7000, "method": "cheque", "ref": "chq 12"}, headers=K).json()
    url = f"/api/khata/entries/{pay['entry_id']}/reverse"
    assert c.post(url, json={"reason": "  "}, headers=O).status_code == 422          # under 3 characters
    blank = c.post(url, json={"reason": "     "}, headers=O)                          # passes the schema, not the repository
    assert blank.status_code == 400 and "reason" in blank.json()["detail"]
    assert c.post(url, json={}, headers=O).status_code == 422
    rev = c.post(url, json={"reason": "cheque bounced"}, headers=O)
    assert rev.status_code == 201
    after = _outstanding(c, O, "C-005")
    again = c.post(url, json={"reason": "cheque bounced"}, headers=O)
    assert again.status_code == 409 and "already reversed" in again.json()["detail"]
    undo = c.post(f"/api/khata/entries/{rev.json()['entry_id']}/reverse", json={"reason": "undo the reversal"}, headers=O)
    assert undo.status_code == 409 and "itself a reversal" in undo.json()["detail"]
    missing = c.post("/api/khata/entries/RCP-1999-999999/reverse", json={"reason": "no such thing"}, headers=O)
    assert missing.status_code == 404 and "no such entry" in missing.json()["detail"]
    assert _outstanding(c, O, "C-005") == after                              # none of the refusals moved money
    assert len([e for e in c.get("/api/khata/C-005", headers=O).json()["ledger"] if e["reversal_of"]]) == 1


# ================================================================ HTTP: expense
def test_api_reverse_expense_nets_expenses_and_cashbook(c):
    O, K, S, D = H(c, "owner"), H(c, "clerk"), H(c, "salesman"), H(c, "driver")
    tot0, out0 = c.get("/api/expenses", headers=O).json()["total"], _cashbook(c, O)["total_out"]
    x = c.post("/api/expenses", json={"category": "fuel", "amount": 55000, "note": "diesel V-01", "method": "cash"}, headers=K).json()
    assert _cashbook(c, O)["total_out"] == out0 + 55000
    url = f"/api/expenses/{x['expense_id']}/reverse"

    _refused_for_non_owners(c, url, {"clerk": K, "salesman": S, "driver": D})

    r = c.post(url, json={"reason": "typed 55,000 for 5,500"}, headers=O)
    assert r.status_code == 201, r.text
    rev = r.json()
    assert rev["expense_id"].startswith("EXP-") and rev["reversal_of"] == x["expense_id"] and rev["amount"] == -55000 and rev["category"] == "fuel"
    assert c.get("/api/expenses", headers=O).json()["total"] == tot0
    cb = _cashbook(c, O)
    assert cb["total_out"] == out0 and any(o["ref"] == rev["expense_id"] and o["amount"] == -55000 for o in cb["cash_out"])
    assert _audit(c, O, "reverse_expense")[0]["approved_by"] == OWNER_SIG

    again = c.post(url, json={"reason": "typed 55,000 for 5,500"}, headers=O)
    assert again.status_code == 409 and "already reversed" in again.json()["detail"]
    undo = c.post(f"/api/expenses/{rev['expense_id']}/reverse", json={"reason": "undo it"}, headers=O)
    assert undo.status_code == 409 and "itself a reversal" in undo.json()["detail"]
    assert c.post("/api/expenses/EXP-NOPE0000/reverse", json={"reason": "gone"}, headers=O).status_code == 404
    assert c.get("/api/expenses", headers=O).json()["total"] == tot0


# ================================================================ HTTP: purchase
def test_api_reverse_purchase_takes_goods_bill_and_payment_back(c):
    O, K, S, D = H(c, "owner"), H(c, "clerk"), H(c, "salesman"), H(c, "driver")
    stock0, bal0 = _on_hand(c, O, "WH-MULTAN", "UREA-50"), _supplier_balance(c, O, "S-001")
    pay0, out0 = c.get("/api/payables", headers=O).json()["total"], _cashbook(c, O)["total_out"]
    pur = c.post("/api/purchases", json={"supplier_id": "S-001", "warehouse_id": "WH-MULTAN", "items": [{"sku": "UREA-50", "qty": 100, "unit_cost": 3600}],
                                         "invoice_ref": "FF-7", "paid_amount": 60000}, headers=K).json()
    assert pur["total"] == 360000 and _on_hand(c, O, "WH-MULTAN", "UREA-50") == stock0 + 100
    assert _supplier_balance(c, O, "S-001") == bal0 + 300000 and _cashbook(c, O)["total_out"] == out0 + 60000
    url = f"/api/purchases/{pur['purchase_id']}/reverse"

    _refused_for_non_owners(c, url, {"clerk": K, "salesman": S, "driver": D})
    assert _on_hand(c, O, "WH-MULTAN", "UREA-50") == stock0 + 100

    r = c.post(url, json={"reason": "wrong supplier bill"}, headers=O)
    assert r.status_code == 201, r.text
    ret = r.json()
    assert ret["purchase_id"].startswith("PRN-") and ret["reversal_of"] == pur["purchase_id"] and ret["total"] == -360000
    assert ret["supplier_name"] and ret["balance"] == bal0
    assert _on_hand(c, O, "WH-MULTAN", "UREA-50") == stock0
    assert _supplier_balance(c, O, "S-001") == bal0 and c.get("/api/payables", headers=O).json()["total"] == pay0
    assert _cashbook(c, O)["total_out"] == out0                                # the cash paid with the bill came back too
    assert _audit(c, O, "reverse_purchase")[0]["approved_by"] == OWNER_SIG

    again = c.post(url, json={"reason": "wrong supplier bill"}, headers=O)
    assert again.status_code == 409 and "already reversed" in again.json()["detail"]
    undo = c.post(f"/api/purchases/{ret['purchase_id']}/reverse", json={"reason": "undo the return"}, headers=O)
    assert undo.status_code == 409 and "purchase return" in undo.json()["detail"]
    assert c.post("/api/purchases/PUR-1999-999999/reverse", json={"reason": "gone"}, headers=O).status_code == 404
    assert _on_hand(c, O, "WH-MULTAN", "UREA-50") == stock0 and _supplier_balance(c, O, "S-001") == bal0


# ================================================================ HTTP: supplier khata entry
def test_api_reverse_supplier_entry_restores_payables_and_cashbook(c):
    O, K, S, D = H(c, "owner"), H(c, "clerk"), H(c, "salesman"), H(c, "driver")
    bal0, pay0, out0 = _supplier_balance(c, O, "S-002"), c.get("/api/payables", headers=O).json()["total"], _cashbook(c, O)["total_out"]
    e = c.post("/api/suppliers/S-002/pay", json={"amount": 40000, "method": "cash", "ref": "counter"}, headers=O).json()
    assert e["entry_id"].startswith("SPY-") and e["balance"] == bal0 - 40000 and _cashbook(c, O)["total_out"] == out0 + 40000
    url = f"/api/suppliers/entries/{e['entry_id']}/reverse"

    _refused_for_non_owners(c, url, {"clerk": K, "salesman": S, "driver": D})

    r = c.post(url, json={"reason": "paid to the wrong supplier"}, headers=O)
    assert r.status_code == 201, r.text
    rev = r.json()
    assert rev["reversal_of"] == e["entry_id"] and rev["amount"] == 40000 and rev["kind"] == "payment" and rev["balance"] == bal0
    assert _supplier_balance(c, O, "S-002") == bal0 and c.get("/api/payables", headers=O).json()["total"] == pay0
    assert _cashbook(c, O)["total_out"] == out0
    assert _audit(c, O, "reverse_supplier_entry")[0]["approved_by"] == OWNER_SIG

    again = c.post(url, json={"reason": "paid to the wrong supplier"}, headers=O)
    assert again.status_code == 409 and "already reversed" in again.json()["detail"]
    undo = c.post(f"/api/suppliers/entries/{rev['entry_id']}/reverse", json={"reason": "undo it"}, headers=O)
    assert undo.status_code == 409 and "itself a reversal" in undo.json()["detail"]
    assert c.post("/api/suppliers/entries/SPY-NOPE0000/reverse", json={"reason": "gone"}, headers=O).status_code == 404
    assert _supplier_balance(c, O, "S-002") == bal0


def test_api_purchase_bill_must_be_reversed_with_its_purchase(c):
    O, K = H(c, "owner"), H(c, "clerk")
    pur = c.post("/api/purchases", json={"supplier_id": "S-001", "warehouse_id": "WH-MULTAN", "items": [{"sku": "UREA-50", "qty": 10, "unit_cost": 3600}]}, headers=K).json()
    bill = next(x for x in c.get("/api/suppliers/S-001/khata", headers=O).json()["recent"] if x["kind"] == "bill" and x["ref"] == pur["purchase_id"])
    bal = _supplier_balance(c, O, "S-001")
    r = c.post(f"/api/suppliers/entries/{bill['entry_id']}/reverse", json={"reason": "wrong bill"}, headers=O)
    assert r.status_code == 409 and "reverse the purchase" in r.json()["detail"]
    assert _supplier_balance(c, O, "S-001") == bal


# ================================================================ chat: HIGH_RISK gating
def _reversal_audit_follows_its_approval(p, tool):
    """The eval checker doesn't know the reversal actions yet, so check the invariant here: the agent's
    reversal row is preceded by an approval_granted row for the same tool."""
    rows = list(reversed(p.repo.audit_log(500)))
    idx = next(i for i, r in enumerate(rows) if r["action"] == tool and r["actor"] in ("hisaab_munshi", "khareed_munshi"))
    return any(r["action"] == "approval_granted" and r["payload"]["tool"] == tool for r in rows[:idx])


def test_chat_owner_reverses_an_expense_after_approval_over_http(c):
    O, K = H(c, "owner"), H(c, "clerk")
    x = c.post("/api/expenses", json={"category": "fuel", "amount": 55000, "note": "diesel", "method": "cash"}, headers=K).json()
    tot = c.get("/api/expenses", headers=O).json()["total"]

    # a clerk can't even ask for it (no such tool for the clerk role, like credit notes)
    r = c.post("/api/chat", json={"thread_id": "k", "text": f"reverse expense {x['expense_id']} typed 55000 instead of 5500"}, headers=K).json()
    assert r["pending"] is None and "owner" in r["text"] and c.get("/api/expenses", headers=O).json()["total"] == tot

    r = c.post("/api/chat", json={"thread_id": "o", "text": f"reverse expense {x['expense_id']} typed 55000 instead of 5500"}, headers=O).json()
    assert r["specialist"] == "hisaab" and r["pending"]["tool"] == "reverse_expense" and r["pending"]["needs_role"] == "owner"
    assert r["pending"]["args"]["expense_id"] == x["expense_id"]
    assert c.get("/api/expenses", headers=O).json()["total"] == tot            # paused: nothing posted yet
    aid = r["pending"]["approval_id"]
    assert c.post(f"/api/approvals/{aid}", json={"approve": True}, headers=K).status_code == 403
    assert c.get("/api/expenses", headers=O).json()["total"] == tot
    d = c.post(f"/api/approvals/{aid}", json={"approve": True}, headers=O)
    assert d.status_code == 200 and x["expense_id"] in d.json()["text"]
    assert c.get("/api/expenses", headers=O).json()["total"] == tot - 55000
    a = _audit(c, O, "reverse_expense")[0]
    assert a["actor"] == "hisaab_munshi" and a["entity_id"] == x["expense_id"] and a["approved_by"] == OWNER_SIG


def test_chat_bounced_cheque_reversal_rejected_then_approved(p):
    e = p.repo.record_payment("C-005", 12000, "cheque", "chq 44", "hisaab_munshi", "clerk")
    before = p.repo.outstanding("C-005")
    r = p.handle_message("h", "owner", f"{e.entry_id} cheque bounced, reverse it")
    assert r.specialist == "hisaab" and r.pending.tool == "reverse_ledger_entry" and r.pending.args["entry_id"] == e.entry_id
    p.resolve(r.pending.approval_id, False, "owner", "not yet")
    assert p.repo.outstanding("C-005") == before and p.repo.reversal_of_ledger(e.entry_id) is None   # a rejection leaves no trace

    r = p.handle_message("h", "owner", f"{e.entry_id} cheque bounced, reverse it")
    with pytest.raises(PermissionError):
        p.resolve(r.pending.approval_id, True, "clerk")
    done = p.resolve(r.pending.approval_id, True, "owner")
    assert "REV-" in done.text and p.repo.outstanding("C-005") == before + 12000
    assert _reversal_audit_follows_its_approval(p, "reverse_ledger_entry")

    # asking again: the card is raised (a real id), approving it is a clear refusal, not a crash, and nothing moves
    r = p.handle_message("h", "owner", f"{e.entry_id} cheque bounced, reverse it")
    again = p.resolve(r.pending.approval_id, True, "owner")
    assert "Couldn't do that" in again.text and "already reversed" in again.text
    assert p.repo.outstanding("C-005") == before + 12000


def test_chat_khareed_reverses_a_purchase_for_the_owner_only(p):
    pur = p.repo.record_purchase("S-001", "WH-MULTAN", [{"sku": "UREA-50", "qty": 50, "unit_cost": 3600}], "FF-9", 0, "khareed_munshi", "clerk")
    stock, bal = p.repo.get_stock("WH-MULTAN", "UREA-50").on_hand, p.repo.supplier_balance("S-001")
    r = p.handle_message("k", "clerk", f"reverse purchase {pur.purchase_id} wrong bill")
    assert r.specialist == "khareed" and r.pending is None and "owner" in r.text      # clerk has no reverse_purchase tool
    r = p.handle_message("k2", "owner", f"reverse purchase {pur.purchase_id} wrong bill")
    assert r.pending.tool == "reverse_purchase" and r.pending.needs_role == "owner" and r.pending.args["purchase_id"] == pur.purchase_id
    assert p.repo.get_stock("WH-MULTAN", "UREA-50").on_hand == stock
    done = p.resolve(r.pending.approval_id, True, "owner")
    assert "PRN-" in done.text
    assert p.repo.get_stock("WH-MULTAN", "UREA-50").on_hand == stock - 50 and p.repo.supplier_balance("S-001") == bal - 180000
    assert _reversal_audit_follows_its_approval(p, "reverse_purchase")


def test_chat_khareed_reverses_a_bounced_supplier_payment(p):
    e = p.repo.pay_supplier("S-001", 25000, "cheque", "chq 9", "khareed_munshi", "owner")
    bal = p.repo.supplier_balance("S-001")
    r = p.handle_message("k", "owner", f"reverse supplier payment {e.entry_id} cheque bounced")
    assert r.specialist == "khareed" and r.pending.tool == "reverse_supplier_entry" and r.pending.args["entry_id"] == e.entry_id
    assert p.repo.supplier_balance("S-001") == bal
    p.resolve(r.pending.approval_id, True, "owner")
    assert p.repo.supplier_balance("S-001") == bal + 25000
    assert _reversal_audit_follows_its_approval(p, "reverse_supplier_entry")


def test_chat_reversal_needs_a_named_entry(p):
    """The words alone never raise a card: without an id the munshi doesn't reach for a reversal tool."""
    n = len(p.repo.pending_approvals())
    r = p.handle_message("h", "owner", "reverse the bounced cheque")
    assert r.specialist == "hisaab" and r.pending is None and len(p.repo.pending_approvals()) == n
