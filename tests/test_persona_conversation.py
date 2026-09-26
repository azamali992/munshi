"""Priority-2 gaps from the owner-persona run: short answers, acting by name, queued work while a card waits, the
owner's everyday questions, the driver at the door -- and the readable answers they get (llm/answers.py).
The end-to-end cases are also in eval/gold_persona.jsonl (gated in tests/test_gold_corpus.py)."""
from __future__ import annotations

import pytest

from munshi.agents import guard
from munshi.domain.seed import seeded_repository
from munshi.llm import answers
from munshi.llm import followup as FU
from munshi.llm import turns as TURNS
from munshi.llm.parse import amount_in, analyse_order, date_in
from munshi.platform import MunshiPlatform, visible

CLERK, OTHER = "Ayesha", "Bilal"


@pytest.fixture
def p():
    plat = MunshiPlatform(seeded_repository())
    yield plat
    plat.close()


# ------------------------------------------------------------------ readable answers (new domain fields)
def test_profit_with_uncosted_sales_says_so_and_quotes_the_costed_margin(p):
    d = {"start": "2026-09-01", "end": "2026-09-26", "revenue": 1085000, "cost_of_goods": 0, "gross_margin": 1085000, "expenses": 16100,
         "net": 1068900, "margin_pct": 100.0, "margin_reliable": False, "costed_margin_pct": 7.4, "cost_missing": {"count": 3, "revenue": 1085000.0},
         "caveat": "Rs 1,085,000 of sales (3 invoices) have no cost on record, so the margin is overstated"}
    en = answers.render("profit_summary", d, p.repo, "profit this month")
    assert "no cost on record" in en and "7.4%" in en
    ru = answers.render("profit_summary", d, p.repo, "is mahine ka munafa kitna hai")
    assert "laagat darj nahi" in ru and "Rs 1,085,000" in ru and "3 bill" in ru and "7.4%" in ru
    ok = answers.render("profit_summary", d | {"margin_reliable": True}, p.repo, "profit this month")
    assert "Note" not in ok
    sales = answers.render("sales_report", d | {"invoices": 3}, p.repo, "sales this month")
    assert "no cost on record" in sales


def test_cashbook_names_the_driver_shortfall_beside_the_drawer_figure(p):
    d = {"date": "2026-09-26", "cash_in": [], "driver_handins": [], "cash_out": [], "total_in": 0, "total_handins": 48000, "total_out": 8500,
         "net": 39500, "shortfalls": [{"kind": "driver shortfall", "who": "Bhatti Kisan Store", "amount": 2000.0}], "total_shortfall": 2000.0}
    txt = answers.render("cashbook", d, p.repo, "aaj ka cashbook")
    assert "net Rs 39,500" in txt and "Rs 2,000 short" in txt and "Bhatti Kisan Store" in txt
    none = answers.render("cashbook", d | {"shortfalls": [], "total_shortfall": 0}, p.repo, "driver ka cash pura aya?")
    assert "pura hai" in none


def test_payments_today_count_only_payments_and_say_reversals_apart(p):
    from munshi.domain.models import business_today
    day = business_today().isoformat()
    d = {"start": day, "end": day, "payment_count": 2, "reversals": {"count": 1, "amount": -50000.0}, "payments": [
        {"entry_id": "RCP-1", "name": "Al-Barakah Traders", "amount": 50000.0, "method": "cheque", "reversal_of": None},
        {"entry_id": "RCP-2", "name": "Haji Sons", "amount": 20000.0, "method": "cash", "reversal_of": None},
        {"entry_id": "RCP-3", "name": "Shalimar Agri Centre", "amount": 5000.0, "method": "cash", "reversal_of": None},
        {"entry_id": "REV-1", "name": "Al-Barakah Traders", "amount": -50000.0, "method": "cheque", "reversal_of": "RCP-1"}]}
    txt = answers.render("collection_report", d, p.repo, "aaj kis kis ne payment ki")
    assert "2 payments" in txt and "Rs 25,000" in txt and "-Rs 50,000" not in txt and "Al-Barakah Traders Rs 50,000" not in txt
    assert "1 payment reverse" in txt and "Rs 50,000" in txt


def test_all_means_the_whole_list(p):
    rows = p.ops.get_stock("")
    short = answers.render("get_stock", rows, p.repo, "aj ka stock count")
    full = answers.render("get_stock", rows, p.repo, "for all the products?")
    assert "Wheat Seed" in short and "Zinc" in short and "mazeed" not in short      # 10 products: the whole list, no 'sab dikhao' needed
    assert "Wheat Seed" in full and "Zinc" in full and "more" not in full


def test_a_stock_question_about_one_godown_answers_for_that_godown(p):
    rows = p.ops.get_stock("UREA-50")
    txt = answers.render("get_stock", rows, p.repo, "aur vehari mei? UREA-50 stock kitna hai")
    assert "Vehari Godown" in txt and "150" in txt and "Multan" not in txt


def test_who_owes_the_most_is_read_from_the_rows(p):
    txt = answers.render("aging_report", p.ops.aging_report(), p.repo, "sab se zyada kis ka he")
    assert txt.startswith("Sab se zyada Malik Agro Store ke baqi hain: Rs 385,000.")


def test_credit_limit_is_shown_when_asked(p):
    txt = answers.render("get_customer_khata", p.ops.get_customer_khata("C-001"), p.repo, "malik agro ki credit limit kitni he")
    assert "Rs 1,200,000" in txt


def test_a_sent_reminder_says_it_was_sent(p):
    d = {"reminder_id": "REM-1", "customer_id": "C-007", "tier": "gentle", "amount_due": 58000, "status": "sent"}
    txt = answers.render("send_reminder", d, p.repo, "send it")
    assert "sent" in txt and "only when" not in txt


# ------------------------------------------------------------------ reading the words
def test_a_cheque_number_is_a_reference_not_an_amount():
    assert amount_in("al barakah ka cheque aya he 50000 ka HBL cheque no 004512").amount == 50000


def test_tareekh_is_a_date_not_a_product(p):
    assert date_in("30 tareekh tak") is not None
    op = analyse_order("haji sons ne kaha 30 tareekh tak 20000 de dega", p.repo)
    assert not op.unknown


def test_two_names_of_one_product_side_by_side_are_one_line(p):
    op = analyse_order("rana brothers ko 3 bori urea aur 1 cyper spray", p.repo)
    assert op.ready and sorted((i["sku"], i["qty"]) for i in op.items) == [("CYPER-1L", 1), ("UREA-50", 3)]


def test_a_godown_name_is_never_looked_up_as_a_product(p):
    op = analyse_order("vehari mei dap kam he, multan se 30 bori vehari bhej do", p.repo)
    assert "vehari" not in op.unknown


@pytest.mark.parametrize("text", ["haan", "ji", "theek he bana do", "ok kar do", "haan wahi", "ye wala"])
def test_short_yes(text):
    assert FU.is_yes(text)


@pytest.mark.parametrize("text", ["nahi", "haji sons", "50000", "haan 20 urea aur"])
def test_not_a_yes(text):
    assert not FU.is_yes(text)


def test_a_pick_by_a_word_of_the_name():
    cands = [{"id": "C-007", "name": "Bhatti Kisan Store"}, {"id": "C-010", "name": "New Kisan Dost"}]
    assert FU.by_name_word("new wala", cands) == 2 and FU.by_name_word("bhatti", cands) == 1 and FU.by_name_word("kisan", cands) is None


def test_corrections_read_old_and_new():
    assert TURNS.old_new("galti ho gayi 15000 nahi 12000 the") == (15000, 12000)
    assert TURNS.batch_of("unko confirm kar do sab")[0] == "confirm"
    assert TURNS.batch_of("isko confirm kar do") is None
    assert TURNS.asks_pending("koi approval pending he?") and TURNS.asks_pending("approvals dikhao")
    assert not TURNS.asks_pending("sab approve kar do") and not TURNS.asks_pending("pending orders dikhao")


# ------------------------------------------------------------------ while a card waits: nothing is dropped
def test_a_second_order_while_the_first_waits_gets_its_own_card_and_the_first_graph_is_untouched(p):
    first = p.handle_message("t", "clerk", "chaudhry farms ko 20 urea 5 dap", user=CLERK)
    cfg = p._cfg("t", "clerk", "order")
    before = p.specialists["order"].agent.get_state(cfg)
    r = p.handle_message("t", "clerk", "rana brothers 10 bori npk aur 2 zinc", user=CLERK)
    assert r.pending and r.pending.args["customer_id"] == "C-005" and r.pending.approval_id != first.pending.approval_id
    after = p.specialists["order"].agent.get_state(cfg)
    assert after.values["messages"] == before.values["messages"] and after.interrupts        # the paused graph was never invoked
    # each resolves on its own graph: the second first, then the first -- two orders, exactly as carded
    p.resolve(r.pending.approval_id, True, "clerk", user=OTHER)
    p.resolve(first.pending.approval_id, True, "clerk", user=OTHER)
    got = sorted((o.customer_id, tuple((i.sku, i.qty) for i in o.items)) for o in p.repo.list_orders(status="draft"))
    assert got == [("C-002", (("UREA-50", 20), ("DAP-50", 5))), ("C-005", (("NPK-25", 10), ("ZINC-10", 2)))]


def test_the_same_request_again_is_not_carded_twice(p):
    p.handle_message("t", "clerk", "chaudhry farms ko 20 urea 5 dap", user=CLERK)
    r = p.handle_message("t", "clerk", "chaudhry farms ko 20 urea 5 dap", user=CLERK)
    assert r.pending is None and r.waiting is not None and len(p.pending_items("t")) == 1


def test_a_salesman_is_never_told_to_approve_and_his_question_is_answered(p):
    p.handle_message("s", "salesman", "Rana Brothers ko 5 dap bhej do", user="Imran")
    r = p.handle_message("s", "salesman", "Rana Brothers ka balance kitna hai?", user="Imran")
    assert r.pending is None and "Rana Brothers" in visible(r.text) and "baqi" in visible(r.text)
    held = p.handle_message("s", "salesman", "haan", user="Imran")
    assert "Approve or reject" not in held.text


def test_pending_approvals_are_listed_for_whoever_can_decide(p):
    p.handle_message("c", "clerk", "haji sons ko 5 urea", user=CLERK)
    r = p.handle_message("o", "owner", "koi approval pending he?", user="Owner")
    assert r.pending is None and "1 card(s) waiting for you to approve" in r.text and "Haji Sons" in r.text
    mine = p.handle_message("c", "clerk", "kya pending he", user=CLERK)
    assert "Your own request" in mine.text


# ------------------------------------------------------------------ the driver at the door
def _loaded(p):
    oid = p.repo.create_order("C-002", [{"sku": "UREA-50", "qty": 20}, {"sku": "DAP-50", "qty": 5}], "chat", "f", "f").order_id
    p.repo.confirm_order(oid, "f", "f")
    p.repo.allocate_order(oid, "WH-MULTAN", "f", "f")
    from munshi.domain.models import business_today
    plan = p.repo.create_dispatch_plan(business_today().isoformat(), "R-MULTAN-N", "V-01", [oid], "f")
    p.repo.approve_dispatch_plan(plan.plan_id, "f", "f")
    st = p.repo.list_stops(plan.plan_id)[0]
    return st.stop_id, p.repo.get_stop(st.stop_id).otp


def test_driver_asks_what_to_collect_and_where(p):
    _loaded(p)
    r = p.handle_message("d", "driver", "kitne paise lene hein chaudhry farms se", user="Driver")
    assert r.specialist == "delivery" and "Rs 108,250" in r.text and "Chak 5-Faiz" in r.text
    assert "STP-" not in visible(r.text)


def test_driver_closes_by_name_and_retries_a_wrong_code(p):
    stop, otp = _loaded(p)
    bad = "0000" if otp != "0000" else "1111"
    r = p.handle_message("d", "driver", f"chaudhry farms pe maal de diya sab, 50000 cash liya code {bad}", user="Driver")
    assert p.repo.get_stop(stop).status == "pending" and "code" in r.text.lower()
    r = p.handle_message("d", "driver", f"sorry code {otp} he", user="Driver")
    assert p.repo.get_stop(stop).status != "pending", r.text
    assert p.repo.get_stop(stop).cash_collected == 50000


def test_customer_not_there_tells_the_office_and_never_closes(p):
    stop, _ = _loaded(p)
    r = p.handle_message("d", "driver", "chaudhry farms wale ghar pe nahi the dukan band thi", user="Driver")
    assert p.repo.get_stop(stop).status == "pending" and "office" in r.text and r.pending is None
    assert any(n["kind"] == "delivery_failed" for n in p.repo.notifications("clerk"))


def test_a_hand_in_by_the_runs_name_needs_no_plan_id(p):
    _loaded(p)                                                   # today's one loaded run: Multan North on V-01
    r = p.handle_message("c", "clerk", "driver ne 18000 jama karwaye he multan gaari ka", user=CLERK)
    assert r.pending and r.pending.tool == "record_deposit" and r.pending.args["amount_counted"] == 18000, r.text


def test_rate_on_a_waiting_purchase_card_replaces_it(p):
    r = p.handle_message("k", "clerk", "drip line ke 50 aur stock ayein hein ali akbar se", user=CLERK)
    assert r.pending and r.pending.tool == "record_purchase" and r.pending.args["supplier_id"] == "S-003"
    r2 = p.handle_message("k", "clerk", "rate 2800 tha", user=CLERK)
    assert r2.pending and r2.pending.args["items"][0]["unit_cost"] == 2800 and r2.pending.args["items"][0]["qty"] == 50, r2.text
    assert len(p.pending_items("k")) == 1


# ------------------------------------------------------------------ money by name
def test_a_bounced_cheque_by_name_raises_the_reversal_of_that_receipt(p):
    r = p.handle_message("o", "owner", "al barakah ne 50000 ka cheque diya", user="Owner")
    assert "by cheque" in visible(r.text)
    p.resolve(r.pending.approval_id, True, "owner", user="Owner")
    rcp = next(e for e in reversed(p.repo.ledger_for("C-004")) if e.kind == "payment" and e.method == "cheque")
    r = p.handle_message("o", "owner", "al barakah ka 50000 ka cheque bounce ho gaya he", user="Owner")
    assert r.pending and r.pending.tool == "reverse_ledger_entry" and r.pending.args["entry_id"] == rcp.entry_id
    # the customer was told 'balance now ...' when it was recorded: a templated correction goes out with the reversal
    p.resolve(r.pending.approval_id, True, "owner", user="Owner")
    bal = p.repo.outstanding("C-004")
    assert any("cheque returned unpaid" in m["text"] and f"Rs {bal:,.0f}" in m["text"] for m in p.repo.outbox())


def test_a_post_approval_payment_correction_explains_the_reversal_and_changes_nothing(p):
    r = p.handle_message("t", "clerk", "chaudhry farms ne 15000 easypaisa kiye", user=CLERK)
    p.resolve(r.pending.approval_id, True, "clerk", user=OTHER)
    n = len(p.repo.ledger_for("C-002"))
    r = p.handle_message("t", "clerk", "galti ho gayi 15000 nahi 12000 the", user=CLERK)
    assert r.pending is None and "reverse" in r.text and "order" not in r.text.lower()
    assert len(p.repo.ledger_for("C-002")) == n


# ------------------------------------------------------------------ a rate-limited model is not asked again for a while (G15)
class RateLimitError(Exception):
    pass


def test_a_daily_rate_limit_stops_the_model_calls_for_a_while():
    from langchain_core.language_models.chat_models import BaseChatModel

    class Limited(BaseChatModel):
        calls: int = 0

        @property
        def _llm_type(self) -> str:
            return "limited"

        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            object.__setattr__(self, "calls", self.calls + 1)
            raise RateLimitError("Error code: 429 - Rate limit reached on tokens per day (TPD): Limit 200000. Please try again in 7m30s.")

    m = Limited()
    p = MunshiPlatform(seeded_repository(), model=m)
    r1 = p.handle_message("t", "clerk", "bhai woh cheez ka kya bana")
    assert r1.model_error and m.calls >= 1
    n = m.calls
    r2 = p.handle_message("t", "clerk", "aur woh doosri cheez ka kya bana")
    assert r2.model_error == "ModelUnavailable" and m.calls == n        # answered from the rules at once
    assert 400 <= MunshiPlatform.rate_limit_wait(RateLimitError("429 ... tokens per day (TPD) ... try again in 7m30s")) <= 460
    assert MunshiPlatform.rate_limit_wait(ValueError("bad request")) == 0
    p.close()


# ------------------------------------------------------------------ the model path: update_order is checked like create_order
def test_the_guard_checks_an_update_order_call(p):
    oid = p.repo.create_order("C-005", [{"sku": "NPK-25", "qty": 10}], "chat", "f", "f").order_id
    ok = guard.check_call("update_order", {"order_id": oid, "items": [{"sku": "NPK-25", "qty": 15}]}, "rana brothers ke order mei npk 15 kar do", p.repo)
    assert ok is None
    wrong_qty = guard.check_call("update_order", {"order_id": oid, "items": [{"sku": "NPK-25", "qty": 51}]}, "rana brothers ke order mei npk 15 kar do", p.repo)
    assert wrong_qty
    other = p.repo.create_order("C-005", [{"sku": "ZINC-10", "qty": 1}], "chat", "f", "f").order_id
    two = guard.check_call("update_order", {"order_id": other, "items": [{"sku": "NPK-25", "qty": 15}]}, "rana brothers ke order mei npk 15 kar do", p.repo)
    assert two                                               # two open drafts: the model may not pick one the user never named
