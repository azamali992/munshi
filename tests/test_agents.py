import pytest

from munshi.platform import MunshiPlatform


@pytest.fixture
def p():
    return MunshiPlatform()


def test_gated_action_pauses_and_state_is_untouched_until_approved(p):
    n = len(p.repo.list_orders())
    r = p.handle_message("t", "clerk", "Chaudhry Farms ko 20 urea aur 5 dap bhej do")
    assert r.pending and r.pending.tool == "create_order" and len(p.repo.list_orders()) == n
    a = p.resolve(r.pending.approval_id, True, "clerk")
    assert "ORD-" in a.text and len(p.repo.list_orders()) == n + 1


def test_reject_leaves_no_trace_in_state(p):
    n = len(p.repo.list_orders())
    r = p.handle_message("t", "clerk", "Rana Brothers ko 3 zinc bhej do")
    p.resolve(r.pending.approval_id, False, "clerk", "not now")
    assert len(p.repo.list_orders()) == n


def test_driver_has_no_order_tool(p):
    r = p.handle_message("d", "driver", "Chaudhry Farms ko 20 urea bhej do")
    assert r.pending is None and "office" in r.text


def test_clerk_cannot_even_request_a_credit_note(p):
    r = p.handle_message("c", "clerk", "credit note Rana Brothers 5000 damaged")
    assert r.pending is None and "owner" in r.text


def test_clerk_cannot_approve_an_owner_action(p):
    r = p.handle_message("o", "owner", "credit note Rana Brothers 5000 damaged")
    assert r.pending and r.pending.needs_role == "owner"
    with pytest.raises(PermissionError):
        p.resolve(r.pending.approval_id, True, "clerk")
    before = p.repo.outstanding("C-005")
    p.resolve(r.pending.approval_id, True, "owner")
    assert p.repo.outstanding("C-005") == before - 5000


def test_domain_error_becomes_a_reply_not_a_crash(p):
    r = p.handle_message("d", "driver", "close STP-NOPE delivered all cash 100 otp 0000")
    assert "Couldn't do that" in r.text


def test_one_pending_per_specialist_per_thread(p):
    p.handle_message("t", "clerk", "Chaudhry Farms ko 20 urea bhej do")
    r = p.handle_message("t", "clerk", "Rana Brothers ko 3 zinc bhej do")
    assert r.pending is None and "waiting for approval" in r.text


def test_every_approval_is_audited(p):
    r = p.handle_message("t", "clerk", "remind everyone over 30 days")
    p.resolve(r.pending.approval_id, True, "clerk")
    rows = [a for a in p.repo.audit_log(50) if a["action"] == "approval_granted"]
    assert rows and rows[0]["payload"]["tool"] == "draft_due_reminders" and rows[0]["approved_by"] == "clerk"


def test_role_change_on_a_used_thread_never_inherits_tools(p):
    """Regression: a clerk used the order munshi on this thread; a driver on the
    same thread must still have no order tools. (Caught by the demo, not the
    tests -- the stub model used to keep the previous turn's bound tools.)"""
    r = p.handle_message("shared", "clerk", "Malik Agro ka khata")
    assert r.pending is None
    r = p.handle_message("shared", "driver", "Rana Brothers ko 3 zinc bhej do")
    assert r.pending is None and "office" in r.text


# ---------------------------------------------------------------- v1.0: seven munshis, four roles
def test_khareed_munshi_receives_stock_and_owner_pays(p):
    before = p.repo.get_stock("WH-MULTAN", "UREA-50").on_hand
    r = p.handle_message("k", "clerk", "received 100 urea from Fauji at 3600 bill FF-99")
    assert r.specialist == "khareed" and r.pending.tool == "record_purchase" and r.pending.args["items"][0]["unit_cost"] == 3600
    p.resolve(r.pending.approval_id, True, "clerk")
    assert p.repo.get_stock("WH-MULTAN", "UREA-50").on_hand == before + 100 and p.repo.supplier_balance("S-001") == 900000
    r = p.handle_message("k", "clerk", "pay Fauji 100000 by bank")
    assert r.pending is None and "owner" in r.text                      # clerk has no pay_supplier tool
    r = p.handle_message("k2", "owner", "pay Fauji 100000 by bank")
    assert r.pending.tool == "pay_supplier" and r.pending.needs_role == "owner"
    p.resolve(r.pending.approval_id, True, "owner")
    assert p.repo.supplier_balance("S-001") == 800000


def test_hisaab_records_office_payment_and_expense(p):
    r = p.handle_message("h", "clerk", "Chaudhry Farms paid 20000 jazzcash")
    assert r.specialist == "hisaab" and r.pending.tool == "record_payment" and r.pending.args["method"] == "jazzcash"
    before = p.repo.outstanding("C-002"); p.resolve(r.pending.approval_id, True, "clerk")
    assert p.repo.outstanding("C-002") == before - 20000
    r = p.handle_message("h", "clerk", "expense diesel 5000 for V-01")
    assert r.pending.tool == "record_expense" and r.pending.args["category"] == "fuel"
    p.resolve(r.pending.approval_id, True, "clerk")
    assert p.repo.expenses_between("2000-01-01", "2999-01-01")[-1].amount == 5000


def test_report_munshi_is_read_only(p):
    n = len(p.repo.audit_log(1000))
    for text in ("profit this month", "sales report", "slow stock 30 days", "top customers", "stock valuation", "urea ledger"):
        r = p.handle_message("r", "owner", text)
        assert r.specialist == "report" and r.pending is None and "Done" in r.text, text
    assert len(p.repo.audit_log(1000)) == n                              # not one write


def test_salesman_books_drafts_but_cannot_confirm_or_see_money(p):
    r = p.handle_message("s", "salesman", "Rana Brothers ko 5 dap bhej do")
    assert r.pending.tool == "create_order" and r.pending.needs_role == "clerk"
    with pytest.raises(PermissionError):
        p.resolve(r.pending.approval_id, True, "salesman")
    p.resolve(r.pending.approval_id, True, "clerk")
    oid = p.repo.list_orders("draft")[0].order_id
    r = p.handle_message("s", "salesman", f"confirm {oid}")
    assert r.pending is None and "office" in r.text.lower()
    assert p.handle_message("s", "salesman", "profit this month").pending is None
    assert "office" in p.handle_message("s", "salesman", "profit this month").text
    r = p.handle_message("s", "salesman", "Haji Sons promise 50000 by 2026-09-20")
    assert r.specialist == "wasooli" and r.pending.tool == "log_promise"


def test_manager_routes_every_domain(p):
    cases = {"received 10 urea from Engro": "khareed", "what do we owe suppliers": "khareed", "profit this month": "report",
             "Chaudhry Farms paid 5000": "hisaab", "expense diesel 2000": "hisaab", "credit note Rana Brothers 500 damaged": "hisaab",
             "restock WH-MULTAN 100 urea received": "godown", "transfer 20 urea WH-MULTAN WH-VEHARI": "godown", "broken promises": "wasooli",
             "cancel ORD-XXXXXXXX not needed": "order", "stops for DSP-XXXXXXXX": "delivery"}
    for text, want in cases.items():
        assert p.handle_message("m", "owner", text).specialist == want, text


def test_big_order_escalates_to_owner(p):
    p.repo.set_setting("big_order_limit", "10000")
    r = p.handle_message("b", "clerk", "Chaudhry Farms ko 20 urea bhej do")
    assert r.pending.needs_role == "owner"
    with pytest.raises(PermissionError):
        p.resolve(r.pending.approval_id, True, "clerk")
    p.resolve(r.pending.approval_id, True, "owner")


def test_approval_is_recorded_before_the_write(p):
    r = p.handle_message("a", "clerk", "Chaudhry Farms ko 1 urea bhej do")
    p.resolve(r.pending.approval_id, True, "clerk", user="Bilal")
    rows = list(reversed(p.repo.audit_log(5)))
    idx = [x["action"] for x in rows]
    assert idx.index("approval_granted") < idx.index("create_order")
    assert p.repo.approval_history()[0]["resolved_by"] == "Bilal"
