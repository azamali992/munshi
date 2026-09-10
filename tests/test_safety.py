import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eval.run_eval import ALWAYS_GATED, safety_violations
from munshi.domain.seed import seeded_repository
from munshi.platform import MunshiPlatform
from munshi.safety.risk import RISK_REGISTRY, RiskTier, approver_for, risk_of, role_may_approve, tools_requiring_approval
from munshi.tools.core import MunshiTools
from munshi.tools.langchain_tools import build_tools
from scripts.gate_ci import compare


def test_registry_covers_every_tool_exactly():
    tools = build_tools(MunshiTools(seeded_repository()))
    assert set(tools) == set(RISK_REGISTRY)


def test_money_and_stock_tools_are_never_read_only():
    for t in ALWAYS_GATED:
        assert risk_of(t) in (RiskTier.LOW_RISK, RiskTier.HIGH_RISK), t
    assert risk_of("close_stop") == RiskTier.OTP_GATED


def test_unregistered_tool_fails_closed():
    with pytest.raises(ValueError):
        risk_of("delete_everything")


def test_owner_only_actions():
    assert approver_for("credit_note") == "owner" and approver_for("adjust_stock") == "owner"
    assert role_may_approve("owner", "credit_note") and not role_may_approve("clerk", "credit_note")
    assert role_may_approve("clerk", "create_order") and not role_may_approve("driver", "create_order")
    assert "close_stop" not in tools_requiring_approval()


def test_gate_math():
    base = {"routing_accuracy": 1, "gating_accuracy": 1, "state_checks_passed": 1, "task_success_rate": 1, "safety_violations": 0}
    assert compare(dict(base), base) == []
    assert any("gating" in f for f in compare(dict(base, gating_accuracy=0.9), base))
    assert compare(dict(base, task_success_rate=0.97), base) == []
    assert any("SAFETY" in f for f in compare(dict(base, safety_violations=1), dict(base, safety_violations=5)))


def test_checker_flags_an_agent_write_with_no_approval():
    p = MunshiPlatform()
    # an agent writes a credit note directly, bypassing the platform's approval path
    p.repo.add_ledger("C-001", "credit_note", -100, "sneaky", None, "hisaab_munshi")
    v = safety_violations(p)
    assert v and v[0]["tool"] == "credit_note"


def test_checker_passes_a_properly_approved_write():
    p = MunshiPlatform()
    r = p.handle_message("t", "owner", "credit note Rana Brothers 500 goodwill")
    p.resolve(r.pending.approval_id, True, "owner")
    assert safety_violations(p) == []
