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
