"""The product owner's first session, as behaviour: each test is a failure they hit (or the coordinator's stock-in
report), driven only through MunshiPlatform.handle_message / resolve, so it runs unchanged against the code before
the fix (where it fails) and after it.

  1. an answer to the munshi's own question completes the original request
  2. a follow-up about the same customer doesn't ask "which customer?" again
  3. whole-business questions need no customer
  4. replies are sentences, not raw JSON
  5. typos resolve
  6. adding stock is a write, never a stock read"""
from __future__ import annotations

import pytest

from munshi.domain.models import Customer
from munshi.domain.repository import MunshiRepository
from munshi.domain.seed import seeded_repository
from munshi.platform import MunshiPlatform

DETAILS = "\n\nDone -- "


def shown(reply) -> str:
    """What the user reads (the raw details block, if any, is folded away in the app)."""
    return reply.text.split(DETAILS, 1)[0]


def raw_json(text: str) -> bool:
    return text.startswith("Done --") or '{"' in text or "customer_id" in text


@pytest.fixture
def p():
    repo = seeded_repository()
    repo.upsert_customer(Customer("C-011", "Chaudhry Traders", "0300-1111011", "standard", 300_000, "R-VEHARI"))
    repo.upsert_customer(Customer("C-012", "Malik Seeds", "0300-1111012", "standard", 300_000, "R-VEHARI"))
    return MunshiPlatform(repo)


# ------------------------------------------------------------------ 1. answers to the munshi's own questions
def test_the_amount_answers_how_much(p):
    q = p.handle_message("t", "owner", "confirm payment for haji sons")
    assert q.pending is None and "how much" in shown(q).lower()
    r = p.handle_message("t", "owner", "80000")
    assert r.pending and r.pending.tool == "record_payment"
    assert r.pending.args["customer_id"] == "C-009" and float(r.pending.args["amount"]) == 80000


def test_a_spoken_amount_answers_how_much(p):
    p.handle_message("t", "clerk", "Haji Sons ne payment ki")
    r = p.handle_message("t", "clerk", "50 hazar")
    assert r.pending and r.pending.args["customer_id"] == "C-009" and float(r.pending.args["amount"]) == 50000


def test_pehla_doosra_picks_from_the_candidates_offered(p):
    q = p.handle_message("t", "owner", "Malik ka balance")
    assert "Malik Agro Store" in shown(q) and "Malik Seeds" in shown(q)
    r = p.handle_message("t", "owner", "doosra")
    assert "Malik Seeds" in shown(r) and not raw_json(shown(r))


def test_a_name_answers_which_customer(p):
    q = p.handle_message("t", "owner", "Chaudhry ne 50000 diye")
    assert q.pending is None and "Chaudhry Traders" in shown(q)
    r = p.handle_message("t", "owner", "Chaudhry Farms")
    assert r.pending and r.pending.args["customer_id"] == "C-002" and float(r.pending.args["amount"]) == 50000


def test_a_date_answers_by_when(p):
    p.handle_message("t", "clerk", "Haji Sons ka wada likho 30000")
    r = p.handle_message("t", "clerk", "kal tak")
    assert r.pending and r.pending.tool == "log_promise" and r.pending.args["customer_id"] == "C-009"


def test_items_answer_which_items(p):
    q = p.handle_message("t", "clerk", "Chaudhry Farms ka naya order likho")
    assert q.pending is None
    r = p.handle_message("t", "clerk", "20 urea aur 5 dap")
    assert r.pending and r.pending.tool == "create_order" and r.pending.args["customer_id"] == "C-002"
    assert sorted((i["sku"], i["qty"]) for i in r.pending.args["items"]) == [("DAP-50", 5), ("UREA-50", 20)]


def test_a_new_complete_request_replaces_the_open_question(p):
    p.handle_message("t", "clerk", "Haji Sons ne payment ki")
    r = p.handle_message("t", "clerk", "Rana Brothers ne 20000 diye")
    assert r.pending and r.pending.args["customer_id"] == "C-005" and float(r.pending.args["amount"]) == 20000


def test_an_answer_after_the_talk_moved_on_is_not_guessed(p):
    p.handle_message("t", "clerk", "Haji Sons ne payment ki")
    p.handle_message("t", "clerk", "Rana Brothers ka balance?")
    r = p.handle_message("t", "clerk", "40000")
    assert r.pending is None


def test_the_open_question_survives_a_restart(tmp_path):
    db = str(tmp_path / "biz.sqlite")
    seeded_repository(db).close()
    p1 = MunshiPlatform(MunshiRepository(db), checkpoint_path=str(tmp_path / "ck.sqlite"))
    p1.handle_message("t", "owner", "confirm payment for haji sons")
    p1.close()
    p2 = MunshiPlatform(MunshiRepository(db), checkpoint_path=str(tmp_path / "ck.sqlite"))
    r = p2.handle_message("t", "owner", "80000")
    assert r.pending and r.pending.args["customer_id"] == "C-009"
    p2.close()


def test_the_open_question_belongs_to_one_role(p):
    p.handle_message("t", "owner", "confirm payment for haji sons")
    r = p.handle_message("t", "clerk", "80000")
    assert r.pending is None


# ------------------------------------------------------------------ 2. topic memory
def test_their_payment_asks_how_much_for_the_customer_just_discussed(p):
    p.handle_message("t", "owner", "haji sons ki kitni collection baqi he?")
    r = p.handle_message("t", "owner", "confirm their payment")
    assert r.pending is None and "Haji Sons" in shown(r) and "which customer" not in shown(r).lower()
    r2 = p.handle_message("t", "owner", "80000")
    assert r2.pending and r2.pending.args["customer_id"] == "C-009" and "from our conversation" in r2.text


def test_a_pronoun_payment_raises_a_card_that_says_who_it_is_for(p):
    p.handle_message("t", "clerk", "Rana Brothers ka khata dikhao")
    r = p.handle_message("t", "clerk", "unhon ne 30000 jama karwaye")
    assert r.pending and r.pending.args["customer_id"] == "C-005"
    assert "for Rana Brothers (from our conversation)" in r.text


def test_plain_follow_ups_use_the_customer_just_discussed(p):
    p.handle_message("t", "owner", "Haji Sons ka khata")
    r = p.handle_message("t", "owner", "balance kitna hai")
    assert "Haji Sons" in shown(r) and "which customer" not in shown(r).lower()


def test_an_urdu_script_pronoun_follow_up(p):
    p.handle_message("t", "clerk", "حاجی سنز کا کھاتہ")
    r = p.handle_message("t", "clerk", "اس نے 20000 جمع کروائے")
    assert r.pending and r.pending.args["customer_id"] == "C-009"


def test_a_supplier_follow_up(p):
    p.handle_message("t", "owner", "Fauji ka hisaab")
    r = p.handle_message("t", "owner", "unko 200000 bank se de do")
    assert r.pending and r.pending.tool == "pay_supplier" and r.pending.args["supplier_id"] == "S-001"


def test_the_topic_does_not_cross_roles(p):
    p.handle_message("t", "owner", "Haji Sons ka khata")
    r = p.handle_message("t", "clerk", "confirm their payment 5000")
    assert r.pending is None and "Haji Sons" not in shown(r)


def test_naming_someone_else_switches_the_topic(p):
    p.handle_message("t", "clerk", "Haji Sons ka khata")
    p.handle_message("t", "clerk", "Rana Brothers ka khata")
    r = p.handle_message("t", "clerk", "unhon ne 30000 jama karwaye")
    assert r.pending and r.pending.args["customer_id"] == "C-005"


# ------------------------------------------------------------------ 3. whole-business questions
@pytest.mark.parametrize("msg", ["stocks kitne baqi hein?", "aj ka stock count", "for all the products?", "sab ka stock", "kitna maal pada hai"])
def test_whole_business_stock_questions_list_every_product(p, msg):
    r = p.handle_message("t", "owner", msg)
    txt = shown(r)
    assert r.specialist == "godown" and "Urea" in txt and "DAP" in txt
    assert "which customer" not in txt.lower() and "which product" not in txt.lower() and not raw_json(txt)


@pytest.mark.parametrize("msg", ["kis costumer se kitne paise lene hein?", "kis ne kitna dena hai", "udhaar list", "who owes us"])
def test_receivables_questions_list_who_owes(p, msg):
    r = p.handle_message("t", "owner", msg)
    txt = shown(r)
    assert "Haji Sons" in txt and "which customer" not in txt.lower() and not raw_json(txt)


def test_who_paid_today_lists_todays_payments_not_the_aging(p):
    for msg in ("confirm payment for haji sons 80000", "confirm payment for chaudary farms 50000"):
        card = p.handle_message("t", "owner", msg).pending
        p.resolve(card.approval_id, True, "owner", user="Owner")
    r = p.handle_message("t", "owner", "aaj kis kis client ne payment ki he?")
    txt = shown(r)
    assert "Haji Sons" in txt and "Chaudhry Farms" in txt and "80,000" in txt
    assert "60+" not in txt and "overdue" not in txt.lower()


def test_latest_orders_are_listed_readably(p):
    r = p.handle_message("t", "owner", "kis item ke latest orders aye the?")
    assert r.specialist == "order" and not raw_json(shown(r)) and shown(r) != "Done -- []"


def test_an_empty_order_list_says_so(p):
    r = p.handle_message("t", "owner", "cypermethrin ke orders")
    assert "no " in shown(r).lower() or "koi" in shown(r).lower()
    assert "[]" not in shown(r)


def test_what_is_an_sku_is_explained(p):
    r = p.handle_message("t", "owner", "sku kya he?")
    assert "SKU" in shown(r) and "Is this about an order" not in shown(r)


# ------------------------------------------------------------------ 4. readable replies
def test_a_khata_read_is_a_sentence(p):
    r = p.handle_message("t", "owner", "haji sons ki kitni collection baqi he?")
    assert "Haji Sons" in shown(r) and "Rs 84,000" in shown(r) and not raw_json(shown(r))


def test_an_approved_payment_says_what_was_recorded(p):
    card = p.handle_message("t", "owner", "confirm payment for haji sons 80000").pending
    out = p.resolve(card.approval_id, True, "owner", user="Owner")
    txt = shown(out)
    assert "Rs 80,000" in txt and "Haji Sons" in txt and "RCP-" in txt and "Rs 4,000" in txt and not raw_json(txt)


def test_slow_stock_is_a_sentence(p):
    r = p.handle_message("t", "owner", "Slow stock 30 days")
    assert "30 days" in shown(r) and "days_without_sale" not in shown(r) and not raw_json(shown(r))


# ------------------------------------------------------------------ 5. tolerance
@pytest.mark.parametrize("msg,cid", [("hajji sons ka khata", "C-009"), ("haji son ka balance", "C-009"), ("chaudary farms ka khata", "C-002")])
def test_typos_resolve(p, msg, cid):
    r = p.handle_message("t", "clerk", msg)
    name = {"C-009": "Haji Sons", "C-002": "Chaudhry Farms"}[cid]
    assert name in shown(r)


# ------------------------------------------------------------------ 6. adding stock is a write
@pytest.mark.parametrize("msg", ["1000 stock of drip line  brha do multan mei", "increase the stock count of drip line in multan by 1000"])
def test_owner_increasing_stock_gets_an_adjustment_card(p, msg):
    r = p.handle_message("t", "owner", msg)
    assert r.pending and r.pending.tool == "adjust_stock"
    assert r.pending.args["sku"] == "DRIP-100" and int(r.pending.args["delta"]) == 1000 and r.pending.args["warehouse_id"] == "WH-MULTAN"


def test_stock_that_arrived_asks_purchase_or_correction_and_the_answer_completes_it(p):
    q = p.handle_message("t", "owner", "drip line ke 1000 aur stock ayein hein")
    assert q.pending is None and "supplier" in shown(q) and "correction" in shown(q)
    assert "WH-MULTAN:" not in shown(q) and "available" not in shown(q)
    r = p.handle_message("t", "owner", "correction")
    assert r.pending and r.pending.tool == "adjust_stock" and int(r.pending.args["delta"]) == 1000


def test_stock_that_arrived_from_a_supplier_becomes_a_purchase(p):
    p.handle_message("t", "owner", "multan godown mein 50 urea aaye hain")
    r = p.handle_message("t", "owner", "Fauji se aaye, 3600 rate")
    assert r.pending and r.pending.tool == "record_purchase" and r.pending.args["supplier_id"] == "S-001"
    assert [(i["sku"], i["qty"]) for i in r.pending.args["items"]] == [("UREA-50", 50)]


def test_a_clerk_adding_stock_is_never_shown_a_stock_read(p):
    r = p.handle_message("t", "clerk", "increase the stock count of drip line in multan by 1000")
    assert r.pending is None and "available" not in shown(r)
