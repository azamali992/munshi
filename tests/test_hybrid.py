"""The hybrid: the rules engine first, a real model only for what it didn't understand, and code checking
every model tool call against the message (agents/guard.py). All offline: FakeChat plays the real model,
with scripted routes and tool calls, and enforces the provider's tool-call protocol on every request."""
from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict

from munshi.agents.guard import GUARD_KEY, check_call
from munshi.agents.manager import CLARIFY
from munshi.domain.models import Customer
from munshi.domain.repository import MunshiRepository
from munshi.domain.seed import seeded_repository
from munshi.llm.parse import analyse_order
from munshi.platform import DETAILS, MODEL_APPROVAL_PREFIX, MunshiPlatform, engine_of, visible


def _protocol(messages: list[BaseMessage]) -> None:
    """What OpenAI-compatible APIs (Groq included) enforce with a 400."""
    for i, m in enumerate(messages):
        if isinstance(m, AIMessage) and m.tool_calls:
            want = {tc["id"] for tc in m.tool_calls}
            got, j = set(), i + 1
            while j < len(messages) and isinstance(messages[j], ToolMessage):
                got.add(messages[j].tool_call_id)
                j += 1
            if want - got:
                raise ValueError(f"400: tool_calls {sorted(want - got)} have no tool response")
        if isinstance(m, ToolMessage) and not (isinstance(m.content, str) or (isinstance(m.content, list) and m.content)):
            raise ValueError("400: 'role:tool' content must be a string or a non-empty array")


class FakeChat(BaseChatModel):
    """A scripted 'real' model. `routes`: message -> specialist the manager picks (absent = no route).
    `plan`: message -> steps, each a list of (tool, args) emitted in one message. `final`: message -> closing text.
    `fail`: messages for which every request raises (a provider timeout). `fail_after`: message -> fail once
    this many specialist requests have succeeded. `requests` logs every request's message text."""

    routes: dict = {}
    plan: dict = {}
    final: dict = {}
    fail: set = set()
    fail_after: dict = {}
    requests: list = []
    _tools: list[str] = []
    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "fake-real"

    def bind_tools(self, tools, **kwargs):
        c = self.model_copy()
        c._tools = [getattr(t, "name", str(t)) for t in tools]
        return c

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        _protocol(messages)
        h = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        text = str(messages[h].content)
        is_router = any(n.startswith("route_to_") for n in self._tools)
        self.requests.append(("route" if is_router else "act", text))
        if text in self.fail:
            raise TimeoutError("simulated provider timeout")
        if is_router:
            spec = self.routes.get(text)
            msg = AIMessage(content="", tool_calls=[{"name": f"route_to_{spec}", "args": {}, "id": "route_1"}]) if spec else AIMessage(content="?")
            return ChatResult(generations=[ChatGeneration(message=msg)])
        acts = sum(1 for k, t in self.requests if k == "act" and t == text)
        if text in self.fail_after and acts > self.fail_after[text]:
            raise TimeoutError("simulated provider timeout mid-turn")
        steps = self.plan.get(text, [])
        done = sum(1 for m in messages[h + 1:] if isinstance(m, AIMessage) and m.tool_calls)
        if done < len(steps):
            calls = [{"name": n, "args": a, "id": f"call_{h}_{done}_{k}"} for k, (n, a) in enumerate(steps[done])]
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))])
        results = [str(m.content)[:80] for m in messages[h + 1:] if isinstance(m, ToolMessage)]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.final.get(text, "Theek hai -- " + " | ".join(results))))])


def _fake(**kw) -> FakeChat:
    kw.setdefault("requests", [])
    return FakeChat(**kw)


def _malik(repo) -> None:
    repo.upsert_customer(Customer("C-012", "Malik Seeds", "0300-1111012", "standard", 300_000, "R-VEHARI", address="Mailsi"))


class _NoRoute:
    """A rules manager that routes nowhere -- the rules engine's explicit "didn't understand" -- so a message the
    rules would handle can be put in front of the model engine on purpose."""

    def invoke(self, inputs, config=None):
        return {"messages": list(inputs["messages"])}


@pytest.fixture
def repo():
    r = seeded_repository()
    _malik(r)
    yield r


def _makai(repo) -> list[dict]:
    return analyse_order("Malik Seeds ko 8 makai", repo).items


UNREAD = "Rana sahab wala kaam kar do jo kal kaha tha"            # the rules manager routes this nowhere


# ====================================================================== deterministic first
def test_rules_answer_without_any_model_call(repo):
    fake = _fake()
    p = MunshiPlatform(repo, model=fake)
    r = p.handle_message("t1", "clerk", "Rana Brothers ka balance?")
    assert r.engine == "rules" and r.model_calls == 0 and "C-005" in r.text
    card = p.handle_message("t2", "clerk", "Malik Seeds ko 8 makai", user="Bilal")
    assert card.pending and card.pending.args["customer_id"] == "C-012" and card.engine == "rules"
    assert engine_of(card.pending.approval_id) == "rules"
    ask = p.handle_message("t3", "clerk", "Malik ko 8 makai")              # a clarifying question is an answer too
    assert ask.pending is None and "Malik Agro Store" in ask.text and "Malik Seeds" in ask.text and ask.engine == "rules"
    assert "C-0" not in ask.text                                       # names, never record codes
    assert fake.requests == []


def test_only_didnt_understand_reaches_the_model(repo):
    fake = _fake(routes={UNREAD: "order", "bhai woh cheez bhej do": "order"},
                 final={UNREAD: "Kaunsa kaam? Customer aur maal ka naam batayein.", "bhai woh cheez bhej do": "Kis customer ko, aur kya?"})
    p = MunshiPlatform(repo, model=fake)
    r = p.handle_message("t", "clerk", UNREAD)                           # routed nowhere by the rules
    assert r.engine == "model" and r.specialist == "order" and r.text.startswith("Kaunsa kaam")
    assert r.model_calls == 2                                            # one routing request, one answer
    r2 = p.handle_message("t2", "clerk", "bhai woh cheez bhej do")      # routed, but the specialist didn't understand
    assert r2.engine == "model" and r2.text == "Kis customer ko, aur kya?"
    assert [t for _, t in fake.requests] == [UNREAD, UNREAD, "bhai woh cheez bhej do", "bhai woh cheez bhej do"]
    # the chat log says which engine answered and what it cost
    last = p.repo.chat_history("t")[-1]
    assert last["meta"]["engine"] == "model" and last["meta"]["model_calls"] == 2


def test_model_routing_nowhere_keeps_the_rules_reply(repo):
    fake = _fake()
    p = MunshiPlatform(repo, model=fake)
    r = p.handle_message("t", "clerk", "asdf qwer zxcv")
    assert r.text == CLARIFY and r.specialist is None and r.model_calls == 1 and r.pending is None


def test_stub_platform_has_no_model_engine_and_is_unchanged():
    p = MunshiPlatform()
    assert p.llm_specialists is None and not p.hybrid
    r = p.handle_message("t", "clerk", "bhai woh cheez bhej do")
    assert r.engine == "rules" and r.text.startswith("Tell me the customer and the items")


# ====================================================================== code resolves entities for the model
def test_guard_refuses_the_observed_malik_seeds_wrong_id(repo):
    items = _makai(repo)
    q = check_call("create_order", {"customer_id": "C-001", "items": items}, "Malik Seeds ko 8 makai", repo)
    assert q and "Malik Seeds" in q and "Malik Agro Store" in q and "C-0" not in q
    assert {c["id"] for c in q.candidates} == {"C-001", "C-012"}           # the ids stay in the open question, not the text
    assert check_call("create_order", {"customer_id": "C-012", "items": items}, "Malik Seeds ko 8 makai", repo) is None
    amb = check_call("create_order", {"customer_id": "C-001", "items": items}, "Malik ko 8 makai", repo)
    assert amb and {c["id"] for c in amb.candidates} == {"C-001", "C-012"}   # ambiguous: ask, never pick


def test_guard_checks_quantities_amounts_methods_and_references(repo):
    items = _makai(repo)
    wrong_qty = [dict(items[0], qty=80)]
    assert check_call("create_order", {"customer_id": "C-012", "items": wrong_qty}, "Malik Seeds ko 8 makai", repo)
    dozen = analyse_order("Rana Brothers ko 2 dozen imida", repo).items             # quantities as parse.py reads them
    assert dozen == [{"sku": "IMIDA-250", "qty": 24}]
    assert check_call("create_order", {"customer_id": "C-005", "items": dozen}, "Rana Brothers ko 2 dozen imida", repo) is None
    assert check_call("record_payment", {"customer_id": "C-005", "amount": 5000, "method": "cash"}, "Rana Brothers ne 50 hazar cash diye", repo)
    assert check_call("record_payment", {"customer_id": "C-005", "amount": 50000, "method": "cash"}, "Rana Brothers ne 50 hazar cash diye", repo) is None
    assert check_call("record_payment", {"customer_id": "C-005", "amount": 50000, "method": "bank"}, "Rana Brothers ne 50 hazar cash diye", repo)
    # an order id the message never mentioned (the model found or recalled it) is not the user's
    assert check_call("confirm_order", {"order_id": "ORD-ABCDEF12"}, "haji sons wala order confirm karo", repo)
    assert check_call("confirm_order", {"order_id": "ORD-ABCDEF12"}, "ORD-ABCDEF12 confirm karo", repo) is None
    # a pronoun may lean on the conversation, and only on the customer it last named
    hist = ["Rana Brothers ka balance?", 'Done -- {"customer": {"customer_id": "C-005"}}']
    assert check_call("record_payment", {"customer_id": "C-005", "amount": 20000, "method": "cash"}, "uska 20000 cash aa gaya", repo, history=hist) is None
    assert check_call("record_payment", {"customer_id": "C-003", "amount": 20000, "method": "cash"}, "uska 20000 cash aa gaya", repo, history=hist)
    # reads are checked only when the message names someone
    assert check_call("get_customer_khata", {"customer_id": "C-001"}, "Malik Seeds ka khata", repo)
    assert check_call("get_customer_khata", {"customer_id": "C-001"}, "sab se purana udhaar kis ka hai", repo) is None


def test_model_wrong_id_becomes_a_question_not_a_card(repo):
    items = _makai(repo)
    text = "Malik Seeds ko 8 makai"
    fake = _fake(routes={text: "order"}, plan={text: [[("find_customer", {"text": "Malik"})], [("create_order", {"customer_id": "C-001", "items": items})]]})
    p = MunshiPlatform(repo, model=fake)
    p.manager = _NoRoute()
    r = p.handle_message("t", "clerk", text, user="Bilal")
    assert r.engine == "model" and r.pending is None and not p.pending
    assert "Which customer" in r.text and "Malik Seeds" in r.text and "C-012" not in r.text
    st = p.llm_specialists["order"].agent.get_state(p._cfg("t", "clerk", "order", "model"))
    assert not st.interrupts and st.values["messages"][-1].response_metadata[GUARD_KEY]["tool"] == "create_order"
    # the thread stays healthy: the next model request on it passes the provider's protocol check
    fake.routes["aur kuch?"] = "order"
    assert p.handle_message("t", "clerk", "aur kuch?").engine == "model"


def test_a_pronoun_follow_up_is_grounded_in_the_conversation_across_engines(repo):
    text = "uska 20000 cash aa gaya"
    fake = _fake(routes={text: "hisaab"}, plan={text: [[("record_payment", {"customer_id": "C-005", "amount": 20000, "method": "cash"})]]})
    p = MunshiPlatform(repo, model=fake)
    first = p.handle_message("t", "clerk", "Rana Brothers ka balance?")           # answered by the RULES engine
    assert first.engine == "rules" and "C-005" in first.text
    p.manager = _NoRoute()
    r = p.handle_message("t", "clerk", text, user="Bilal")                        # the MODEL engine, same conversation
    assert r.pending and r.pending.args["customer_id"] == "C-005" and r.engine == "model"
    # the same follow-up with another customer's id is not the one the conversation was about
    fake.plan["uska 20000 cash aa gaya "] = [[("record_payment", {"customer_id": "C-003", "amount": 20000, "method": "cash"})]]
    fake.routes["uska 20000 cash aa gaya "] = "hisaab"
    q = p.handle_message("t2", "clerk", "uska 20000 cash aa gaya ", user="Bilal")  # a fresh thread: no history at all
    assert q.pending is None and "Which customer" in q.text


def test_model_lookup_is_never_a_guess():
    r = seeded_repository()
    _malik(r)
    from munshi.tools.core import MunshiTools
    ops = MunshiTools(r)
    amb = ops.find_customer("Malik")
    assert amb["found"] is False and amb["ambiguous"] and {c["customer_id"] for c in amb["candidates"]} == {"C-001", "C-012"}
    assert ops.find_customer("Malik Seeds")["customer_id"] == "C-012"
    assert ops.find_customer("C-005")["name"] == "Rana Brothers"


def test_a_correct_model_call_becomes_a_card_and_is_resumed_by_the_model_engine(repo):
    items = _makai(repo)
    text = "Malik Seeds ko 8 makai"
    fake = _fake(routes={text: "order"}, plan={text: [[("find_customer", {"text": "Malik Seeds"})], [("create_order", {"customer_id": "C-012", "items": items})]]},
                 final={text: "Order ban gaya."})
    p = MunshiPlatform(repo, model=fake)
    p.manager = _NoRoute()
    r = p.handle_message("t", "clerk", text, user="Bilal")
    assert r.pending and r.pending.tool == "create_order" and r.pending.args["customer_id"] == "C-012"
    assert r.pending.approval_id.startswith(MODEL_APPROVAL_PREFIX) and engine_of(r.pending.approval_id) == "model"
    assert r.pending.needs_role == "clerk" and r.engine == "model"
    # four-eyes unchanged: the requester can't approve their own card
    with pytest.raises(PermissionError):
        p.resolve(r.pending.approval_id, True, "clerk", user="Bilal")
    before = len(p.repo.list_orders(customer_id="C-012"))
    out = p.resolve(r.pending.approval_id, True, "clerk", user="Sana")
    assert out.engine == "model" and out.model_calls == 1
    # what happened is said by code from the tool result, never by the model's follow-up prose
    assert visible(out.text).startswith("Approved. Draft order") and "Malik Seeds" in out.text and "Order ban gaya." not in out.text
    assert len(p.repo.list_orders(customer_id="C-012")) == before + 1
    # the rules engine's graph for this thread never saw the call
    assert not p.specialists["order"].agent.get_state(p._cfg("t", "clerk", "order")).values


def test_a_card_blocks_both_engines_on_its_specialist(repo):
    items = _makai(repo)
    text = "Malik Seeds ko 8 makai"
    fake = _fake(routes={text: "order", UNREAD: "order"}, plan={text: [[("create_order", {"customer_id": "C-012", "items": items})]]})
    p = MunshiPlatform(repo, model=fake)
    card = p.handle_message("t", "clerk", text, user="Bilal")              # the RULES engine's card
    assert card.pending and engine_of(card.pending.approval_id) == "rules"
    r = p.handle_message("t", "clerk", UNREAD, user="Bilal")               # the model routes to order: held, not invoked
    assert r.waiting and r.waiting.approval_id == card.pending.approval_id and r.pending is None
    assert [k for k, _ in fake.requests] == ["route"]


# ====================================================================== restart: each card resumes on its own engine
def test_cards_resume_on_the_engine_that_raised_them_after_a_restart(tmp_path):
    db, ck = str(tmp_path / "biz.sqlite"), str(tmp_path / "ck.sqlite")
    repo = seeded_repository(db)
    _malik(repo)
    items = _makai(repo)
    text = "Malik Seeds ko 8 makai"
    fake = _fake(routes={text: "order"}, plan={text: [[("create_order", {"customer_id": "C-012", "items": items})]]}, final={text: "Ho gaya."})
    p = MunshiPlatform(repo, model=fake, checkpoint_path=ck)
    rules_card = p.handle_message("t-rules", "clerk", "Rana Brothers ko 10 urea", user="Bilal").pending
    p.manager = _NoRoute()
    model_card = p.handle_message("t-model", "clerk", text, user="Bilal").pending
    assert engine_of(rules_card.approval_id) == "rules" and engine_of(model_card.approval_id) == "model"
    p.close()

    # restart with the model configured
    fake2 = _fake(final={text: "Ho gaya."})
    p2 = MunshiPlatform(MunshiRepository(db), model=fake2, checkpoint_path=ck)
    assert {x.approval_id for x in p2.pending_items()} == {rules_card.approval_id, model_card.approval_id}
    a = p2.resolve(rules_card.approval_id, True, "clerk", user="Sana")
    assert a.engine == "rules" and a.model_calls == 0 and "ORD-" in visible(a.text) and DETAILS in a.text and fake2.requests == []
    b = p2.resolve(model_card.approval_id, True, "clerk", user="Sana")
    assert b.engine == "model" and visible(b.text).startswith("Approved. Draft order") and "Ho gaya" not in b.text and fake2.requests == [("act", text)]
    assert [o.customer_id for o in p2.repo.list_orders(limit=2)] and not p2.pending
    p2.close()


def test_a_model_card_still_resumes_after_restarting_without_a_model(tmp_path):
    db, ck = str(tmp_path / "biz.sqlite"), str(tmp_path / "ck.sqlite")
    repo = seeded_repository(db)
    _malik(repo)
    text = "Malik Seeds ko 8 makai"
    fake = _fake(routes={text: "order"}, plan={text: [[("create_order", {"customer_id": "C-012", "items": _makai(repo)})]]})
    p = MunshiPlatform(repo, model=fake, checkpoint_path=ck)
    p.manager = _NoRoute()
    card = p.handle_message("t", "clerk", text, user="Bilal").pending
    p.close()
    p2 = MunshiPlatform(MunshiRepository(db), checkpoint_path=ck)            # LLM_PROVIDER=stub now
    n = len(p2.repo.list_orders(customer_id="C-012"))
    out = p2.resolve(card.approval_id, True, "clerk", user="Sana")
    assert out.engine == "model" and out.model_calls == 0 and DETAILS in out.text and "Malik Seeds" in visible(out.text)
    assert len(p2.repo.list_orders(customer_id="C-012")) == n + 1
    p2.close()


# ====================================================================== robustness
def test_model_failure_answers_from_the_rules(repo):
    fake = _fake(fail={UNREAD})
    p = MunshiPlatform(repo, model=fake)
    r = p.handle_message("t", "clerk", UNREAD)
    assert r.text == CLARIFY and r.model_error == "TimeoutError" and r.pending is None
    assert p.repo.chat_history("t")[-1]["meta"]["model_error"] == "TimeoutError"


def test_model_failing_mid_turn_leaves_no_half_state(repo):
    text = "bhai woh cheez bhej do"
    fake = _fake(routes={text: "order", "aur?": "order"}, plan={text: [[("list_orders", {"status": "draft"})], [("get_order", {"order_id": "x"})]]},
                 fail_after={text: 1}, final={"aur?": "Ji?"})
    p = MunshiPlatform(repo, model=fake)
    r = p.handle_message("t", "clerk", text)                                # the read ran, then the provider timed out
    assert r.model_error == "TimeoutError" and r.text.startswith("Tell me the customer") and r.pending is None
    cfg = p._cfg("t", "clerk", "order", "model")
    st = p.llm_specialists["order"].agent.get_state(cfg)
    assert not st.interrupts
    _protocol(st.values["messages"])                                       # nothing dangling
    nxt = p.handle_message("t", "clerk", "aur?")                           # and the thread still works
    assert nxt.engine == "model" and nxt.text == "Ji?"


def test_an_empty_tool_result_goes_back_as_text(repo):
    """Observed on Groq: a tool returning [] became a tool message with content [] and the provider answered 400."""
    text = "bhai woh cheez bhej do"
    fake = _fake(routes={text: "order"}, plan={text: [[("list_orders", {"status": "short"})]]}, final={text: "Koi order nahi."})
    p = MunshiPlatform(repo, model=fake)
    r = p.handle_message("t", "clerk", text)
    assert r.model_error is None and "short" in r.text and "Koi order nahi." not in r.text      # code says it, from the []


def test_a_claimed_action_that_never_happened_is_not_passed_on(repo):
    """Observed on Groq: 'Green Valley ka payment record kar diya gaya: 25,000' with no tool call at all.
    (The message observed then -- 'Green Valley ka banda aaya tha, raqam de gaya pachees hazar' -- is now read by the rules
    as a payment card; a message the rules still can't read stands in for it here, so the model is reached.)"""
    text = "Green Valley wala scene set ho gaya"
    fake = _fake(routes={text: "hisaab"}, plan={text: [[("find_customer", {"text": "Green Valley"})]]},
                 final={text: "Green Valley ka payment record kar diya gaya: 25,000 PKR."})
    p = MunshiPlatform(repo, model=fake)
    p.manager = _NoRoute()          # (the rules now read '<customer> ... scene' as a khata question: put it in front of the model)
    r = p.handle_message("t", "clerk", text)
    assert r.engine == "model" and r.pending is None
    # the turn ran a lookup: the reply is what code read (Green Valley's balance), the claim and its number are gone
    assert "record kar diya" not in r.text and "25,000" not in r.text and "Green Valley Seeds" in r.text
    assert p.repo.chat_history("t")[-1]["meta"]["model_text"].startswith("Green Valley ka payment")
    # with no tool at all, the claim is replaced by "nothing was recorded" and the didn't-understand reply
    fake.plan[text] = []
    r = p.handle_message("t2", "clerk", text)
    assert r.text.startswith("Nothing was recorded") and "record kar diya" not in r.text and "25,000" not in r.text
    assert p.repo.chat_history("t2")[-1]["meta"]["claim_blocked"].startswith("Green Valley ka payment")
    # a question or a plain answer is not a claim
    assert not MunshiPlatform._claims_done("Kya delivery ho gayi?")
    assert not MunshiPlatform._claims_done("Nothing was recorded yet.")
    assert not MunshiPlatform._claims_done("Bhatti Kisan Store ka balance Rs 58,000 hai.")
    assert MunshiPlatform._claims_done("Order bana diya hai. Kuch aur?")


def test_the_model_is_told_the_reply_script_in_code(repo):
    seen = []

    class Spy(FakeChat):
        def _generate(self, messages, stop=None, run_manager=None, **kw):
            if not any(n.startswith("route_to_") for n in self._tools):
                seen.append([str(m.content) for m in messages if isinstance(m, HumanMessage)])
            return super()._generate(messages, stop, run_manager, **kw)

    fake = Spy(routes={UNREAD: "order", "بھائی وہ چیز بھیج دو": "order"}, requests=[])
    p = MunshiPlatform(repo, model=fake)
    p.handle_message("t", "clerk", UNREAD)
    p.handle_message("u", "clerk", "بھائی وہ چیز بھیج دو")
    assert "Latin letters" in seen[0][-2] and seen[0][-1] == UNREAD
    assert "Urdu script" in seen[1][-2] and "Latin letters" not in seen[1][-2]


def test_an_urdu_script_tail_is_dropped_from_a_reply_to_roman_urdu(repo):
    """Observed on Groq: 'Malik Seeds ka khata: outstanding balance 0.00 PKR. کوئی اور مدد؟' to a Roman-Urdu message."""
    assert MunshiPlatform._latin_only("Malik Seeds ka khata: outstanding balance 0.00 PKR. کوئی اور مدد؟") == "Malik Seeds ka khata: outstanding balance 0.00 PKR."
    assert MunshiPlatform._latin_only("کھاتہ صاف ہے۔") == "کھاتہ صاف ہے۔"                  # all Urdu: left alone, never emptied
    text = "Bhatti sahab ka account dekhna hai zara"
    fake = _fake(routes={text: "hisaab"}, final={text: "Bhatti sahab kis ka naam hai? کوئی اور مدد؟"})
    p = MunshiPlatform(repo, model=fake)
    p.manager = _NoRoute()          # the rules now read 'account' as khata themselves: put this one in front of the model on purpose
    r = p.handle_message("t", "clerk", text)
    assert r.text == "Bhatti sahab kis ka naam hai?"           # a clarifying question survives grounding; its Urdu-script tail doesn't


def test_a_reply_in_content_parts_is_shown_as_plain_text(repo):
    """Observed on Gemini: the reply arrives as a list of content parts and was shown to the user raw,
    "[{'type': 'text', 'text': 'Bhai, kaunsi cheez ...'}]"."""
    text = "Bhatti sahab ka account dekhna hai zara"

    class Parts(FakeChat):
        def _generate(self, messages, stop=None, run_manager=None, **kw):
            out = super()._generate(messages, stop, run_manager, **kw)
            msg = out.generations[0].message
            if not msg.tool_calls and isinstance(msg.content, str) and msg.content:
                out.generations[0].message = AIMessage(content=[{"type": "text", "text": msg.content}])
            return out

    fake = Parts(routes={text: "hisaab"}, final={text: "Bhai, Bhatti Kisan Store ya koi aur Bhatti?"}, requests=[])
    p = MunshiPlatform(repo, model=fake)
    p.manager = _NoRoute()
    r = p.handle_message("t", "clerk", text)
    assert r.text == "Bhai, Bhatti Kisan Store ya koi aur Bhatti?"
    assert "'type'" not in r.text and not r.text.startswith("[")


def test_the_model_is_handed_what_code_read(repo):
    from munshi.agents.guard import hints
    assert hints("Malik Seeds ka maal: makai aath", repo) == "Code read this message as: customer Malik Seeds = C-012; items 8 x SEED-MAIZE."
    amb = hints("Malik ka maal: makai aath", repo)
    assert "ambiguous" in amb and "C-001" in amb and "C-012" in amb
    assert "amount 20000" in hints("Punjab Seed Mart walon ki raqam wusool hui, bees hazar", repo)
    assert hints("pichle hafte kitna kamaya", repo) == ""


def test_request_limits_come_from_the_environment(monkeypatch):
    from munshi.llm.factory import request_limits
    monkeypatch.setenv("LLM_TIMEOUT_S", "12")
    monkeypatch.setenv("LLM_MAX_RETRIES", "4")
    assert request_limits() == {"timeout_s": 12.0, "max_retries": 4}
    monkeypatch.setenv("LLM_TIMEOUT_S", "nonsense")
    assert request_limits()["timeout_s"] == 30.0


def test_groq_model_gets_timeout_retries_and_reasoning_effort(monkeypatch):
    pytest.importorskip("langchain_groq")
    from munshi.llm.factory import build_chat_model
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_not_a_real_key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("LLM_TIMEOUT_S", "20")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    m = build_chat_model("groq")
    assert m.request_timeout == 20.0 and m.max_retries == 1 and m.reasoning_effort == "low"
