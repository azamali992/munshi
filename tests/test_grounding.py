"""Grounding the model (llm/grounding.py, platform._ground): the model may choose WHAT to do, but it is never the source
of a fact or of a claim about an action. With a scripted fake model (tests/test_hybrid.FakeChat):

  - a turn that ran read tools is answered by code's rendering of their results -- the model's prose, and any
    number in it, is never shown;
  - a turn with no tool call shows only a clean clarifying question; a claim ("note kar liya", "approval darkar hai")
    or a fact is replaced by the rules' "didn't understand";
  - raw record codes never survive, and the model never gets to ask the user for an ID;
  - a model card's result after approval is said by code ("Approved. ..."), never "still waiting"."""
from __future__ import annotations

import pytest

from munshi.domain.seed import seeded_repository
from munshi.llm import grounding as GR
from munshi.platform import DETAILS, MunshiPlatform, visible
from tests.test_hybrid import _fake, _makai, _malik, _NoRoute


@pytest.fixture
def repo():
    r = seeded_repository()
    _malik(r)
    yield r


def _model(repo, text, specialist, plan=None, final=""):
    fake = _fake(routes={text: specialist}, plan={text: plan or []}, final={text: final})
    p = MunshiPlatform(repo, model=fake)
    p.manager = _NoRoute()                      # put the message in front of the model on purpose
    return p, fake


def test_a_wrong_number_in_model_prose_is_never_shown(repo):
    text = "Bhatti sahab ka account dekhna hai zara"
    p, _ = _model(repo, text, "hisaab", [[("get_customer_khata", {"customer_id": "C-007"})]], "Bhatti ka khata Rs 99,999 hai, sab theek.")
    r = p.handle_message("t", "clerk", text)
    shown = visible(r.text)
    assert r.engine == "model" and "58,000" in shown and "99,999" not in r.text          # code's figure, from the tool result
    assert DETAILS in r.text                                                           # the raw result folded after it
    meta = p.repo.chat_history("t")[-1]["meta"]
    assert meta["model_text"].startswith("Bhatti ka khata Rs 99,999") and meta["grounded"] == ["get_customer_khata"]


def test_the_model_answers_every_row_code_reads(repo):
    """Persona #16: the model named 2 of 5 overdue customers. The aging list is rendered by code, every row."""
    text = "which of my customers are over their credit terms?"
    p, _ = _model(repo, text, "wasooli", [[("aging_report", {})]], "Haji Sons and Al-Barakah are over terms.")
    r = visible(p.handle_message("t", "owner", text).text)
    for name in ("Haji Sons", "Al-Barakah Traders", "Chaudhry Farms", "Punjab Seed Mart", "Rana Brothers"):
        assert name in r


def test_a_bank_question_is_answered_from_the_payments_code_read(repo):
    """Persona #117: 'no bank entries today' when Rs 100,000 came by bank."""
    p0 = MunshiPlatform(repo)
    card = p0.handle_message("c", "clerk", "rana brothers ne 1 lakh ka online transfer kiya meezan bank mei").pending
    p0.resolve(card.approval_id, True, "owner")
    text = "bank mei kitna aya aaj"
    from munshi.domain.models import business_today
    day = business_today().isoformat()
    p, _ = _model(repo, text, "report", [[("collection_report", {"start": day, "end": day})]], "Aaj direct bank ka koi entry nahi.")
    r = visible(p.handle_message("t", "owner", text).text)
    assert "100,000" in r and "koi entry nahi" not in r


def test_a_false_note_with_no_tool_is_replaced(repo):
    text = "haji sons wala maal wapis aa gaya, kal dobara bhejna he"
    p, _ = _model(repo, text, "godown", [], "Ji, kal dobara bhejne ke liye note kar liya hai. Kya dispatch plan mein daal dun?")
    r = p.handle_message("t", "clerk", text)
    assert "note kar liya" not in r.text and r.pending is None
    assert "samajh nahi" in r.text or "didn't understand" in r.text
    assert p.repo.chat_history("t")[-1]["meta"]["model_text_replaced"].startswith("Ji, kal dobara")


def test_a_phantom_approval_is_replaced(repo):
    """Persona #72/#75: 'approval darkar hai' with no card anywhere."""
    text = "urea ki 6 bori damage ho gayi, stock se nikal do"
    p, _ = _model(repo, text, "godown", [], "Multan Godown se 6 urea bori kam karne ke liye owner ki approval darkar hai.")
    r = p.handle_message("t", "clerk", text)
    assert "darkar" not in r.text and "approval" not in r.text.lower() and not p.pending


def test_a_pure_clarifying_question_passes(repo):
    text = "woh kal wala kaam kar do"
    p, _ = _model(repo, text, "order", [], "Kaun se customer ki baat kar rahe hain?")
    assert p.handle_message("t", "clerk", text).text == "Kaun se customer ki baat kar rahe hain?"


def test_a_question_with_a_number_or_an_id_request_is_not_passed(repo):
    for final in ("Kya Haji Sons ke 4000 wale bill ki baat hai?", "Order ka ID bata dein?", "Customer ki ID kya hai?",
                  "Plan number batayein taake check kar sakun."):
        text = f"woh wala kaam {len(final)}"
        p, _ = _model(repo, text, "order", [], final)
        r = p.handle_message("t", "clerk", text)
        assert r.text != final and "ID" not in r.text, final


def test_raw_ids_are_stripped_from_model_text(repo):
    assert GR.strip_ids("Kya WH-MULTAN se bhejna hai ya WH-VEHARI se?", repo) == "Kya Multan Godown se bhejna hai ya Vehari Godown se?"
    assert GR.strip_ids("Bhatti Kisan Store (C-007) ya New Kisan Dost?", repo) == "Bhatti Kisan Store ya New Kisan Dost?"
    assert "SEED-MAIZE" not in GR.strip_ids("SEED-MAIZE chahiye?", repo)
    text = "godown wala scene"
    p, _ = _model(repo, text, "godown", [], "Kya WH-MULTAN wala maal chahiye ya Vehari wala?")
    assert p.handle_message("t", "clerk", text).text == "Kya Multan Godown wala maal chahiye ya Vehari wala?"


def test_a_correct_model_count_is_rendered_by_code_not_blocked(repo):
    """Persona #213: the model said '11 orders' (right) and the claim check blocked it. Code now renders the count."""
    text = "آج کتنے آرڈر آئے"
    p, _ = _model(repo, text, "order", [[("list_orders", {"days": 1})]], "آج 11 آرڈر آئے ہیں۔")
    r = p.handle_message("t", "clerk", text)
    assert "سمجھ نہیں" not in r.text and "11" not in visible(r.text)          # the fixture has no orders today: code's count, not the model's


def test_a_model_card_reply_after_approval_says_what_happened(repo):
    """Persona A11: after approval the model said 'wants to ... Needs clerk approval' / 'intezaar hai'."""
    text = "Malik Seeds ko 8 makai"
    p, _ = _model(repo, text, "order", [[("create_order", {"customer_id": "C-012", "items": _makai(repo)})]],
                  "Order bana diya gaya hai, clerk ki approval ka intezaar hai.")
    r = p.handle_message("t", "clerk", text, user="Bilal")
    assert r.pending and "wants to" in r.text and "intezaar" not in r.text                 # the card's own code-built text
    out = p.resolve(r.pending.approval_id, True, "owner", user="Sultan")
    shown = visible(out.text)
    assert shown.startswith("Approved.") and "ORD-" in shown and "intezaar" not in out.text and "waiting" not in shown.lower()
    assert p.repo.chat_history("t")[-1]["meta"]["model_text"]
    rej = p.handle_message("t2", "clerk", text, user="Bilal").pending
    assert p.resolve(rej.approval_id, False, "owner", note="galat", user="Sultan").text.startswith("Rejected. Nothing was done.")


def test_a_closed_stop_is_said_from_its_result_not_as_pending(repo):
    """Persona #61: the stop WAS closed, the model said 'approval ke liye pending'."""
    from munshi.domain.models import business_today
    r0 = repo
    o = r0.create_order("C-002", [{"sku": "UREA-50", "qty": 20}], "chat", "fixture", "fixture").order_id
    r0.confirm_order(o, "fixture", "fixture")
    r0.allocate_order(o, "WH-MULTAN", "fixture", "fixture")
    plan = r0.create_dispatch_plan(business_today().isoformat(), "R-MULTAN-N", "V-01", [o], "fixture")
    r0.approve_dispatch_plan(plan.plan_id, "fixture", "fixture")
    st = r0.list_stops(plan.plan_id)[0]
    otp = r0.get_stop(st.stop_id).otp
    text = f"chaudhry wale ka scene: 15 de di 5 wapis, cash 50000 code {otp}"
    args = {"stop_id": st.stop_id, "delivered_items": [{"sku": "UREA-50", "qty": 15}], "returned_items": [{"sku": "UREA-50", "qty": 5}],
            "cash_collected": 50000, "otp": otp}
    p, _ = _model(repo, text, "delivery", [[("close_stop", args)]], "Entry save kar di hai, yeh approval ke liye pending hai.")
    p._driver_route = lambda t: None            # (the rules read a driver's door words themselves: put this one in front of the model)
    r = p.handle_message("t", "driver", text)
    shown = visible(r.text)
    assert "pending" not in shown and "approval" not in shown.lower() and "50,000" in shown
    assert r0.get_stop(st.stop_id).status == "short"


def test_a_model_write_the_message_never_asked_for_is_refused(repo):
    """Seen on Gemini after grounding: a cancel_order card for 'haan kal wale plan mei daal do', a reminder card (tier
    'standard') for 'Chaudhry Farms ke hawale se koi pending kaam'. The model picks the action; the message must ask for it."""
    from munshi.agents.guard import check_call
    oid = repo.create_order("C-005", [{"sku": "ZINC-10", "qty": 3}], "chat", "t", "t").order_id
    assert check_call("cancel_order", {"order_id": oid, "reason": "x"}, "haan kal wale plan mei daal do", repo, history=[f"Rana {oid}"])
    assert check_call("draft_reminder", {"customer_id": "C-002", "tier": "gentle"}, "Chaudhry Farms ke hawale se koi pending kaam", repo)
    assert check_call("draft_reminder", {"customer_id": "C-002", "tier": "standard"}, "Chaudhry Farms ko reminder bhej do", repo)
    assert check_call("draft_reminder", {"customer_id": "C-002", "tier": ""}, "Chaudhry Farms ko reminder bhej do", repo) is None
    assert check_call("cancel_order", {"order_id": oid, "reason": "x"}, "Rana Brothers ka order cancel karo", repo) is None
    # a bare 'haan' may carry the verb of the question it answers
    assert check_call("cancel_order", {"order_id": oid, "reason": "x"}, "haan", repo, history=[f"Rana Brothers ka order {oid} cancel kar dun?"]) is None


def test_a_model_reversal_must_be_the_one_receipt_code_reads(repo):
    """Seen on Gemini (every message forced to the model): '...50000 wali payment reverse karo' carded the Rs 20,000 receipt;
    '...payment reverse karo' with two receipts carded one without asking. The guard reads the receipt the rules' way."""
    from munshi.agents.guard import check_call
    e10 = repo.record_payment("C-002", 10000, "jazzcash", "", "hisaab_munshi", "owner")
    e20 = repo.record_payment("C-002", 20000, "cash", "", "hisaab_munshi", "owner")
    assert check_call("reverse_ledger_entry", {"entry_id": e20.entry_id, "reason": "x"}, "chaudhry farms ki 50000 wali payment reverse karo", repo)
    assert check_call("reverse_ledger_entry", {"entry_id": e20.entry_id, "reason": "x"}, "chaudhry farms ki payment reverse karo", repo)
    assert check_call("reverse_ledger_entry", {"entry_id": e10.entry_id, "reason": "x"}, "chaudhry farms ki 20000 wali payment reverse karo", repo)
    assert check_call("reverse_ledger_entry", {"entry_id": e20.entry_id, "reason": "x"}, "chaudhry farms ki 20000 wali payment reverse karo", repo) is None
    assert check_call("reverse_ledger_entry", {"entry_id": e20.entry_id, "reason": "x"}, f"{e20.entry_id} reverse karo galat raqam", repo) is None


def test_gemini_token_usage_is_counted():
    """Persona round 2: model_tokens was always 0 on Gemini (usage is on the message, not in llm_output)."""
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    from munshi.platform import _ModelCalls
    c = _ModelCalls()
    msg = AIMessage(content="x", usage_metadata={"input_tokens": 90, "output_tokens": 30, "total_tokens": 120})
    c.on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]], llm_output={}))
    assert c.tokens == 120


def test_ok_question_rules():
    assert GR.ok_question("Kaun sa customer?")
    assert GR.ok_question("Kaunsa kaam? Customer aur maal ka naam batayein.")
    assert not GR.ok_question("Haji Sons ka balance 4000 hai.")
    assert not GR.ok_question("Order bana diya. Kuch aur?")
    assert not GR.ok_question("Kya approval ke liye bhej dun?")
    assert not GR.ok_question("Multan wale customer ka naam ya order number bata dein taake check kar sakun.")
