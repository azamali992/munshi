"""Approval cards: what a human reads before tapping Approve.

The card behind every gated tool names things (customer, product, supplier, godown) instead of ids,
lists the lines, gives the total, and says in one sentence what will change, with before/after figures
read from the books. It is built read-only and never raises. The viewer-specific fields say whether an
Approve tap would be accepted, using the same check the decision route makes. A message sent while a
card waits gets that card back (or, if it is a plain question, an answer from the read-only Report munshi)
and never reaches the paused graph.

Every card here is raised through chat, by a scripted model that makes exactly the tool call a real
model would, so the whole path (manager -> specialist -> HITL pause -> card) is exercised."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from munshi.agents.specialists import build_godown_munshi, build_hisaab_munshi, build_khareed_munshi, build_order_munshi, build_wasooli_munshi
from munshi.domain.models import today_iso
from munshi.platform import CARD_EN, CARD_WORDS_EN, MunshiPlatform, PendingApproval
from munshi.safety.risk import approver_for, risk_of, tools_requiring_approval
from munshi.web.app import build_app
from tests.test_approval_integrity import ScriptedModel

BUILD = {"order": build_order_munshi, "godown": build_godown_munshi, "hisaab": build_hisaab_munshi, "khareed": build_khareed_munshi, "wasooli": build_wasooli_munshi}
WORD = {"order": "order", "godown": "godown", "hisaab": "cashbook", "khareed": "supplier", "wasooli": "wasooli"}   # what the manager routes on


def rs(v: float) -> str:
    return f"Rs {v:,.0f}"


@pytest.fixture
def p():
    return MunshiPlatform()


_n = iter(range(10_000))


def ask(p, spec, tool, args, role="owner", user="Sultan Ahmed", thread=None):
    """Send one chat message that the scripted specialist answers with exactly this gated call."""
    text = f"{WORD[spec]} #{next(_n)}"
    p.specialists[spec] = BUILD[spec](p.ops, p.repo, ScriptedModel(plan={text: [[(tool, args)]]}, calls_seen=[]))
    return p.handle_message(thread or f"t-{tool}", role, text, user=user)


def card_for(p, spec, tool, args, **kw):
    r = ask(p, spec, tool, args, **kw)
    assert r.pending is not None and r.pending.tool == tool, r.text
    c = p.card(r.pending)
    assert c["fallback"] is False and c["tool"] == tool
    assert c["tier"] == risk_of(tool).value
    json.dumps(c)                                           # the API can serialise it
    return r, c


# ------------------------------------------------------------------ fixtures in the books
def _draft(p, cid="C-002", items=(("UREA-50", 20), ("DAP-50", 5))):
    return p.repo.create_order(cid, [{"sku": s, "qty": q} for s, q in items], "test", "", "test")


def _confirmed(p, **kw):
    o = _draft(p, **kw); p.repo.confirm_order(o.order_id, "test", "clerk"); return p.repo.get_order(o.order_id)


def _allocated(p):
    o = _confirmed(p, items=(("UREA-50", 2),)); p.repo.allocate_order(o.order_id, "WH-MULTAN", "test", "clerk"); return p.repo.get_order(o.order_id)


def _loaded_plan(p):
    o = _allocated(p)
    plan = p.repo.create_dispatch_plan(today_iso(), "R-MULTAN-N", "V-01", [o.order_id], "test")
    p.repo.approve_dispatch_plan(plan.plan_id, "test", "owner")
    return plan, o


# ================================================================== one card per gated tool
def test_create_order_card_names_the_customer_and_products_and_gives_the_khata_effect(p):
    now = p.repo.outstanding("C-002")
    r, c = card_for(p, "order", "create_order", {"customer_id": "C-002", "items": [{"sku": "UREA-50", "qty": 20}, {"sku": "DAP-50", "qty": 5}]}, role="clerk", user="Bilal Hussain")
    assert c["title"] == "Create order for Chaudhry Farms" and "C-002" not in c["title"]
    assert [(ln["name"], ln["qty"], ln["unit_price"], ln["line_total"]) for ln in c["lines"]] == [("Urea 50kg", 20, 3850.0, 77000.0), ("DAP 50kg", 5, 6250.0, 31250.0)]
    assert c["total"] == 108250.0
    assert f"Chaudhry Farms owes {rs(now)} now, {rs(now + 108250)} once delivered" in c["effect"]
    assert c["needs_role"] == "clerk" and c["approver"]["key"] == "approver_clerk"
    assert c["requested_by"] == "Bilal Hussain" and c["requested_by_role"] == "clerk" and c["created_at"]
    assert c["warnings"] == []
    # the chat reply and the owner's notification read by name too
    assert "Create order for Chaudhry Farms, Rs 108,250" in r.text
    assert any("Chaudhry Farms" in n["text"] for n in p.repo.notifications("clerk"))
    # structured for the app's Urdu templates: money as {"rs": ...}, names as strings
    assert c["title_key"] == "t_create_order" and c["title_vars"] == {"customer": "Chaudhry Farms"}
    assert c["effect_key"] == "order_new" and c["effect_vars"]["total"] == {"rs": 108250.0}


def test_big_order_card_says_the_owner_is_needed_and_why(p):
    p.repo.set_setting("big_order_limit", "10000")
    _, c = card_for(p, "order", "create_order", {"customer_id": "C-002", "items": [{"sku": "UREA-50", "qty": 20}]}, role="clerk", user="Bilal Hussain")
    assert c["needs_role"] == "owner" and c["approver"]["text"] == "The owner must approve"
    assert [w["code"] for w in c["warnings"]] == ["big_order"] and "Rs 10,000" in c["warnings"][0]["text"]


def test_order_card_warns_over_credit_limit_and_short_stock(p):
    _, c = card_for(p, "order", "create_order", {"customer_id": "C-010", "items": [{"sku": "SEED-MAIZE", "qty": 30}, {"sku": "DRIP-100", "qty": 100000}]}, role="clerk", user="Bilal Hussain")
    codes = [w["code"] for w in c["warnings"]]
    assert "over_credit" in codes and "short_stock" in codes
    over = next(w for w in c["warnings"] if w["code"] == "over_credit")
    assert "New Kisan Dost" in over["text"] and "Rs 150,000" in over["text"]
    short = next(w for w in c["warnings"] if w["code"] == "short_stock")
    assert "Drip Line 100m" in short["text"] and "needs 100,000" in short["text"]


def test_confirm_order_card(p):
    o = _draft(p); now = p.repo.outstanding("C-002")
    _, c = card_for(p, "order", "confirm_order", {"order_id": o.order_id}, role="clerk", user="Bilal Hussain")
    assert c["title"] == "Confirm order for Chaudhry Farms" and c["total"] == o.total == 108250.0
    assert o.order_id in c["effect"] and f"owes {rs(now)} now, {rs(now + o.total)} once delivered" in c["effect"]
    assert [ln["name"] for ln in c["lines"]] == ["Urea 50kg", "DAP 50kg"]


def test_confirm_over_limit_card_is_the_owners_with_the_reason(p):
    from munshi.domain.repository.base import CreditHoldError
    with pytest.raises(CreditHoldError) as held:                 # saved as a draft on credit hold
        p.repo.create_order("C-010", [{"sku": "SEED-MAIZE", "qty": 30}], "test", "", "test")
    oid = re.search(r"ORD-[0-9A-F]+", str(held.value)).group(0)
    _, c = card_for(p, "order", "confirm_order", {"order_id": oid}, role="clerk", user="Bilal Hussain")
    assert c["needs_role"] == "owner" and c["approver"]["text"] == "The owner must approve"
    assert [w["code"] for w in c["warnings"]] == ["over_credit"]


def test_cancel_order_card_releases_reserved_stock(p):
    o = _allocated(p)
    _, c = card_for(p, "order", "cancel_order", {"order_id": o.order_id, "reason": "customer changed mind"}, role="clerk", user="Bilal Hussain")
    assert c["title"] == "Cancel order for Chaudhry Farms" and c["total"] == o.total
    assert "released" in c["effect"] and "Multan Godown" in c["effect"]
    assert c["facts"] == [{"key": "reason", "value": "customer changed mind"}]


def test_allocate_order_card_shows_available_stock_before_and_after(p):
    o = _confirmed(p, items=(("UREA-50", 20),))
    avail = p.repo.get_stock("WH-MULTAN", "UREA-50").available
    _, c = card_for(p, "godown", "allocate_order", {"order_id": o.order_id, "warehouse_id": "WH-MULTAN"}, role="clerk", user="Bilal Hussain")
    assert c["title"] == "Reserve stock for Chaudhry Farms's order"
    assert f"Urea 50kg {avail} → {avail - 20}" in c["effect"] and "Multan Godown" in c["effect"]
    assert c["effect_vars"]["changes"] == [{"name": "Urea 50kg", "before": avail, "after": avail - 20}]


def test_create_dispatch_plan_card(p):
    o = _allocated(p)
    _, c = card_for(p, "godown", "create_dispatch_plan", {"route_id": "R-MULTAN-N", "vehicle_id": "V-01", "order_ids": [o.order_id]}, role="clerk", user="Bilal Hussain")
    assert c["title"] == "Plan delivery: Multan North on MNK-4521"
    assert c["lines"][0]["name"] == "Chaudhry Farms" and c["lines"][0]["qty"] == 2 and c["total"] == o.total
    assert "1 order(s), 2 units on a vehicle that holds 120" in c["effect"]


def test_approve_dispatch_plan_card_says_stock_leaves_the_godown(p):
    o = _allocated(p)
    plan = p.repo.create_dispatch_plan(today_iso(), "R-MULTAN-N", "V-01", [o.order_id], "test")
    on_hand = p.repo.get_stock("WH-MULTAN", "UREA-50").on_hand
    _, c = card_for(p, "godown", "approve_dispatch_plan", {"plan_id": plan.plan_id}, role="clerk", user="Bilal Hussain")
    assert c["title"] == "Load MNK-4521 for Multan North"
    assert f"Stock leaves Multan Godown: Urea 50kg {on_hand} → {on_hand - 2}" in c["effect"] and "1 customer(s) get a delivery code" in c["effect"]


def test_adjust_stock_card_is_owner_tier_with_before_after_and_cost(p):
    on_hand = p.repo.get_stock("WH-MULTAN", "UREA-50").on_hand
    _, c = card_for(p, "godown", "adjust_stock", {"warehouse_id": "WH-MULTAN", "sku": "UREA-50", "delta": -20, "reason": "damaged in rain"})
    assert c["tier"] == "high_risk" and c["needs_role"] == "owner"
    assert c["title"] == "Write off 20 Urea 50kg at Multan Godown"
    assert f"Urea 50kg at Multan Godown goes {on_hand} → {on_hand - 20}" in c["effect"] and "at cost" in c["effect"]
    # writing off more than is there is flagged before anyone approves it
    _, c = card_for(p, "godown", "adjust_stock", {"warehouse_id": "WH-MULTAN", "sku": "UREA-50", "delta": -(on_hand + 1), "reason": "x"}, thread="t2")
    assert [w["code"] for w in c["warnings"]] == ["below_zero"]


def test_transfer_stock_card(p):
    a, b = p.repo.get_stock("WH-MULTAN", "UREA-50").on_hand, p.repo.get_stock("WH-VEHARI", "UREA-50").on_hand
    _, c = card_for(p, "godown", "transfer_stock", {"from_warehouse": "WH-MULTAN", "to_warehouse": "WH-VEHARI", "sku": "UREA-50", "qty": 20}, role="clerk", user="Bilal Hussain")
    assert c["title"] == "Move 20 Urea 50kg from Multan Godown to Vehari Godown"
    assert f"Multan Godown {a} → {a - 20}, Vehari Godown {b} → {b + 20}" in c["effect"]


def test_record_deposit_card_shows_the_shortfall(p):
    plan, o = _loaded_plan(p)
    stop = p.repo.list_stops(plan.plan_id)[0]
    p.repo.close_stop(stop.stop_id, [{"sku": "UREA-50", "qty": 2}], [], 7700, p.repo.get_stop(stop.stop_id).otp, "driver")
    _, c = card_for(p, "hisaab", "record_deposit", {"plan_id": plan.plan_id, "amount_counted": 5000}, role="clerk", user="Bilal Hussain")
    assert c["title"] == "Record cash hand-in for Multan North (MNK-4521)"
    assert c["effect"].startswith("Counted Rs 5,000 against Rs 7,700 expected: short by Rs 2,700")
    assert [w["code"] for w in c["warnings"]] == ["cash_short"]


def test_record_payment_card(p):
    now = p.repo.outstanding("C-002")
    _, c = card_for(p, "hisaab", "record_payment", {"customer_id": "C-002", "amount": 20000, "method": "jazzcash"}, role="clerk", user="Bilal Hussain")
    assert c["title"] == "Record Rs 20,000 received from Chaudhry Farms" and c["total"] == 20000.0
    assert c["effect"] == f"Chaudhry Farms will owe {rs(now - 20000)} (now {rs(now)})."
    assert c["facts"] == [{"key": "method", "value": {"k": "jazzcash"}}]


def test_record_expense_card(p):
    _, c = card_for(p, "hisaab", "record_expense", {"category": "fuel", "amount": 5000, "note": "V-01 diesel"}, role="clerk", user="Bilal Hussain")
    assert c["title"] == "Record Rs 5,000 fuel expense" and c["effect"] == "Rs 5,000 goes out as a fuel expense, paid by cash."


def test_credit_note_card(p):
    now = p.repo.outstanding("C-005")
    _, c = card_for(p, "hisaab", "credit_note", {"customer_id": "C-005", "amount": 5000, "reason": "damaged bags"})
    assert c["tier"] == "high_risk" and c["title"] == "Credit note of Rs 5,000 to Rana Brothers"
    assert c["effect"] == f"Rana Brothers will owe {rs(now - 5000)} (now {rs(now)}). Reported revenue drops by Rs 5,000."


def test_record_purchase_card(p):
    bal, on_hand = p.repo.supplier_balance("S-001"), p.repo.get_stock("WH-MULTAN", "UREA-50").on_hand
    _, c = card_for(p, "khareed", "record_purchase", {"supplier_id": "S-001", "items": [{"sku": "UREA-50", "qty": 100, "unit_cost": 3600}], "invoice_ref": "FF-99"},
                    role="clerk", user="Bilal Hussain")
    assert c["title"] == "Receive stock from Fauji Fertilizer (Multan depot)" and c["total"] == 360000.0
    assert c["lines"] == [{"sku": "UREA-50", "name": "Urea 50kg", "qty": 100, "unit": "bag", "unit_price": 3600.0, "line_total": 360000.0}]
    assert f"Urea 50kg {on_hand} → {on_hand + 100}" in c["effect"] and f"owe Fauji Fertilizer (Multan depot) {rs(bal + 360000)} (now {rs(bal)})" in c["effect"]


def test_pay_supplier_card(p):
    bal = p.repo.supplier_balance("S-001")
    _, c = card_for(p, "khareed", "pay_supplier", {"supplier_id": "S-001", "amount": 100000, "method": "bank"})
    assert c["tier"] == "high_risk" and c["title"] == "Pay Fauji Fertilizer (Multan depot) Rs 100,000"
    assert c["effect"] == f"We will owe Fauji Fertilizer (Multan depot) {rs(bal - 100000)} (now {rs(bal)}). Paid by bank."


def test_reminder_cards(p):
    ag = p.repo.aging(customer_id="C-009")[0]
    _, c = card_for(p, "wasooli", "draft_reminder", {"customer_id": "C-009"}, role="clerk", user="Bilal Hussain")
    assert c["title"] == "Draft a final payment reminder to Haji Sons" and rs(ag["balance"]) in c["effect"]
    _, c = card_for(p, "wasooli", "draft_due_reminders", {"min_days_overdue": 30}, role="clerk", user="Bilal Hussain")
    assert c["title"] == "Draft reminders for everyone 30+ days overdue" and "Haji Sons" in [ln["name"] for ln in c["lines"]]
    assert c["total"] == sum(ln["line_total"] for ln in c["lines"])
    rid = p.ops.draft_reminder("C-009")["reminder_id"]
    _, c = card_for(p, "wasooli", "send_reminder", {"reminder_id": rid}, role="clerk", user="Bilal Hussain")
    assert c["title"] == "Send payment reminder to Haji Sons" and "0300-1111009" in c["effect"] and c["quote"].startswith("Haji Sons")


def test_log_promise_card(p):
    _, c = card_for(p, "wasooli", "log_promise", {"customer_id": "C-009", "amount": 50000, "promised_date": "2026-10-01"}, role="clerk", user="Bilal Hussain")
    assert c["title"] == "Log promise: Haji Sons pays Rs 50,000 by 2026-10-01" and c["effect"].startswith("No money moves.")


# ------------------------------------------------------------------ the four reversal tools
def test_reverse_ledger_entry_card(p):
    e = p.repo.record_payment("C-002", 20000, "cheque", "chq 1", "hisaab_munshi", "clerk")
    now = p.repo.outstanding("C-002")
    _, c = card_for(p, "hisaab", "reverse_ledger_entry", {"entry_id": e.entry_id, "reason": "cheque bounced"})
    assert c["tier"] == "high_risk" and c["title"] == f"Reverse Receipt {e.entry_id} for Chaudhry Farms"
    assert c["effect"] == f"Cancels Receipt {e.entry_id} (Rs 20,000): Chaudhry Farms will owe {rs(now + 20000)} (now {rs(now)})."
    assert c["warnings"] == [] and c["facts"] == [{"key": "reason", "value": "cheque bounced"}]
    # already reversed: said on the card, before anyone approves it
    p.repo.reverse_ledger_entry(e.entry_id, "bounced", "test", "owner")
    _, c = card_for(p, "hisaab", "reverse_ledger_entry", {"entry_id": e.entry_id, "reason": "again"}, thread="t2")
    assert [w["code"] for w in c["warnings"]] == ["already_reversed"]


def test_reverse_expense_card(p):
    x = p.repo.record_expense("fuel", 55000, "diesel", "cash", "", "test")
    _, c = card_for(p, "hisaab", "reverse_expense", {"expense_id": x.expense_id, "reason": "typed 55000 instead of 5500"})
    assert c["title"] == f"Reverse fuel expense {x.expense_id}" and c["total"] == 55000.0
    assert c["effect"].startswith(f"Cancels expense {x.expense_id} (Rs 55,000 fuel, {x.expense_date})")


def test_reverse_purchase_card(p):
    pur = p.repo.record_purchase("S-001", "WH-MULTAN", [{"sku": "UREA-50", "qty": 50, "unit_cost": 3600}], "FF-9", 0, "khareed_munshi", "clerk")
    bal, on_hand = p.repo.supplier_balance("S-001"), p.repo.get_stock("WH-MULTAN", "UREA-50").on_hand
    _, c = card_for(p, "khareed", "reverse_purchase", {"purchase_id": pur.purchase_id, "reason": "wrong bill"})
    assert c["title"] == f"Reverse purchase {pur.purchase_id} from Fauji Fertilizer (Multan depot)" and c["total"] == 180000.0
    assert f"Urea 50kg {on_hand} → {on_hand - 50}" in c["effect"] and f"{rs(bal - 180000)} (now {rs(bal)})" in c["effect"]


def test_reverse_supplier_entry_card(p):
    e = p.repo.pay_supplier("S-001", 25000, "cheque", "chq 9", "khareed_munshi", "owner")
    bal = p.repo.supplier_balance("S-001")
    _, c = card_for(p, "khareed", "reverse_supplier_entry", {"entry_id": e.entry_id, "reason": "cheque bounced"})
    assert c["title"] == f"Reverse Payment {e.entry_id} for Fauji Fertilizer (Multan depot)"
    assert c["effect"] == f"Cancels Payment {e.entry_id} (Rs 25,000): we will owe Fauji Fertilizer (Multan depot) {rs(bal + 25000)} (now {rs(bal)})."


def test_every_gated_tool_has_a_card_builder():
    from munshi.platform import CardBuilder
    assert [t for t in tools_requiring_approval() if not hasattr(CardBuilder, "_c_" + t)] == []


# ================================================================== blank / unknown ids
@pytest.mark.parametrize("spec,tool,args,named", [
    ("hisaab", "reverse_ledger_entry", {"entry_id": "", "reason": "bounced"}, "entry to reverse"),
    ("hisaab", "reverse_ledger_entry", {"reason": "bounced"}, "entry to reverse"),                  # left out altogether
    ("hisaab", "reverse_expense", {"expense_id": "  ", "reason": "typo"}, "expense to reverse"),
    ("khareed", "reverse_purchase", {"reason": "wrong"}, "purchase to reverse"),
    ("khareed", "reverse_supplier_entry", {"entry_id": "", "reason": "bounced"}, "entry to reverse"),
])
def test_a_blank_reversal_id_raises_no_card(p, spec, tool, args, named):
    r = ask(p, spec, tool, args)
    assert r.pending is None and p.pending == {}
    assert f"no {named} was named" in r.text and "Nothing was done" in r.text


def test_a_well_formed_unknown_reversal_id_still_gets_a_card_that_says_so(p):
    """Like eval step cancel_unknown_order: a well-formed id is not checked for existence before the card."""
    _, c = card_for(p, "hisaab", "reverse_ledger_entry", {"entry_id": "RCP-2099-999999", "reason": "x"})
    assert c["title"] == "Reverse khata entry RCP-2099-999999" and [w["code"] for w in c["warnings"]] == ["not_found"]


# ================================================================== never raises, never writes
JUNK = [{}, {"order_id": 12, "customer_id": None, "items": "junk", "amount": "abc", "qty": None, "delta": "x", "plan_id": ["a"], "order_ids": "ORD-1",
             "entry_id": {"a": 1}, "expense_id": 3.5, "purchase_id": "PUR-NOPE", "supplier_id": "S-NOPE", "reminder_id": "REM-NOPE", "sku": "NOPE", "warehouse_id": "WH-NOPE"},
        {"customer_id": "C-NOPE", "order_id": "ORD-NOPE", "items": [{"sku": "NOPE", "qty": "many"}], "amount": 1e30, "promised_date": None}]


@pytest.mark.parametrize("tool", tools_requiring_approval())
def test_card_building_never_raises_and_never_writes(p, tool):
    before = p.repo._conn.total_changes
    for args in JUNK:
        pa = PendingApproval("X", "t", "order", tool, args, risk_of(tool).value, approver_for(tool), "clerk", "Bilal")
        c = p.card(pa)
        assert c["title"] and c["tool"] == tool and isinstance(c["warnings"], list)
        json.dumps(c)
    assert p.repo._conn.total_changes == before


def test_card_survives_an_entity_deleted_after_the_request(p):
    o = _allocated(p)
    r, _ = card_for(p, "godown", "create_dispatch_plan", {"route_id": "R-MULTAN-N", "vehicle_id": "V-01", "order_ids": [o.order_id]}, role="clerk", user="Bilal Hussain")
    p.repo.delete_vehicle("V-01")
    c = p.card(r.pending)
    assert c["title"] == "Plan delivery: Multan North on V-01" and "not_found" in [w["code"] for w in c["warnings"]]
    with p.repo._tx() as cur:                                 # an order row gone from under a card
        cur.execute("DELETE FROM orders WHERE order_id=?", (o.order_id,))
    c = p.card(r.pending)
    assert c["title"] and [w["code"] for w in c["warnings"]].count("not_found") == 2


# ================================================================== a message while a card waits
class _Spy:
    """Counts invocations of a specialist's graph (a paused graph must never be invoked with a new message)."""

    def __init__(self, agent):
        self._agent, self.calls = agent, 0

    def invoke(self, *a, **kw):
        self.calls += 1
        return self._agent.invoke(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._agent, name)


def test_a_write_while_a_card_waits_returns_that_card_and_leaves_the_graph_alone(p):
    first = p.handle_message("t", "clerk", "Chaudhry Farms ko 20 urea aur 5 dap bhej do", user="Bilal Hussain")
    spy = p.specialists["order"].agent = _Spy(p.specialists["order"].agent)
    r = p.handle_message("t", "clerk", "Rana Brothers ko 3 zinc bhej do", user="Bilal Hussain")
    assert r.pending is None and r.waiting is not None and r.waiting.approval_id == first.pending.approval_id
    assert r.text.startswith("Still waiting for approval: Create order for Chaudhry Farms, Rs 108,250 — another clerk or the owner needs to approve")
    assert spy.calls == 0 and list(p.pending) == [first.pending.approval_id]
    # the held message didn't disturb the card: it still approves and does what it said
    n = len(p.repo.list_orders())
    p.resolve(first.pending.approval_id, True, "owner", user="Sultan Ahmed")
    assert len(p.repo.list_orders()) == n + 1 and p.repo.list_orders()[0].total == 108250.0


def test_a_read_only_question_while_a_card_waits_goes_to_the_report_munshi(p):
    first = p.handle_message("t", "clerk", "Chaudhry Farms ko 20 urea bhej do", user="Bilal Hussain")
    order = p.specialists["order"].agent = _Spy(p.specialists["order"].agent)
    audit = len(p.repo.audit_log(1000))
    for q in ("what is Chaudhry Farms balance?", "Chaudhry Farms ka khata kitna hai?", "dap kitna hai?", "top customers this month?"):
        r = p.handle_message("t", "clerk", q, user="Bilal Hussain")
        assert r.specialist == "report" and r.pending is None and r.waiting is None, q
    assert order.calls == 0 and list(p.pending) == [first.pending.approval_id]
    assert len(p.repo.audit_log(1000)) == audit                 # nothing written
    # the paused order graph is intact: approving it still creates exactly the order on the card
    p.resolve(first.pending.approval_id, True, "owner", user="Sultan Ahmed")
    assert p.repo.list_orders()[0].customer_id == "C-002"


@pytest.mark.parametrize("text", ["yes confirm it", "cancel that and book 5 dap", "Rana Brothers ko 3 zinc bhej do", "ok confirm the order"])
def test_anything_that_might_be_a_write_stays_held(p, text):
    p.handle_message("t", "clerk", "Chaudhry Farms ko 20 urea bhej do", user="Bilal Hussain")
    r = p.handle_message("t", "clerk", text, user="Bilal Hussain")
    assert r.waiting is not None and r.specialist == "order"


def test_a_salesman_has_no_report_munshi_so_his_question_stays_held_with_the_card(p):
    p.handle_message("s", "salesman", "Rana Brothers ko 5 dap bhej do", user="Imran Khan")
    r = p.handle_message("s", "salesman", "Rana Brothers ka balance kitna hai?", user="Imran Khan")
    assert r.waiting is not None and r.waiting.tool == "create_order"


# ================================================================== over HTTP: the viewer's buttons
DEMO = {"owner": ("0300-0000001", "1111"), "clerk": ("0300-0000002", "2222"), "salesman": ("0300-0000004", "4444")}


@pytest.fixture
def api():
    c = TestClient(build_app(in_memory=True, demo=True, scheduler=False))
    h = {}
    for role, (phone, pin) in DEMO.items():
        h[role] = {"X-Session": c.post("/api/session", json={"phone": phone, "pin": pin}).json()["token"]}
    assert c.post("/api/staff", json={"name": "Sana Malik", "phone": "0301-2223334", "role": "clerk", "pin": "2580"}, headers=h["owner"]).status_code == 201
    h["clerk2"] = {"X-Session": c.post("/api/session", json={"phone": "0301-2223334", "pin": "2580"}).json()["token"]}
    return c, h


def test_requester_sees_why_not_instead_of_a_dead_approve_button(api):
    c, h = api
    r = c.post("/api/chat", json={"thread_id": "t", "text": "Chaudhry Farms ko 20 urea aur 5 dap bhej do"}, headers=h["clerk"]).json()
    pend = r["pending"]
    assert pend["card"]["title"] == "Create order for Chaudhry Farms" and pend["card"]["total"] == 108250.0
    assert pend["summary"] and pend["tool"] == "create_order" and pend["still_waiting"] is False       # the old fields are all still there
    assert pend["can_approve"] is False and pend["blocked_code"] == "own_request"
    assert pend["blocked_reason"] == "You asked for this: waiting for the owner (or another clerk) to approve."
    assert pend["can_withdraw"] is True and pend["can_reject"] is True and pend["is_requester"] is True
    # ... and the button it would have shown really would have been refused
    aid = pend["approval_id"]
    assert c.post(f"/api/approvals/{aid}", json={"approve": True}, headers=h["clerk"]).status_code == 403

    own = c.get("/api/approvals", headers=h["owner"]).json()[0]
    assert own["approval_id"] == aid and own["can_approve"] is True and own["blocked_reason"] is None and own["can_withdraw"] is False
    assert own["card"] == pend["card"]
    other = c.get("/api/approvals", headers=h["clerk2"]).json()[0]
    assert other["can_approve"] is True
    # the requester's badge doesn't count their own request; the owner's does
    assert c.get("/api/badge", headers=h["clerk"]).json()["approvals"] == 0
    assert c.get("/api/badge", headers=h["owner"]).json()["approvals"] == 1
    assert c.post(f"/api/approvals/{aid}", json={"approve": True}, headers=h["owner"]).status_code == 200


def test_owner_tier_card_tells_a_clerk_it_is_the_owners(api):
    c, h = api
    r = c.post("/api/chat", json={"thread_id": "o", "text": "pay Fauji 100000 by bank"}, headers=h["owner"]).json()
    aid = r["pending"]["approval_id"]
    assert r["pending"]["can_approve"] is True and r["pending"]["card"]["tier"] == "high_risk"          # an owner may clear their own
    k = next(x for x in c.get("/api/approvals", headers=h["clerk"]).json() if x["approval_id"] == aid)
    assert k["can_approve"] is False and k["blocked_code"] == "needs_owner" and k["can_withdraw"] is False


def test_requester_can_withdraw(api):
    c, h = api
    aid = c.post("/api/chat", json={"thread_id": "t", "text": "expense diesel 5000 for V-01"}, headers=h["clerk"]).json()["pending"]["approval_id"]
    assert c.post(f"/api/approvals/{aid}", json={"approve": False, "note": "withdrawn by the requester"}, headers=h["clerk"]).status_code == 200
    assert c.get("/api/approvals", headers=h["owner"]).json() == []


def test_salesman_sees_waiting_for_the_office(api):
    c, h = api
    p = c.post("/api/chat", json={"thread_id": "s", "text": "Rana Brothers ko 5 dap bhej do"}, headers=h["salesman"]).json()["pending"]
    assert p["can_approve"] is False and p["blocked_code"] == "not_approver" and p["can_reject"] is False and p["can_withdraw"] is False


def test_http_reply_while_a_card_waits_carries_the_card(api):
    c, h = api
    first = c.post("/api/chat", json={"thread_id": "t", "text": "Chaudhry Farms ko 20 urea bhej do"}, headers=h["clerk"]).json()["pending"]
    r = c.post("/api/chat", json={"thread_id": "t", "text": "Rana Brothers ko 3 zinc bhej do"}, headers=h["clerk"]).json()
    assert r["pending"]["approval_id"] == first["approval_id"] and r["pending"]["still_waiting"] is True
    assert r["pending"]["card"]["title"] == "Create order for Chaudhry Farms" and r["pending"]["can_approve"] is False
    assert "Still waiting for approval" in r["text"]
    q = c.post("/api/chat", json={"thread_id": "t", "text": "what is Chaudhry Farms balance?"}, headers=h["clerk"]).json()
    assert q["specialist"] == "report" and q["pending"] is None
    assert len(c.get("/api/approvals", headers=h["owner"]).json()) == 1


# ================================================================== the app's templates match the server's
def _js_keys(block: str) -> dict[str, str]:
    """'ap.<key>': '<template>' pairs in one language block of i18n.js."""
    return {m.group(1): m.group(3) for m in re.finditer(r"""['"](ap\.[a-z_]+)['"]:\s*(['"])(.*?)\2,""", block)}


def test_i18n_has_every_card_template_in_both_languages_with_the_same_placeholders():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "src/munshi/web/static/i18n.js").read_text(encoding="utf-8")
    en_block, ur_block = src.split("\n  ur: {", 1)
    en, ur = _js_keys(en_block), _js_keys(ur_block)
    holes = lambda s: sorted(set(re.findall(r"\{(\w+)\}", s)))           # noqa: E731
    for key, text in CARD_EN.items():
        assert en.get("ap." + key) == text, key                        # English is the server's sentence, word for word
        assert "ap." + key in ur, key
        assert holes(ur["ap." + key]) == holes(text), key
    for w, text in CARD_WORDS_EN.items():
        assert f"k_{w}: {json.dumps(text)}" in en_block, w
