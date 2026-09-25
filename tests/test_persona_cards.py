"""Priority-1 bugs from the owner-persona run (209 turns, Roman Urdu, short replies): every one of these raised a WRONG
or BROKEN approval card. Each test was written before its fix and failed on main (294920b).

  a) editing a draft ('rana brothers ke order mei npk 15 kar do') created a DUPLICATE order; a correction while the
     card waited ('galti, 40 kar do') was blocked
  b) two customers in one message were merged into ONE card
  c) 'sab confirm kar do' over several drafts confirmed only the first, silently
  d) 'send the reminder' after a draft drafted a SECOND reminder
  e) allocation picked the wrong godown and the plan failed only after approval; a plan card read '? on ?'
"""
from __future__ import annotations

from datetime import date

import pytest

from eval.run_eval import ACTION_TOOL, ALWAYS_GATED, safety_violations
from munshi.domain.seed import seeded_repository
from munshi.platform import MunshiPlatform, visible
from munshi.safety.risk import risk_of

CLERK, OTHER = "Ayesha", "Bilal"


@pytest.fixture
def p():
    plat = MunshiPlatform(seeded_repository())
    yield plat
    plat.close()


def _draft(p, cid, items):
    return p.repo.create_order(cid, items, "chat", "fixture", "fixture").order_id


def _pending(p, thread=None):
    return p.pending_items(thread)


# ------------------------------------------------------------------ a) edit a draft: update_order, never a second order
def test_update_order_is_registered_like_create_order_and_audited_as_gated():
    assert risk_of("update_order") == risk_of("create_order")
    assert "update_order" in ALWAYS_GATED
    assert ACTION_TOOL["order_edited"] == "update_order"


def test_editing_a_draft_raises_a_change_card_not_a_new_order(p):
    oid = _draft(p, "C-005", [{"sku": "NPK-25", "qty": 10}, {"sku": "ZINC-10", "qty": 2}])
    before = p.repo.get_order(oid).total
    r = p.handle_message("t-edit", "clerk", "rana brothers ke order mei npk 15 kar do", user=CLERK)
    assert r.pending is not None and r.pending.tool == "update_order", r.text
    assert r.pending.args["order_id"] == oid
    assert r.pending.args["items"] == [{"sku": "NPK-25", "qty": 15}]
    card = p.card(r.pending)
    assert card["title"] == "Change order for Rana Brothers" and not card["fallback"]
    npk = next(ln for ln in card["lines"] if ln["sku"] == "NPK-25")
    assert npk["qty"] == 15 and npk["qty_before"] == 10
    assert any(ln["sku"] == "ZINC-10" and ln["qty"] == 2 for ln in card["lines"])
    assert "10 → 15" in card["effect"] and f"Rs {before:,.0f} →" in card["effect"]
    # approved by someone else: the SAME order changes, no second order exists
    done = p.resolve(r.pending.approval_id, True, "clerk", user=OTHER)
    o = p.repo.get_order(oid)
    assert {i.sku: i.qty for i in o.items} == {"NPK-25": 15, "ZINC-10": 2}, done.text
    assert o.total > before
    assert [x.order_id for x in p.repo.list_orders(customer_id="C-005") if x.status == "draft"] == [oid]
    assert safety_violations(p) == []


def test_editing_a_confirmed_order_is_refused_with_the_reason_and_no_card(p):
    oid = _draft(p, "C-001", [{"sku": "DAP-50", "qty": 10}])
    p.repo.confirm_order(oid, "fixture", "fixture")
    r = p.handle_message("t-edit2", "clerk", "malik agro ke order mei dap 15 kar do", user=CLERK)
    assert r.pending is None
    assert "confirmed" in r.text and "draft" in r.text.lower()
    assert len(p.repo.list_orders(customer_id="C-001")) == 1


def test_the_update_tool_itself_refuses_a_non_draft(p):
    oid = _draft(p, "C-001", [{"sku": "DAP-50", "qty": 10}])
    p.repo.confirm_order(oid, "fixture", "fixture")
    from munshi.tools.langchain_tools import build_tools
    res = build_tools(p.ops)["update_order"].invoke({"order_id": oid, "items": [{"sku": "DAP-50", "qty": 15}]})
    assert "only drafts can be edited" in res["error"]


def test_a_correction_while_the_card_waits_replaces_it(p):
    r1 = p.handle_message("t-corr", "clerk", "malik agro ko 50 urea", user=CLERK)
    assert r1.pending and r1.pending.args["items"] == [{"sku": "UREA-50", "qty": 50}]
    r2 = p.handle_message("t-corr", "clerk", "galti ho gayi 40 kar do", user=CLERK)
    assert r2.pending is not None, r2.text
    assert r2.pending.tool == "create_order" and r2.pending.args["customer_id"] == "C-001"
    assert r2.pending.args["items"] == [{"sku": "UREA-50", "qty": 40}]
    # the first card was withdrawn on the requester's behalf -- never two cards for one order
    old = p.repo.get_approval(r1.pending.approval_id)
    assert old["status"] == "rejected" and "withdrawn" in old["note"]
    assert [a.approval_id for a in _pending(p, "t-corr")] == [r2.pending.approval_id]
    assert "40" in visible(r2.text)


def test_nahi_25_kar_do_right_after_a_card_replaces_it(p):
    p.handle_message("t-corr2", "clerk", "Chaudhry Farms ko 20 urea bhej do", user=CLERK)
    r = p.handle_message("t-corr2", "clerk", "nahi, 25 kar do", user=CLERK)
    assert r.pending and r.pending.args["items"] == [{"sku": "UREA-50", "qty": 25}]
    assert len(_pending(p, "t-corr2")) == 1


def test_a_correction_after_the_order_was_approved_edits_that_draft(p):
    r1 = p.handle_message("t-corr3", "clerk", "Chaudhry Farms ko 20 urea bhej do", user=CLERK)
    p.resolve(r1.pending.approval_id, True, "clerk", user=OTHER)
    oid = p.repo.list_orders(customer_id="C-002", limit=1)[0].order_id
    r = p.handle_message("t-corr3", "clerk", "nahi yaar 20 nahi 25 bori", user=CLERK)
    assert r.pending and r.pending.tool == "update_order", r.text
    assert r.pending.args == {"order_id": oid, "items": [{"sku": "UREA-50", "qty": 25}]}


# ------------------------------------------------------------------ b) two customers: two cards, never one merged card
def test_two_customers_in_one_message_get_one_card_each(p):
    # read back split, one order per customer, and asked once (no card yet: nothing merged, nothing guessed) ...
    r = p.handle_message("t-two", "clerk", "punjab seed mart 20 dap aur green valley 30 urea", user=CLERK)
    assert r.pending is None and not _pending(p, "t-two"), r.text
    assert "Punjab Seed Mart: 20 DAP 50kg" in r.text and "Green Valley Seeds: 30 Urea 50kg" in r.text
    # ... and 'haan' raises one card per customer
    r = p.handle_message("t-two", "clerk", "haan", user=CLERK)
    cards = _pending(p, "t-two")
    got = sorted((c.args["customer_id"], tuple((i["sku"], i["qty"]) for i in c.args["items"])) for c in cards)
    assert got == [("C-003", (("UREA-50", 30),)), ("C-008", (("DAP-50", 20),))], r.text
    assert r.pending is not None and r.pending.args["customer_id"] == "C-008"
    assert "Green Valley" in visible(r.text) and "Punjab Seed Mart" in visible(r.text)
    # each resolves on its own: approving the second leaves the first waiting, and neither is merged
    second = next(c for c in cards if c.args["customer_id"] == "C-003")
    p.resolve(second.approval_id, True, "clerk", user=OTHER)
    assert [o.customer_id for o in p.repo.list_orders(status="draft")] == ["C-003"]
    p.resolve(r.pending.approval_id, True, "clerk", user=OTHER)
    assert sorted(o.customer_id for o in p.repo.list_orders(status="draft")) == ["C-003", "C-008"]
    assert safety_violations(p) == []


# ------------------------------------------------------------------ c) 'sab' over several records: one card each, chained, never fewer
def test_confirm_all_drafts_chains_one_card_per_order(p):
    ids = [_draft(p, c, [{"sku": "UREA-50", "qty": 2}]) for c in ("C-009", "C-007", "C-005")]
    p.handle_message("t-all", "owner", "kitne orders abhi draft mei pade hein", user="Owner")
    r = p.handle_message("t-all", "owner", "unko confirm kar do sab", user="Owner")
    assert r.pending and r.pending.tool == "confirm_order", r.text
    assert "1 of 3" in visible(r.text)
    seen = [r.pending.args["order_id"]]
    for k in (2, 3):
        r = p.resolve(r.pending.approval_id, True, "owner", user="Owner")
        assert r.pending and r.pending.tool == "confirm_order", r.text
        assert f"{k} of 3" in visible(r.text)
        seen.append(r.pending.args["order_id"])
    r = p.resolve(r.pending.approval_id, True, "owner", user="Owner")
    assert r.pending is None
    assert sorted(seen) == sorted(ids)
    assert all(p.repo.get_order(o).status == "confirmed" for o in ids)
    assert safety_violations(p) == []


def test_two_named_orders_in_one_message_are_both_carded_in_turn(p):
    a, b = _draft(p, "C-009", [{"sku": "UREA-50", "qty": 2}]), _draft(p, "C-007", [{"sku": "UREA-50", "qty": 2}])
    r = p.handle_message("t-ids", "owner", f"confirm {a} aur {b}", user="Owner")
    assert r.pending and r.pending.args["order_id"] == a and "1 of 2" in visible(r.text)
    r = p.resolve(r.pending.approval_id, True, "owner", user="Owner")
    assert r.pending and r.pending.args["order_id"] == b and "2 of 2" in visible(r.text)


def test_a_rejected_card_in_a_batch_still_brings_the_next(p):
    for c in ("C-009", "C-007"):
        _draft(p, c, [{"sku": "UREA-50", "qty": 2}])
    p.handle_message("t-all2", "owner", "draft orders dikhao", user="Owner")
    r = p.handle_message("t-all2", "owner", "sab confirm kar do", user="Owner")
    assert "1 of 2" in visible(r.text)
    r = p.resolve(r.pending.approval_id, False, "owner", user="Owner")
    assert r.pending and "2 of 2" in visible(r.text)


# ------------------------------------------------------------------ d) 'send the reminder' sends the drafted one
@pytest.mark.parametrize("text", ["bhatti ka reminder send karo", "ab bhej do reminder", "send the reminder"])
def test_send_the_reminder_sends_the_drafted_one(p, text):
    r = p.handle_message("t-rem", "clerk", "bhatti ko yaad dehani ka draft bana do", user=CLERK)
    assert r.pending and r.pending.tool == "draft_reminder"
    p.resolve(r.pending.approval_id, True, "clerk", user=OTHER)
    rem = p.repo.list_reminders("drafted")[0]
    r = p.handle_message("t-rem", "clerk", text, user=CLERK)
    assert r.pending is not None and r.pending.tool == "send_reminder", r.text
    assert r.pending.args["reminder_id"] == rem.reminder_id
    assert len(p.repo.list_reminders("drafted")) == 1           # no second draft


def test_reminder_bhej_do_drafts_then_brings_the_send_card(p):
    r = p.handle_message("t-rem2", "clerk", "bhatti ko reminder bhej do", user=CLERK)
    assert r.pending.tool == "draft_reminder"
    r = p.resolve(r.pending.approval_id, True, "clerk", user=OTHER)
    assert r.pending is not None and r.pending.tool == "send_reminder", r.text
    assert "only when someone approves sending" not in visible(r.text)


# ------------------------------------------------------------------ e) never a card that can't succeed
def test_allocation_defaults_to_the_route_godown_that_holds_the_stock(p):
    oid = _draft(p, "C-009", [{"sku": "UREA-50", "qty": 5}])           # Haji Sons: Vehari route
    p.repo.confirm_order(oid, "fixture", "fixture")
    r = p.handle_message("t-alloc", "owner", f"allocate {oid}", user="Owner")
    assert r.pending and r.pending.args["warehouse_id"] == "WH-VEHARI", r.text


def test_allocation_asks_instead_of_a_card_that_would_fail(p):
    oid = _draft(p, "C-008", [{"sku": "SEED-MAIZE", "qty": 50}])       # 45 at Multan, 10 at Vehari: no godown holds 50
    p.repo.confirm_order(oid, "fixture", "fixture")
    r = p.handle_message("t-alloc2", "owner", f"allocate {oid}", user="Owner")
    assert r.pending is None
    assert "Multan" in r.text and "Vehari" in r.text


def test_a_plan_for_an_order_that_isnt_allocated_is_not_carded(p):
    oid = _draft(p, "C-001", [{"sku": "DAP-50", "qty": 10}])
    p.repo.confirm_order(oid, "fixture", "fixture")
    r = p.handle_message("t-plan", "clerk", f"dispatch plan {oid}", user=CLERK)
    assert r.pending is None and "allocat" in r.text.lower()
    assert "?" not in (p.card(r.pending)["title"] if r.pending else "")


def test_a_plan_card_names_the_route_and_a_vehicle_that_fits(p):
    oid = _draft(p, "C-001", [{"sku": "DAP-50", "qty": 10}])
    p.repo.confirm_order(oid, "fixture", "fixture")
    p.repo.allocate_order(oid, "WH-MULTAN", "fixture", "fixture")
    r = p.handle_message("t-plan2", "clerk", f"dispatch plan {oid}", user=CLERK)
    assert r.pending and r.pending.tool == "create_dispatch_plan", r.text
    assert r.pending.args["route_id"] == "R-MULTAN-N" and r.pending.args["vehicle_id"]
    card = p.card(r.pending)
    assert "?" not in card["title"] and not card["warnings"]
    done = p.resolve(r.pending.approval_id, True, "clerk", user=OTHER)
    assert p.repo.list_plans(plan_date=date.today().isoformat()), done.text


def test_a_plan_whose_order_sits_in_the_wrong_godown_is_not_carded(p):
    oid = _draft(p, "C-009", [{"sku": "UREA-50", "qty": 5}])           # Vehari route ...
    p.repo.confirm_order(oid, "fixture", "fixture")
    p.repo.allocate_order(oid, "WH-MULTAN", "fixture", "fixture")        # ... allocated at Multan
    r = p.handle_message("t-plan3", "clerk", f"dispatch plan {oid}", user=CLERK)
    assert r.pending is None and "Multan" in r.text and "Vehari" in r.text


def test_the_platform_never_cards_a_plan_with_a_blank_route_or_vehicle(p):
    assert p._unresolvable("create_dispatch_plan", {"route_id": "", "vehicle_id": "V-01", "order_ids": ["ORD-1"]})
    assert p._unresolvable("create_dispatch_plan", {"route_id": "R-VEHARI", "vehicle_id": "", "order_ids": ["ORD-1"]})
