"""Regression tests for the approval gate's integrity.

Bug A: the person who asked for a gated action must not be the person who
       clears it (four-eyes), in the common multi-staff case. The owner is the
       business's final authority and is always an eligible second approver
       for a clerk, so the owner may clear their own request (a solo owner has
       nobody else) -- nobody else may.
Bug B: an agent turn that wants more than one gated tool call must never
       leave an action un-gated, never report "Done" while an action is stuck,
       and approving must do exactly what the card said.
Also:  an approval card must never be created for an entity that doesn't exist.
"""
import re
from dataclasses import replace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict

from munshi.agents.specialists import build_order_munshi
from munshi.domain.repository.base import CreditHoldError
from munshi.platform import MunshiPlatform


@pytest.fixture
def p():
    return MunshiPlatform()


def _draft(p, customer="C-002", sku="UREA-50", qty=1):
    try:
        oid = p.repo.create_order(customer, [{"sku": sku, "qty": qty}], "test", "test", "test").order_id
    except CreditHoldError as e:                       # over limit: saved as a draft on hold
        oid = re.search(r"ORD-[0-9A-F]+", str(e)).group(0)
    assert p.repo.get_order(oid).status == "draft"
    return oid


# ====================================================================== Bug A
def test_same_clerk_cannot_approve_own_request_but_a_second_person_can(p):
    n = len(p.repo.expenses_between("2000-01-01", "2999-01-01"))
    r = p.handle_message("t", "clerk", "expense diesel 5000 for V-01", user="Bilal")
    assert r.pending and r.pending.tool == "record_expense" and r.pending.requested_by == "Bilal"
    with pytest.raises(PermissionError, match="asked for this"):
        p.resolve(r.pending.approval_id, True, "clerk", user="Bilal")
    # refused means refused: nothing posted, the card is still waiting
    assert len(p.repo.expenses_between("2000-01-01", "2999-01-01")) == n
    assert r.pending.approval_id in p.pending
    assert not [a for a in p.repo.audit_log(50) if a["action"] == "approval_granted"]
    # a different clerk clears it
    out = p.resolve(r.pending.approval_id, True, "clerk", user="Sana")
    assert "Couldn't" not in out.text
    assert len(p.repo.expenses_between("2000-01-01", "2999-01-01")) == n + 1
    assert p.repo.approval_history()[0]["resolved_by"] == "Sana"


def test_one_clerk_shop_routes_the_clerks_request_to_the_owner(p):
    r = p.handle_message("t", "clerk", "Chaudhry Farms ko 1 urea bhej do", user="Bilal")
    with pytest.raises(PermissionError):
        p.resolve(r.pending.approval_id, True, "clerk", user="Bilal")
    n = len(p.repo.list_orders())
    p.resolve(r.pending.approval_id, True, "owner", user="Sultan")
    assert len(p.repo.list_orders()) == n + 1


def test_owner_may_clear_their_own_request(p):
    """The owner has nobody above them; a solo owner must still be able to work."""
    r = p.handle_message("o", "owner", "credit note Rana Brothers 500 goodwill", user="Sultan")
    before = p.repo.outstanding("C-005")
    p.resolve(r.pending.approval_id, True, "owner", user="Sultan")
    assert p.repo.outstanding("C-005") == before - 500


def test_self_approval_check_ignores_case_and_spacing(p):
    r = p.handle_message("t", "clerk", "expense diesel 5000 for V-01", user="Bilal Hussain")
    with pytest.raises(PermissionError):
        p.resolve(r.pending.approval_id, True, "clerk", user="  bilal hussain ")


def test_anonymous_approval_of_a_named_request_is_refused(p):
    r = p.handle_message("t", "clerk", "expense diesel 5000 for V-01", user="Bilal")
    with pytest.raises(PermissionError):
        p.resolve(r.pending.approval_id, True, "clerk")          # who is approving? unknown -> can't prove it's someone else


def test_requester_may_still_withdraw_their_own_request(p):
    n = len(p.repo.expenses_between("2000-01-01", "2999-01-01"))
    r = p.handle_message("t", "clerk", "expense diesel 5000 for V-01", user="Bilal")
    p.resolve(r.pending.approval_id, False, "clerk", "typo", user="Bilal")
    assert p.pending == {} and len(p.repo.expenses_between("2000-01-01", "2999-01-01")) == n


def test_raised_credit_limit_does_not_let_the_same_clerk_approve_the_order(p):
    """Backend's route: raise the customer's limit (customers:write is a clerk
    permission), then approve your own now-within-limit confirmation."""
    oid = _draft(p, "C-010", "SEED-MAIZE", 30)
    assert p.repo.over_credit(p.repo.get_order(oid))
    p.repo.upsert_customer(replace(p.repo.get_customer("C-010"), credit_limit=10_000_000))
    r = p.handle_message("t", "clerk", f"confirm {oid}", user="Bilal")
    assert r.pending and r.pending.tool == "confirm_order" and r.pending.needs_role == "clerk"
    with pytest.raises(PermissionError, match="asked for this"):
        p.resolve(r.pending.approval_id, True, "clerk", user="Bilal")
    assert p.repo.get_order(oid).status == "draft"
    p.resolve(r.pending.approval_id, True, "owner", user="Sultan")
    assert p.repo.get_order(oid).status == "confirmed"


def test_over_limit_card_is_not_downgraded_by_raising_the_limit_afterwards(p):
    oid = _draft(p, "C-010", "SEED-MAIZE", 30)
    r = p.handle_message("t", "clerk", f"confirm {oid}", user="Bilal")
    assert r.pending.needs_role == "owner"
    p.repo.upsert_customer(replace(p.repo.get_customer("C-010"), credit_limit=10_000_000))
    with pytest.raises(PermissionError):
        p.resolve(r.pending.approval_id, True, "clerk", user="Sana")
    assert p.repo.get_order(oid).status == "draft"


def test_card_is_escalated_if_the_order_goes_over_limit_before_approval(p):
    oid = _draft(p, "C-010", "SEED-MAIZE", 1)
    r = p.handle_message("t", "clerk", f"confirm {oid}", user="Bilal")
    assert r.pending.needs_role == "clerk"
    p.repo.upsert_customer(replace(p.repo.get_customer("C-010"), credit_limit=1))
    with pytest.raises(PermissionError, match="owner"):
        p.resolve(r.pending.approval_id, True, "clerk", user="Sana")
    assert [a for a in p.repo.audit_log(50) if a["action"] == "approval_granted"] == []


def test_approval_refusal_policy_unit():
    from munshi.safety.risk import approval_refusal
    assert approval_refusal("clerk", "record_expense", "clerk", approver="Sana", requester="Bilal") is None
    assert "asked for this" in approval_refusal("clerk", "record_expense", "clerk", approver="Bilal", requester="Bilal")
    assert approval_refusal("owner", "record_expense", "clerk", approver="Sultan", requester="Sultan") is None
    assert approval_refusal("clerk", "credit_note", "owner", approver="Sana", requester="Sultan")
    assert approval_refusal("clerk", "create_order", "owner", approver="Sana", requester="Bilal")
    assert approval_refusal("salesman", "create_order", "clerk", approver="Imran", requester="Bilal")
    assert approval_refusal("clerk", "record_expense", "clerk", approver="", requester="Bilal")


# ====================================================================== Bug B
def _assert_tool_protocol(messages: list[BaseMessage]) -> None:
    """What OpenAI-compatible chat APIs (Groq included) enforce with a 400: an
    assistant message carrying tool_calls must be followed by a tool message
    for every one of its call ids, and a tool message must answer a call."""
    for i, m in enumerate(messages):
        if isinstance(m, AIMessage) and m.tool_calls:
            want = {tc["id"] for tc in m.tool_calls}
            got, j = set(), i + 1
            while j < len(messages) and isinstance(messages[j], ToolMessage):
                got.add(messages[j].tool_call_id); j += 1
            if want - got:
                raise ValueError(f"400 invalid_request: tool_calls {sorted(want - got)} have no tool response")
        if isinstance(m, ToolMessage):
            prev = next((x for x in reversed(messages[:i]) if not isinstance(x, ToolMessage)), None)
            if not (isinstance(prev, AIMessage) and m.tool_call_id in {tc["id"] for tc in prev.tool_calls}):
                raise ValueError(f"400 invalid_request: tool message {m.tool_call_id} answers no call")


class ScriptedModel(BaseChatModel):
    """`plan` maps a user message to the tool-call steps a real model would
    take for it; each step is a list of (tool, args) emitted in ONE AIMessage.
    Enforces the provider's tool-call protocol on every request."""

    plan: dict = {}
    calls_seen: list = []
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        _assert_tool_protocol(messages)
        h = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        steps = self.plan.get(str(messages[h].content), [])
        done = sum(1 for m in messages[h + 1:] if isinstance(m, AIMessage) and m.tool_calls)
        if done < len(steps):
            calls = [{"name": n, "args": a, "id": f"call_{h}_{done}_{k}"} for k, (n, a) in enumerate(steps[done])]
            self.calls_seen.append([c["args"] for c in calls])
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))])
        results = [str(m.content) for m in messages[h + 1:] if isinstance(m, ToolMessage)]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Done -- " + " | ".join(results)[:400]))])


def _scripted(p, plan):
    # guarded=False: these tests drive the approval gate itself with arbitrary scripted calls; the entity guard
    # that sits in front of it for a real model is tested in test_hybrid.py
    p.specialists["order"] = build_order_munshi(p.ops, p.repo, ScriptedModel(plan=plan, calls_seen=[]), guarded=False)
    return p


def _interrupted(p, thread, role="clerk"):
    st = p.specialists["order"].agent.get_state(p._cfg(thread, role, "order"))
    return bool(st.next) or bool(st.interrupts)


def test_two_gated_calls_in_one_turn_never_bypass_or_break_the_gate(p):
    o1, o2 = _draft(p), _draft(p)
    text = f"confirm {o1} and {o2}"
    _scripted(p, {text: [[("confirm_order", {"order_id": o1}), ("confirm_order", {"order_id": o2})]],
                  "any update on my order?": []})
    r = p.handle_message("t", "clerk", text, user="Bilal")
    # exactly one card, for the first call; the second is named to the user as NOT asked for
    assert r.pending and r.pending.tool == "confirm_order" and r.pending.args["order_id"] == o1
    assert len(p.pending) == 1
    assert o2 in r.text and "not" in r.text.lower()
    assert p.repo.get_order(o1).status == p.repo.get_order(o2).status == "draft"
    # approving does what the card said: o1 confirmed, nothing else, no resume failure
    out = p.resolve(r.pending.approval_id, True, "clerk", user="Sana")
    assert "Couldn't" not in out.text and out.pending is None
    assert p.repo.get_order(o1).status == "confirmed"
    assert p.repo.get_order(o2).status == "draft"            # never confirmed without its own approval
    assert not _interrupted(p, "t")
    # the conversation is healthy: the next message goes through the provider protocol check
    nxt = p.handle_message("t", "clerk", "any update on my order?", user="Bilal")
    assert nxt.text.startswith("Done")
    # and o2 can be asked for on its own
    _scripted(p, {f"confirm {o2}": [[("confirm_order", {"order_id": o2})]]})
    r2 = p.handle_message("t2", "clerk", f"confirm {o2}", user="Bilal")
    p.resolve(r2.pending.approval_id, True, "owner", user="Sultan")
    assert p.repo.get_order(o2).status == "confirmed"


def test_chained_gated_call_after_an_approval_gets_its_own_card(p):
    o1, o2 = _draft(p), _draft(p)
    text = f"confirm {o1} then {o2}"
    _scripted(p, {text: [[("confirm_order", {"order_id": o1})], [("confirm_order", {"order_id": o2})]]})
    r = p.handle_message("t", "clerk", text, user="Bilal")
    assert r.pending.args["order_id"] == o1
    out = p.resolve(r.pending.approval_id, True, "clerk", user="Sana")
    # not a bare "Done." -- the second action is surfaced as a real card
    assert out.pending is not None, out.text
    assert out.pending.tool == "confirm_order" and out.pending.args["order_id"] == o2
    assert out.pending.requested_by == "Bilal" and out.pending.requested_by_role == "clerk"
    assert o2 in out.text and out.text != "Done."
    assert [x.args["order_id"] for x in p.pending.values()] == [o2]
    assert p.repo.get_order(o1).status == "confirmed" and p.repo.get_order(o2).status == "draft"
    # the chained card is still four-eyes: the requester can't clear it
    with pytest.raises(PermissionError):
        p.resolve(out.pending.approval_id, True, "clerk", user="Bilal")
    fin = p.resolve(out.pending.approval_id, True, "clerk", user="Sana")
    assert fin.pending is None and p.repo.get_order(o2).status == "confirmed"
    assert not _interrupted(p, "t")


def test_conversation_still_works_after_a_chained_turn(p):
    """Second-order effect of the chained case: the next message on the thread
    must not carry a dangling, unanswered tool call to the model."""
    o1, o2 = _draft(p), _draft(p)
    text = f"confirm {o1} then {o2}"
    _scripted(p, {text: [[("confirm_order", {"order_id": o1})], [("confirm_order", {"order_id": o2})]],
                  "any update on my order?": []})
    r = p.handle_message("t", "clerk", text, user="Bilal")
    out = p.resolve(r.pending.approval_id, True, "clerk", user="Sana")
    if out.pending:                                  # fixed behaviour: decide the chained card first
        p.resolve(out.pending.approval_id, False, "clerk", "not yet", user="Sana")
    nxt = p.handle_message("t", "clerk", "any update on my order?", user="Bilal")
    assert nxt.text.startswith("Done"), nxt.text
    assert not _interrupted(p, "t")
    assert p.repo.get_order(o2).status == "draft"


def test_a_thread_wedged_with_no_card_heals_on_the_next_message(p):
    """A paused graph with no card behind it (threads wedged before this fix, or a
    crashed resume) is declined -- never executed -- before the next turn."""
    oid = _draft(p)
    _scripted(p, {f"confirm {oid}": [[("confirm_order", {"order_id": oid})]], "any update on my order?": []})
    p.specialists["order"].agent.invoke({"messages": [HumanMessage(f"confirm {oid}")], "role": "clerk"}, config=p._cfg("t", "clerk", "order"))
    assert _interrupted(p, "t") and p.pending == {}
    nxt = p.handle_message("t", "clerk", "any update on my order?", user="Bilal")
    assert nxt.text.startswith("Done") and not _interrupted(p, "t")
    assert p.repo.get_order(oid).status == "draft"


# ====================================================================== cards for entities that don't resolve
def test_no_card_with_a_blank_order_id(p):
    """Seen live: "Confirm order . Needs clerk approval", then "no such order:" on approve."""
    r = p.handle_message("t", "clerk", "confirm it please", user="Bilal")
    assert r.pending is None and p.pending == {}
    assert "no order was named" in r.text.lower() and "nothing was done" in r.text.lower()
    assert not _interrupted(p, "t")
    # the thread isn't wedged: a real request right after still gets its card
    oid = _draft(p)
    r = p.handle_message("t", "clerk", f"confirm {oid}", user="Bilal")
    assert r.pending and r.pending.args["order_id"] == oid


def test_unknown_order_id_card_fails_safely_on_approve(p):
    """A well-formed id that doesn't exist still gets a card (eval step
    cancel_unknown_order pins this); approving it writes nothing."""
    n, audit = len(p.repo.list_orders()), len(p.repo.audit_log(1000))
    r = p.handle_message("t", "clerk", "confirm ORD-NOSUCH1", user="Bilal")
    out = p.resolve(r.pending.approval_id, True, "owner", user="Sultan")
    assert "no such order" in out.text.lower() and len(p.repo.list_orders()) == n
    rows = p.repo.audit_log(1000)
    assert [a["action"] for a in rows[: len(rows) - audit]] == ["approval_granted"]   # the approval, and no write
