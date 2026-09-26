"""Persona round 2 (218 turns, Roman-Urdu owner style, real Gemini): the wrong cards and false claims it found, each
pinned here on the offline rules (the model side is pinned in tests/test_grounding.py).

  a) 'reverse / galat entry / bounce' + a payment reverses the matching receipt (or asks which) -- never a NEW payment
  b) a question never raises a write card; a reminder for everyone needs an explicit bulk instruction
  c) an owner-tier request from a clerk / salesman is passed on (notification) and said plainly -- tiers unchanged
  d) a route's plan carries EVERY order reserved for it, from the books; the guard rejects a model plan that leaves one out
  e) a correction / withdrawal finds the card it is about, not just the first one waiting"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from munshi.agents.guard import check_call, plan_orders
from munshi.domain.models import business_today
from munshi.llm import answers
from munshi.platform import MunshiPlatform, visible


@pytest.fixture
def p():
    return MunshiPlatform()


def _pay(p, text="chaudhry farms ne 20000 cash diye"):
    r = p.handle_message("setup", "clerk", text)
    assert r.pending and r.pending.tool == "record_payment", r.text
    out = p.resolve(r.pending.approval_id, True, "owner")
    return next(e for e in reversed(p.repo.ledger_for(r.pending.args["customer_id"])) if e.kind == "payment")


# ---------------------------------------------------------------- a) reversals
def test_reverse_by_customer_and_amount_reverses_that_receipt(p):
    e = _pay(p)
    r = p.handle_message("t", "owner", "chaudhry farms ki aaj wali 20000 cash payment reverse karo")
    assert r.pending and r.pending.tool == "reverse_ledger_entry" and r.pending.args["entry_id"] == e.entry_id
    assert r.pending.needs_role == "owner"
    p.resolve(r.pending.approval_id, False, "owner", note="test")
    for k, text in enumerate(("chaudhry farms ki 20000 wali entry galat thi, cancel kar do", "chaudhry farms ki payment reverse karo")):
        q = p.handle_message(f"t{k + 2}", "owner", text)
        assert q.pending and q.pending.tool == "reverse_ledger_entry" and q.pending.args["entry_id"] == e.entry_id, (text, q.text)
        p.resolve(q.pending.approval_id, False, "owner", note="test")


def test_several_receipts_ask_which_and_the_answer_reverses_it(p):
    _pay(p, "chaudhry farms ne 10000 jazzcash diye")
    e20 = _pay(p)
    r = p.handle_message("t", "owner", "chaudhry farms ki payment reverse karo")
    assert r.pending is None and "20,000" in r.text and "10,000" in r.text and "RCP-" not in r.text
    r2 = p.handle_message("t", "owner", "20000 wali")
    assert r2.pending and r2.pending.tool == "reverse_ledger_entry" and r2.pending.args["entry_id"] == e20.entry_id


def test_no_matching_receipt_is_said_and_never_becomes_a_payment(p):
    _pay(p)
    r = p.handle_message("t", "owner", "chaudhry farms ki 50000 wali payment reverse karo")
    assert r.pending is None and "Rs 50,000" in r.text and "nothing was done" in r.text


def test_a_correction_reverses_the_wrong_receipt_and_chains_the_right_amount(p):
    e = _pay(p)
    r = p.handle_message("t", "owner", "chaudhry farms ki 20000 wali payment galat thi, asal mei 12000 thi, theek kar do")
    assert r.pending and r.pending.tool == "reverse_ledger_entry" and r.pending.args["entry_id"] == e.entry_id and "12,000" in r.text
    nxt = p.resolve(r.pending.approval_id, True, "owner")
    assert nxt.pending and nxt.pending.tool == "record_payment" and float(nxt.pending.args["amount"]) == 12000
    assert p.repo.reversal_of_ledger(e.entry_id)


def test_a_clerks_reversal_is_passed_to_the_owner(p):
    _pay(p)
    before = len(p.repo.notifications("owner"))
    r = p.handle_message("t", "clerk", "yaar galti ho gayi, chaudhry farms ki cash payment 20000 nahi 12000 thi")
    assert r.pending is None and "owner" in r.text and "12,000" in r.text
    assert len(p.repo.notifications("owner")) == before + 1


# ---------------------------------------------------------------- b) questions never raise a write card
@pytest.mark.parametrize("text,tool", [("bhai scene kya he wasooli ka", "aging_report"), ("wasooli ka kya haal hai?", "aging_report"),
                                       ("rana brothers ka order pakka hua?", "list_orders")])
def test_a_question_is_a_read(p, text, tool):
    r = p.handle_message("t", "owner", text)
    assert r.pending is None and r.tool == tool, r.text


def test_a_balance_question_after_a_payment_is_not_a_confirm_card(p):
    p.handle_message("t", "clerk", "رانا برادرز نے ۲۵۰۰۰ نقد دیے")
    r = p.handle_message("t", "clerk", "pakka? balance kitna reh jayega")
    assert (r.pending is None or r.pending.tool != "confirm_order") and "Confirm order" not in r.text
    assert r.tool == "get_customer_khata" and r.call["args"]["customer_id"] == "C-005"


def test_bulk_reminders_need_an_explicit_bulk_instruction(p):
    assert p.handle_message("t", "owner", "sab overdue customers ko reminder bhejo").pending.tool == "draft_due_reminders"
    assert p.handle_message("t2", "owner", "wasooli dekho zara").pending is None


# ---------------------------------------------------------------- c) owner-tier requests from a clerk / salesman
@pytest.mark.parametrize("role,text,who", [("clerk", "urea ki 6 bori damage ho gayi multan godown mei, stock se nikal do", "owner"),
                                           ("clerk", "credit note Rana Brothers 5000 damaged", "owner"),
                                           ("clerk", "fauji ko 500000 payment ki cheque se", "owner"),
                                           ("salesman", "haji sons ne counter pe 5000 cash diye", "clerk")])
def test_a_request_the_role_cant_make_is_passed_on_not_dropped(p, role, text, who):
    before = len(p.repo.notifications(who))
    r = p.handle_message("t", role, text, user="Bilal")
    assert r.pending is None and not p.pending                              # tiers unchanged: no card from this role
    assert ("owner" if who == "owner" else "office") in r.text
    notes = p.repo.notifications(who)
    assert len(notes) == before + 1 and "Bilal" in notes[0]["text"]


# ---------------------------------------------------------------- d) a route's plan is every order reserved for it
def _two_allocated(p):
    for text in ("malik agro ka order allocate karo", "green valley ko 10 urea", "green valley ka order confirm karo", "green valley ka order allocate karo"):
        r = p.handle_message("setup", "clerk", text)
        assert r.pending, (text, r.text)
        p.resolve(r.pending.approval_id, True, "owner")


def test_a_route_plan_carries_every_reserved_order(p):
    p.repo.create_order("C-001", [{"sku": "DAP-50", "qty": 10}], "chat", "fixture", "fixture")
    malik = p.repo.list_orders(customer_id="C-001", limit=1)[0].order_id
    p.repo.confirm_order(malik, "fixture", "fixture")
    _two_allocated(p)
    want = plan_orders("R-MULTAN-N", p.repo)
    assert len(want) == 2 and want[0] == malik                                # route order: Malik Agro (C-001) first
    r = p.handle_message("t", "clerk", "ab multan north ka plan bana do, dusri gaari se")
    assert r.pending and r.pending.tool == "create_dispatch_plan" and r.pending.args["order_ids"] == want
    assert r.pending.args["route_id"] == "R-MULTAN-N"
    # a model's plan that leaves one out is refused, naming who was left out
    q = check_call("create_dispatch_plan", {"route_id": "R-MULTAN-N", "vehicle_id": "V-02", "order_ids": want[1:]}, "multan north ka plan bana do", p.repo)
    assert q and "Malik Agro Store" in q and "leaves out" in q
    assert check_call("create_dispatch_plan", {"route_id": "R-MULTAN-N", "vehicle_id": "V-02", "order_ids": want}, "multan north ka plan bana do", p.repo) is None


# ---------------------------------------------------------------- e) the card a correction / withdrawal is about
def test_correcting_the_second_waiting_card(p):
    first = p.handle_message("t", "clerk", "chaudhry farms ko 20 urea 5 dap").pending
    second = p.handle_message("t", "clerk", "malik agro ko 50 urea").pending
    r = p.handle_message("t", "clerk", "galti ho gayi malik agro ko 40 urea chahiye 50 nahi")
    assert r.pending and r.pending.args["customer_id"] == "C-001" and r.pending.args["items"] == [{"sku": "UREA-50", "qty": 40}]
    left = {x.approval_id for x in p.pending_items("t")}
    assert first.approval_id in left and second.approval_id not in left


def test_withdrawing_a_card_by_name_or_as_ye(p):
    first = p.handle_message("t", "clerk", "chaudhry farms ko 20 urea 5 dap").pending
    second = p.handle_message("t", "clerk", "malik agro ko 50 urea").pending
    r = p.handle_message("t", "clerk", "malik agro wala card wapis le lo")
    assert r.pending is None and "Withdrawn" in r.text and "Malik Agro" in r.text
    assert {x.approval_id for x in p.pending_items("t")} == {first.approval_id}
    r2 = p.handle_message("t", "clerk", "wapis lo ye, abhi approve na karna")
    assert "Withdrawn" in r2.text and not p.pending_items("t")
    assert second.approval_id not in p.pending


# ---------------------------------------------------------------- dates on the business's calendar
def test_a_timestamp_is_shown_on_the_business_day():
    """A payment at 01:30 in Pakistan is stored as 20:30 UTC the day before: it is said as today, not yesterday."""
    today = business_today()
    late = (datetime.combine(today, datetime.min.time()) + timedelta(hours=1, minutes=30) - timedelta(hours=5)).replace(tzinfo=UTC)
    assert late.date() == today - timedelta(days=1)                                  # the UTC date prefix is yesterday
    assert answers._day(late.isoformat()) == answers._day(today.isoformat())
    assert answers._day("2026-09-25") == "25 Sep"                                    # a bare date is taken as it is


def test_receipts_offered_to_pick_from_use_the_business_day(p):
    # two receipts made at 01:30 Pakistan time today -- stored as 20:30 UTC YESTERDAY (written directly: the ledger is append-only)
    stamp = (datetime.combine(business_today(), datetime.min.time()) - timedelta(hours=3, minutes=30)).replace(tzinfo=UTC).isoformat()
    with p.repo._tx() as c:
        for k, (amt, how) in enumerate(((1_000_000, "jazzcash"), (2_000_000, "cash"))):
            c.execute("INSERT INTO ledger (entry_id, customer_id, kind, amount, ref, due_date, created_at, method, received_by) VALUES (?,?,?,?,?,?,?,?,?)",
                      (f"PAY-LATE{k}", "C-002", "payment", -amt, "late", None, stamp, how, "office"))
    r = p.handle_message("t", "owner", "chaudhry farms ki payment reverse karo")
    assert answers._day(business_today().isoformat()) in r.text


# ---------------------------------------------------------------- readable replies
def test_lists_and_questions_use_names_not_codes(p):
    r = p.handle_message("t", "owner", "stocks kitne baqi hein?")
    assert "Zinc Sulphate" in r.text and "mazeed" not in visible(r.text)             # 10 products: all of them
    q = MunshiPlatform().handle_message("t", "clerk", "kisan wale ko 5 urea")
    assert "Bhatti Kisan Store" in q.text and "New Kisan Dost" in q.text and "C-0" not in q.text
    o = p.handle_message("t3", "clerk", "draft orders dikhao")
    assert "ORD-" not in visible(o.text)


def test_supplier_payment_keeps_the_cheque_number_and_reads_lakh(p):
    r = p.handle_message("t", "owner", "fauji ko 5 lakh ka cheque de diya aaj, MCB cheque 22871")
    assert r.pending and r.pending.tool == "pay_supplier" and float(r.pending.args["amount"]) == 500000 and r.pending.args["method"] == "cheque"
    assert "22871" in r.pending.args["ref"] and "22871" in r.text
    a = p.handle_message("t2", "owner", "ali akbar ko 35000 cash de diye")
    assert a.pending and a.pending.args["supplier_id"] == "S-003" and float(a.pending.args["amount"]) == 35000
    c = p.handle_message("t3", "clerk", "al barakah ka cheque aya he 50000 ka HBL cheque no 004512")
    assert c.pending and c.pending.args["ref"] == "cheque 004512 (HBL)" and float(c.pending.args["amount"]) == 50000


def test_driver_partial_delivery_in_urdu_word_order(p):
    from munshi.llm.parse import analyse_close
    c = analyse_close("chaudhry farms ne 15 urea li 5 urea wapis, 5 dap li, 50000 cash diya, code 1234", p.repo)
    assert sorted((i["sku"], i["qty"]) for i in c.delivered) == [("DAP-50", 5), ("UREA-50", 15)] and c.returned == [{"sku": "UREA-50", "qty": 5}]
    c2 = analyse_close("close STP-1 delivered 8 npk 5 sop returned 2 npk cash 60000 otp 3519", p.repo)   # English order unchanged
    assert c2.returned == [{"sku": "NPK-25", "qty": 2}]
