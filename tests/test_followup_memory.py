"""Unit tests for the conversation memory (llm/followup.py), the readable replies (llm/answers.py), the open-question
tagging (llm/replies.Ask, the stub's ASK_KEY) and the guard's WHO check agreeing with the offline rules on follow-ups."""
from __future__ import annotations

from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage

from munshi.agents import guard
from munshi.domain.models import Customer
from munshi.domain.seed import seeded_repository
from munshi.llm import answers
from munshi.llm import followup as FU
from munshi.llm import replies as RP
from munshi.llm.stub_model import StubToolCallingModel, ask_of
from munshi.platform import DETAILS, MunshiPlatform, visible
from tests.test_hybrid import _fake, _NoRoute

HAJI = {"customer": {"id": "C-009", "name": "Haji Sons"}, "last": "customer", "role": "clerk"}


@pytest.fixture
def repo():
    r = seeded_repository()
    r.upsert_customer(Customer("C-011", "Chaudhry Traders", "0300-1111011", "standard", 300_000, "R-VEHARI"))
    r.upsert_customer(Customer("C-012", "Malik Seeds", "0300-1111012", "standard", 300_000, "R-VEHARI"))
    return r


# ------------------------------------------------------------------ open questions
def test_a_question_for_one_missing_piece_is_tagged_with_its_slot():
    q = RP.t("how_much", False)
    assert isinstance(q, RP.Ask) and q.slot == "amount" and str(q) == RP.EN["how_much"]
    assert RP.t("fix_qty", False, why="x") == "I haven't made a card: x. Please send the order again with the exact quantities."
    assert not isinstance(RP.t("fix_qty", False, why="x"), RP.Ask)


def test_the_offline_model_marks_an_asking_reply():
    m = StubToolCallingModel(rules=[], fallback_fn=lambda t, s: RP.t("how_much", False))
    msg = m.invoke("Haji Sons ne payment ki")
    assert ask_of(msg) == {"slot": "amount", "candidates": []}
    assert ask_of(AIMessage(content="How much?")) is None


@pytest.mark.parametrize("text,n", [("pehla", 1), ("pehla wala", 1), ("first one", 1), ("2", 2), ("doosra wala", 2), ("teesra", 3), ("dusri", 2)])
def test_ordinals(text, n):
    assert FU.ordinal(text) == n


@pytest.mark.parametrize("text", ["pehla order confirm karo", "2 urea", "doosra aur teesra"])
def test_an_ordinal_with_anything_else_is_not_a_pick(text):
    assert FU.ordinal(text) is None


def test_only_the_missing_piece_is_an_answer(repo):
    ask = {"slot": "amount", "text": "Haji Sons ne payment ki"}
    assert FU.answer(ask, "80000", repo) == ("Haji Sons ne payment ki 80000", None)
    assert FU.answer(ask, "80 hazar cash", repo) == ("Haji Sons ne payment ki 80 hazar cash", None)
    assert FU.answer(ask, "Rana Brothers ne 20000 diye", repo) is None          # names someone: a new request
    assert FU.answer(ask, "urea ka stock", repo) is None
    cust = {"slot": "customer", "text": "Malik ka balance", "candidates": [{"id": "C-001", "name": "Malik Agro Store"}, {"id": "C-012", "name": "Malik Seeds"}]}
    assert FU.answer(cust, "doosra", repo) == ("Malik ka balance C-012", None)
    assert FU.answer(cust, "Malik Seeds wala", repo) == ("Malik ka balance C-012", None)
    assert FU.answer(cust, "teesra", repo) is None                              # there was no third
    assert FU.answer(cust, "Malik Seeds ko 5 urea bhej do", repo) is None
    kind = {"slot": "stock_kind", "text": "50 urea aaye"}
    assert FU.answer(kind, "correction", repo) == ("50 urea aaye adjust", "godown")
    assert FU.answer(kind, "Fauji se", repo) == ("50 urea aaye Fauji se", "khareed")


def test_an_open_question_expires(repo, monkeypatch):
    p = MunshiPlatform(repo)
    p.handle_message("t", "owner", "confirm payment for haji sons")
    later = FU.now() + timedelta(seconds=FU.EXPIRY_S + 60)
    monkeypatch.setattr(FU, "now", lambda: later)
    assert p.handle_message("t", "owner", "80000").pending is None


def test_an_open_question_survives_a_little_small_talk_but_not_more(repo):
    p = MunshiPlatform(repo)
    p.handle_message("t", "owner", "confirm payment for haji sons")
    p.handle_message("t", "owner", "shukriya")
    r = p.handle_message("t", "owner", "80000")
    assert r.pending and r.pending.args["customer_id"] == "C-009"
    p.handle_message("t2", "owner", "confirm payment for haji sons")
    for _ in range(FU.MAX_CHATTER + 1):
        p.handle_message("t2", "owner", "shukriya")
    assert p.handle_message("t2", "owner", "80000").pending is None


def test_the_memory_lives_in_the_chat_log(repo):
    p = MunshiPlatform(repo)
    p.handle_message("t", "owner", "confirm payment for haji sons")
    memo = repo.chat_history("t")[-1]["meta"]["memo"]
    assert memo["role"] == "owner" and memo["ask"]["slot"] == "amount" and memo["topic"]["customer"]["id"] == "C-009"


# ------------------------------------------------------------------ topic memory
def test_augment_adds_the_remembered_customer_only_when_the_message_means_them(repo):
    assert FU.augment("confirm their payment", HAJI, repo) == ("confirm their payment C-009", {"kind": "customer", "id": "C-009", "name": "Haji Sons"})
    assert FU.augment("balance kitna hai", HAJI, repo)[1]["id"] == "C-009"
    assert FU.augment("aur payment?", HAJI, repo)[1]["id"] == "C-009"
    for text in ("aaj kis kis ne payment ki", "udhaar list", "profit this month", "urea ka stock kitna hai", "Rana Brothers ka balance", "40000",
                 "20 urea bhej do", "Chaudhry ka balance"):
        assert FU.augment(text, HAJI, repo) == (text, None), text
    assert FU.augment("inko 20 urea bhej do", HAJI, repo)[1]["id"] == "C-009"      # an order leans on the topic only with a pronoun


def test_the_topic_follows_what_was_named(repo):
    t = FU.next_topic(None, "Haji Sons ka khata", repo, "clerk")
    assert t["customer"]["id"] == "C-009" and t["last"] == "customer"
    t = FU.next_topic(t, "Fauji ka hisaab", repo, "clerk")
    assert t["supplier"]["id"] == "S-001" and t["last"] == "supplier" and t["customer"]["id"] == "C-009"
    t = FU.next_topic(t, "Chaudhry ka balance", repo, "clerk")                    # ambiguous: the customer is dropped, never guessed
    assert t["customer"] is None
    assert FU.next_topic(t, "urea ka stock", repo, "owner")["role"] == "owner"   # another role starts afresh
    assert "customer" not in FU.next_topic(t, "urea ka stock", repo, "owner")


# ------------------------------------------------------------------ the guard agrees with the rules
def test_the_guard_accepts_a_remembered_customer_exactly_when_the_rules_would(repo):
    call = {"customer_id": "C-009", "amount": 5000, "method": "cash"}
    assert guard.check_call("record_payment", call, "confirm payment 5000", repo, history=[]) is not None       # nobody named, no memory: ask
    tok = guard.set_topic(HAJI)
    try:
        assert guard.check_call("record_payment", call, "confirm payment 5000", repo, history=[]) is None
        assert guard.check_call("record_payment", call, "unhon ne 5000 diye", repo, history=[]) is None
        assert guard.check_call("record_payment", call | {"customer_id": "C-005"}, "confirm payment 5000", repo, history=[]) is not None
        assert guard.check_call("record_payment", call, "aaj kis kis ne 5000 payment ki", repo, history=[]) is not None
    finally:
        guard.reset_topic(tok)


def test_a_model_card_for_a_remembered_customer_passes_the_guard_and_says_so(repo):
    text = "confirm payment 5000"
    fake = _fake(routes={text: "hisaab"}, plan={text: [[("record_payment", {"customer_id": "C-009", "amount": 5000, "method": "cash"})]]})
    p = MunshiPlatform(repo, model=fake)
    p.handle_message("t", "clerk", "Haji Sons ka khata")                     # rules engine: the topic is Haji Sons
    p.manager = _NoRoute()                                                  # put the next message in front of the model
    r = p.handle_message("t", "clerk", text, user="Bilal")
    assert r.engine == "model" and r.pending and r.pending.args["customer_id"] == "C-009"
    assert "for Haji Sons (from our conversation)" in r.text


# ------------------------------------------------------------------ readable replies
def test_lists_are_short_with_a_count_of_the_rest(repo):
    rows = [{"customer_id": f"C-{i:03d}", "name": f"Customer {i}", "balance": 1000 * i, "days_overdue": i} for i in range(1, 13)]
    txt = answers.render("aging_report", rows, repo, "who owes us")
    assert txt.startswith("12 customers owe Rs 78,000") and "...and 4 more." in txt and "{" not in txt and "customer_id" not in txt


def test_roman_urdu_and_urdu_script_replies(repo):
    kh = {"customer": {"customer_id": "C-009", "name": "Haji Sons"}, "outstanding": 84000.0, "aging": {"days_overdue": 65}, "recent": [], "promise": None}
    assert answers.render("get_customer_khata", kh, repo, "haji sons ka balance") == "Haji Sons ke Rs 84,000 baqi hain (sab se purana bill 65 din se overdue)."
    assert "باقی" in answers.render("get_customer_khata", kh, repo, "حاجی سنز کا کھاتہ")
    assert answers.render("get_customer_khata", kh, repo, "what does Haji Sons owe").startswith("Haji Sons owes Rs 84,000")


def test_empty_states_and_non_results(repo):
    assert answers.render("list_orders", [], repo, "today's orders", {"days": 1}) == "No orders yet today."
    assert answers.render("list_orders", [], repo, "aaj ke orders", {"days": 1}) == "Aaj abhi tak koi order nahi aaya."
    assert answers.render("list_orders", [], repo, "urea orders", {"sku": "UREA-50"}) == "No Urea 50kg orders in the last 30 days."
    assert answers.render("get_stock", '{"error": "no such product"}', repo, "x") is None
    assert answers.render("no_such_tool", [], repo, "x") is None
    assert answers.render("create_order", "User rejected the tool call for `create_order` with reason: rejected by clerk", repo, "x") == \
        "Nothing was done -- rejected by clerk."


def test_a_reply_keeps_the_raw_result_folded_after_the_sentence(repo):
    r = MunshiPlatform(repo).handle_message("t", "clerk", "urea ka stock kitna hai")
    assert DETAILS in r.text and visible(r.text).startswith("Stock -- Urea 50kg") and "WH-MULTAN" not in visible(r.text)
    assert '"warehouse_id": "WH-MULTAN"' in r.text.split(DETAILS, 1)[1]
