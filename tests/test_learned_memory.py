"""Memory across conversations: learned names (what a person confirmed they mean by 'Bhatti sahab'), their precedence
against real names, re-pointing and forgetting, the card saying which memory it used, order habits ("wahi order
dobara"), ranking of "which one?" options, the V6 migration, the per-business cache, tenant isolation, and the model
guard agreeing with the rules on every learned name."""
from __future__ import annotations

import sqlite3
import time
from datetime import date, timedelta

import pytest

from munshi.agents import guard
from munshi.domain import migrations
from munshi.domain.models import Customer
from munshi.domain.repository import MunshiRepository
from munshi.domain.seed import seeded_repository
from munshi.llm import followup as FU
from munshi.llm import memory as MEM
from munshi.llm.parse import analyse_order, customer_resolution, supplier_resolution
from munshi.llm.text import phrase_key
from munshi.platform import MunshiPlatform, visible
from tests.test_hybrid import _fake, _NoRoute


@pytest.fixture
def repo():
    r = seeded_repository()
    r.upsert_customer(Customer("C-012", "Malik Seeds", "0300-1111012", "standard", 300_000, "R-VEHARI"))
    return r


def _bhatti_traders(r) -> None:
    r.upsert_customer(Customer("C-013", "Bhatti Traders", "0300-1111013", "standard", 300_000, "R-VEHARI"))


def _active(r) -> dict[str, str]:
    return {a["phrase_norm"]: a["entity_id"] for a in r.learned_aliases()}


def _teach_bhatti_by_approval(p: MunshiPlatform, requester: str = "Bilal", approver: str = "Sana"):
    """Day 1: 'Bhatti sahab ko 10 urea' -> which one? -> 'pehla' -> a card for Bhatti Kisan Store, approved."""
    q = p.handle_message("day1", "clerk", "Bhatti sahab ko 10 urea bhej do", user=requester)
    assert q.pending is None and "Bhatti Traders" in q.text and "Bhatti Kisan Store" in q.text
    card = p.handle_message("day1", "clerk", "pehla", user=requester)
    assert card.pending and card.pending.args["customer_id"] == "C-007"
    return card, p.resolve(card.pending.approval_id, True, "owner", user=approver)


# ====================================================================== learning
def test_an_approved_card_after_a_which_one_answer_teaches_the_phrase_and_it_works_next_day_in_a_new_thread(repo, monkeypatch):
    _bhatti_traders(repo)
    p = MunshiPlatform(repo)
    card, _ = _teach_bhatti_by_approval(p)
    rows = [a for a in repo.learned_aliases() if a["entity_kind"] == "customer"]
    assert len(rows) == 1
    a = rows[0]
    assert (a["phrase"], a["entity_id"], a["taught_by"], a["confirmed_by"]) == ("Bhatti sahab", "C-007", "Bilal", "Sana")
    assert a["source"] == f"answer:{card.pending.approval_id}" and a["taught_at"] and a["active"] == 1
    assert any(x["action"] == "alias_learned" and x["actor"] == "memory" for x in repo.audit_log(50))
    # the next day, a brand-new conversation: no per-conversation memory is left, the learned name is
    later = FU.now() + timedelta(days=1)
    monkeypatch.setattr(FU, "now", lambda: later)
    r = p.handle_message("day2", "clerk", "Bhatti sahab ko 4 dap bhej do", user="Bilal")
    assert r.pending and r.pending.args["customer_id"] == "C-007"
    assert "Bhatti sahab = Bhatti Kisan Store (remembered)" in r.text
    facts = p.card(r.pending)["facts"]
    assert {"key": "note", "value": "Bhatti sahab = Bhatti Kisan Store (remembered)"} in facts
    # a read leans on it the same way, and approving the card counts a confirmed use
    assert "Bhatti Kisan Store" in p.handle_message("day2b", "clerk", "bhatti sahab ka khata", user="Bilal").text
    p.resolve(r.pending.approval_id, True, "owner", user="Sana")
    assert repo.get_alias(a["alias_id"])["uses"] == 1


def test_a_plain_approved_card_pins_a_nickname_but_a_full_name_teaches_nothing(repo):
    p = MunshiPlatform(repo)
    card = p.handle_message("t", "clerk", "Bhatti sahab ko 10 urea bhej do", user="Bilal")      # only one Bhatti yet
    assert card.pending and card.pending.args["customer_id"] == "C-007" and "(remembered)" not in card.text
    p.resolve(card.pending.approval_id, True, "owner", user="Sana")
    full = p.handle_message("t2", "clerk", "Haji Sons ko 5 urea bhej do", user="Bilal")
    p.resolve(full.pending.approval_id, True, "owner", user="Sana")
    assert _active(repo) == {phrase_key("bhatti sahab"): "C-007"}
    # a second Bhatti arrives: 'Bhatti sahab' is still the one they confirmed -- and the card says so
    _bhatti_traders(repo)
    r = p.handle_message("t3", "clerk", "Bhatti sahab ko 2 dap bhej do", user="Bilal")
    assert r.pending and r.pending.args["customer_id"] == "C-007" and "Bhatti sahab = Bhatti Kisan Store" in r.text
    assert "Bhatti Traders" in p.handle_message("t4", "clerk", "Bhatti ko 2 dap bhej do", user="Bilal").text     # plain 'Bhatti' still asks


def test_rejected_undecided_and_warned_cards_teach_nothing(repo):
    _bhatti_traders(repo)
    p = MunshiPlatform(repo)
    for n, (thread, decision) in enumerate((("rej", False), ("open", None))):
        p.handle_message(thread, "clerk", "Bhatti sahab ko 10 urea bhej do", user="Bilal")
        card = p.handle_message(thread, "clerk", "pehla", user="Bilal")
        if decision is not None:
            p.resolve(card.pending.approval_id, decision, "owner", user="Sana")
            assert {ln["outcome"] for ln in repo.card_links(card.pending.approval_id)} == {"rejected"}
    assert _active(repo) == {}
    # a card with a warning (not enough stock) is approved, but it teaches nothing
    p.handle_message("warn", "clerk", "Bhatti sahab ko 1000 urea bhej do", user="Bilal")
    card = p.handle_message("warn", "clerk", "pehla", user="Bilal")
    assert card.pending and p.card(card.pending)["warnings"]
    p.resolve(card.pending.approval_id, True, "owner", user="Sana")
    assert _active(repo) == {} and {ln["outcome"] for ln in repo.card_links(card.pending.approval_id)} == {"skipped:warnings"}
    assert "Bhatti Traders" in p.handle_message("next", "clerk", "Bhatti sahab ko 4 dap bhej do", user="Bilal").text


def test_a_which_one_answer_on_a_read_teaches_only_for_the_owner_or_a_clerk(repo):
    p = MunshiPlatform(repo)
    p.handle_message("s", "salesman", "Malik ka khata dikhao", user="Imran")
    assert "Malik Seeds" in p.handle_message("s", "salesman", "Malik Seeds", user="Imran").text
    assert _active(repo) == {}
    p.handle_message("o", "owner", "Malik ka khata dikhao", user="Sultan")
    assert "Malik Seeds" in p.handle_message("o", "owner", "doosra", user="Sultan").text
    assert _active(repo) == {phrase_key("Malik"): "C-012"}
    r = p.handle_message("o2", "clerk", "Malik ko 3 zinc bhej do", user="Bilal")
    assert r.pending and r.pending.args["customer_id"] == "C-012" and "Malik = Malik Seeds (remembered)" in r.text


# ====================================================================== precedence
def test_a_fuller_name_or_the_exact_name_of_someone_else_beats_a_learned_name(repo):
    repo.learn_alias(phrase_key("Malik"), "Malik", "customer", "C-012", "test")
    assert customer_resolution("Malik ko 5 urea", repo).id == "C-012"
    full = customer_resolution("Malik Agro Store ko 5 urea", repo)          # the words were the name, not the nickname
    assert full.ok and full.id == "C-001" and full.alias is None
    repo.learn_alias(phrase_key("malik seeds"), "malik seeds", "customer", "C-001", "test")   # a memory that contradicts a name
    both = customer_resolution("Malik Seeds ko 5 urea", repo)
    assert both.status == "ambiguous" and {c.id for c in both.candidates} == {"C-001", "C-012"}


def test_a_phrase_that_later_names_someone_else_asks_instead_of_using_memory(repo):
    p = MunshiPlatform(repo)
    assert "Bhatti Kisan Store" in p.handle_message("t", "owner", "zamindar sahab matlab Bhatti Kisan Store hai", user="Sultan").text
    assert customer_resolution("zamindar sahab ko 5 urea", repo).id == "C-007"
    repo.upsert_customer(Customer("C-014", "Zamindar Traders", "", "standard", 300_000, "R-VEHARI"))
    r = p.handle_message("t2", "clerk", "zamindar sahab ko 5 urea bhej do", user="Bilal")
    assert r.pending is None and "Zamindar Traders" in r.text and "Bhatti Kisan Store" in r.text


def test_a_learned_name_and_another_customer_in_one_message_is_two_customers(repo):
    repo.learn_alias(phrase_key("Bhatti sahab"), "Bhatti sahab", "customer", "C-007", "test")
    op = analyse_order("Bhatti sahab aur Haji Sons ko 5 urea", repo)
    assert not op.ready and any(pr.startswith("two customers") for pr in op.problems)


def test_learned_names_match_across_spellings_and_scripts(repo):
    repo.learn_alias(phrase_key("Bhatti sahab"), "Bhatti sahab", "customer", "C-007", "test")
    _bhatti_traders(repo)
    for text in ("bhatti sahib ko 4 dap", "BHATTI SAAB ko 4 dap", "بھٹی صاحب کو 4 ڈی اے پی"):
        assert customer_resolution(text, repo).id == "C-007", text
    assert customer_resolution("bhatti ko 4 dap", repo).status == "ambiguous"


# ====================================================================== correct / forget / list
def test_repointing_is_logged_and_the_first_card_after_it_says_what_it_used_to_mean(repo):
    _bhatti_traders(repo)
    p = MunshiPlatform(repo)
    _teach_bhatti_by_approval(p)
    old = repo.learned_aliases("customer")[0]
    refused = p.handle_message("s", "salesman", "Bhatti sahab matlab Bhatti Traders hai", user="Imran").text.lower()
    assert "owner" in refused and "clerk" in refused
    assert _active(repo)[phrase_key("bhatti sahab")] == "C-007"
    said = p.handle_message("t", "clerk", "Bhatti sahab matlab Bhatti Traders hai", user="Bilal")
    assert said.pending is None and "Bhatti Traders" in said.text and "Bhatti Kisan Store" in said.text
    closed = repo.get_alias(old["alias_id"])
    new = repo.learned_aliases("customer")[0]
    assert (closed["active"], closed["end_reason"], closed["superseded_by"]) == (0, "repointed", new["alias_id"])
    assert (new["entity_id"], new["previous_entity_id"], new["taught_by"]) == ("C-013", "C-007", "Bilal")
    assert any(x["action"] == "alias_repointed" for x in repo.audit_log(50))
    first = p.handle_message("d2", "clerk", "Bhatti sahab ko 4 dap bhej do", user="Bilal")
    assert first.pending.args["customer_id"] == "C-013"
    assert "Bhatti sahab = Bhatti Traders (last time you meant Bhatti Kisan Store)" in first.text
    p.resolve(first.pending.approval_id, True, "owner", user="Sana")
    second = p.handle_message("d3", "clerk", "Bhatti sahab ko 2 dap bhej do", user="Bilal")
    assert second.pending.args["customer_id"] == "C-013" and "last time" not in second.text and "Bhatti sahab = Bhatti Traders" in second.text


def test_picking_a_different_one_for_the_same_phrase_repoints_it(repo):
    p = MunshiPlatform(repo)
    p.handle_message("a", "owner", "Malik ka khata dikhao", user="Sultan"); p.handle_message("a", "owner", "doosra", user="Sultan")
    assert _active(repo)[phrase_key("malik")] == "C-012"
    repo.learn_alias(phrase_key("malik"), "Malik", "customer", "C-001", "answer", taught_by="Sultan")      # a later pick of the other Malik
    rows = repo.alias_history()
    assert [(r["entity_id"], r["active"]) for r in rows[:2]] == [("C-001", 1), ("C-012", 0)] and rows[0]["previous_entity_id"] == "C-012"


def test_forgetting_and_listing(repo):
    p = MunshiPlatform(repo)
    p.handle_message("t", "owner", "FFC matlab Fauji Fertilizer hai", user="Sultan")
    p.handle_message("t", "owner", "nali matlab drip line hai", user="Sultan")
    listing = visible(p.handle_message("t", "owner", "kya kya yaad hai", user="Sultan").text)
    assert "FFC = Fauji Fertilizer (Multan depot) (supplier)" in listing and "nali = Drip Line 100m (product)" in listing and "Sultan" in listing
    assert "only the owner or a clerk" in p.handle_message("d", "driver", "learned names", user="Rafiq").text.lower()
    assert supplier_resolution("FFC ko 2 lakh bank transfer kar do", repo).id == "S-001"
    assert analyse_order("Haji Sons ko 3 nali", repo).items == [{"sku": "DRIP-100", "qty": 3}]
    assert "only the owner or a clerk" in p.handle_message("s", "salesman", "forget FFC", user="Imran").text.lower()
    assert "forgotten" in p.handle_message("t", "clerk", "forget FFC", user="Bilal").text
    assert "'nali' (it meant Drip Line 100m)" in p.handle_message("t", "clerk", "nali bhool jao", user="Bilal").text
    assert _active(repo) == {}
    assert supplier_resolution("FFC ko 2 lakh", repo).status == "none" and analyse_order("Haji Sons ko 3 nali", repo).unknown == ["nali"]
    assert {r["end_reason"] for r in repo.alias_history()} == {"forgotten"}            # nothing deleted
    assert "don't have 'shah ji' remembered" in p.handle_message("t", "clerk", "forget shah ji", user="Bilal").text
    assert "haven't learned any names" in p.handle_message("t", "owner", "what do you remember", user="Sultan").text


def test_teaching_refuses_a_phrase_that_is_already_someone_elses_name_and_unclear_targets(repo):
    p = MunshiPlatform(repo)
    assert "Rana Brothers" in p.handle_message("t", "owner", "rana ji matlab Chaudhry Farms hai", user="Sultan").text
    assert "drip" in p.handle_message("t", "owner", "drip matlab urea hai", user="Sultan").text.lower()
    assert "Malik Agro Store" in p.handle_message("t", "owner", "doctor sahab matlab Malik hai", user="Sultan").text        # which Malik?
    assert "don't know who" in p.handle_message("t", "owner", "doctor sahab means Qureshi", user="Sultan").text
    assert _active(repo) == {}


@pytest.mark.parametrize("text,kind", [("Bhatti sahab matlab Bhatti Traders hai", "teach"), ("bhatti sahab = C-013", "teach"),
                                       ("FFC means Fauji Fertilizer", "teach"), ("Bhatti sahab bhool jao", "forget"), ("forget bhatti sahab", "forget"),
                                       ("kya kya yaad hai", "list"), ("learned names", "list"), ("بھٹی صاحب مطلب بھٹی ٹریڈرز ہے", "teach")])
def test_memory_command_shapes(text, kind):
    assert MEM.command_of(text).kind == kind


@pytest.mark.parametrize("text", ["SKU ka matlab kya hai?", "Haji Sons ko yaad dehani bhejo", "Chaudhry Farms ko 20 urea bhej do", "urea ka rate kya hai"])
def test_ordinary_messages_are_not_memory_commands(text):
    assert MEM.command_of(text) is None


# ====================================================================== habits
def _deliver(r, cid: str, items: list[dict]) -> str:
    oid = r.create_order(cid, items, "chat", "t", "test").order_id
    r.confirm_order(oid, "test", "test"); r.allocate_order(oid, "WH-VEHARI", "test", "test")
    plan = r.create_dispatch_plan(date.today().isoformat(), "R-VEHARI", "V-03", [oid], "test")
    r.approve_dispatch_plan(plan.plan_id, "test", "test")
    st = r.list_stops(plan.plan_id)[0]
    r.close_stop(st.stop_id, [{"sku": i["sku"], "qty": i["qty"]} for i in items], [], 0, r.get_stop(st.stop_id).otp, "test")
    return oid


def test_wahi_order_dobara_repeats_the_last_delivered_order_on_a_card(repo):
    p = MunshiPlatform(repo)
    assert "no confirmed order" in p.handle_message("t0", "clerk", "Haji Sons ko wahi order dobara bhej do").text
    _deliver(repo, "C-009", [{"sku": "UREA-50", "qty": 12}, {"sku": "ZINC-10", "qty": 3}])
    r = p.handle_message("t", "clerk", "Haji Sons ko wahi order dobara bhej do", user="Bilal")
    assert r.pending and r.pending.args["customer_id"] == "C-009"
    assert r.pending.args["items"] == [{"sku": "UREA-50", "qty": 12}, {"sku": "ZINC-10", "qty": 3}]
    assert [ln["qty"] for ln in p.card(r.pending)["lines"]] == [12, 3]
    # "same as last time" with the customer from the conversation
    p.handle_message("t2", "clerk", "Haji Sons ka khata dikhao")
    r2 = p.handle_message("t2", "clerk", "pichla order repeat karo")
    assert r2.pending and r2.pending.args["customer_id"] == "C-009" and "from our conversation" in r2.text
    # a question about the last order is a read, never a card
    assert p.handle_message("t3", "clerk", "Haji Sons ka pichla order kya tha?").pending is None


def test_a_repeat_keeps_a_negotiated_price_and_says_so(repo):
    _deliver(repo, "C-009", [{"sku": "UREA-50", "qty": 10, "unit_price": 3700}])
    p = MunshiPlatform(repo)
    r = p.handle_message("t", "clerk", "same as last time Haji Sons ko", user="Bilal")
    assert r.pending and r.pending.args["items"] == [{"sku": "UREA-50", "qty": 10, "unit_price": 3700.0}]
    card = p.card(r.pending)
    assert card["lines"][0]["unit_price"] == 3700.0 and any("negotiated" in f["value"] for f in card["facts"])


def test_a_last_order_still_on_its_way_is_not_repeated(repo):
    oid = repo.create_order("C-009", [{"sku": "UREA-50", "qty": 5}], "chat", "t", "test").order_id
    repo.confirm_order(oid, "test", "test")
    r = MunshiPlatform(repo).handle_message("t", "clerk", "Haji Sons ko wahi order dobara bhej do")
    assert r.pending is None and oid in r.text and "double" in r.text


def test_a_newer_draft_blocks_a_repeat_too(repo):
    _deliver(repo, "C-009", [{"sku": "UREA-50", "qty": 12}])
    draft = repo.create_order("C-009", [{"sku": "DAP-50", "qty": 2}], "chat", "t", "test").order_id
    r = MunshiPlatform(repo).handle_message("t", "clerk", "Haji Sons ko wahi order dobara bhej do")
    assert r.pending is None and draft in r.text


def test_which_one_options_are_ranked_by_what_this_user_orders_most_never_picked(repo):
    with repo.acting_as("Bilal"):
        repo.create_order("C-012", [{"sku": "UREA-50", "qty": 1}], "chat", "t", "test")
    p = MunshiPlatform(repo)
    q = p.handle_message("t", "clerk", "Malik ko 5 urea bhej do", user="Bilal")
    assert q.pending is None and q.text.index("Malik Seeds") < q.text.index("Malik Agro Store")
    card = p.handle_message("t", "clerk", "pehla", user="Bilal")
    assert card.pending.args["customer_id"] == "C-012"
    other = p.handle_message("u", "clerk", "Malik ko 5 urea bhej do", user="Sana")        # no history: the usual order
    assert other.text.index("Malik Agro Store") < other.text.index("Malik Seeds")


# ====================================================================== storage, cache, isolation
def test_v6_applies_to_a_pre_v6_file_and_reopening_applies_nothing(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path, isolation_level=None)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:5])
    assert migrations.migrate(conn) == [1, 2, 3, 4, 5]
    conn.execute("INSERT INTO customers (customer_id, name, phone, tier, credit_limit, route_id, language) VALUES ('C-1','Bhatti Kisan Store','','standard',0,NULL,'ur')")
    conn.close()
    monkeypatch.undo()
    r = MunshiRepository(path)
    assert migrations.current_version(r._conn) == migrations.MIGRATIONS[-1][0] >= 6 and r.get_customer("C-1").name == "Bhatti Kisan Store"
    assert r.learned_aliases() == [] and r._one("SELECT COUNT(*) FROM alias_card_links")[0] == 0
    r.learn_alias(phrase_key("bhatti sahab"), "Bhatti sahab", "customer", "C-1", "test")
    with pytest.raises(sqlite3.IntegrityError):                   # one ACTIVE row per phrase, enforced by the file itself
        with r._tx() as c:
            c.execute("INSERT INTO learned_aliases (phrase_norm, phrase, entity_kind, entity_id, taught_at) VALUES (?,?,?,?,?)",
                      (phrase_key("bhatti sahab"), "x", "customer", "C-1", "now"))
    r.close()
    again = MunshiRepository(path)
    assert migrations.migrate(again._conn) == [] and len(again.learned_aliases()) == 1


def test_a_failing_v6_leaves_the_file_at_v5(tmp_path, monkeypatch):
    path = str(tmp_path / "bad.db")
    conn = sqlite3.connect(path, isolation_level=None)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:5] + [(6, migrations.V6 + ";\nCREATE TABLE learned_aliases (x)")])
    with pytest.raises(sqlite3.OperationalError):
        migrations.migrate(conn)
    assert migrations.current_version(conn) == 5
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('learned_aliases','alias_card_links')").fetchone()[0] == 0
    conn.close()


def test_the_cache_follows_every_write_and_a_restored_file(repo):
    assert repo.learned_aliases() == []
    snap = sqlite3.connect(":memory:")
    repo._conn.backup(snap)
    repo.learn_alias(phrase_key("haji"), "haji", "customer", "C-009", "test")
    assert _active(repo) == {"haji": "C-009"}
    repo.learn_alias(phrase_key("haji"), "haji", "customer", "C-005", "test")
    assert _active(repo) == {"haji": "C-005"}
    snap.backup(repo._conn)                                        # the file's contents swapped underneath (a restore)
    assert repo.learned_aliases() == []


def test_another_business_never_sees_what_this_one_learned(tmp_path):
    from munshi.tenancy.hub import TenantHub
    hub = TenantHub(str(tmp_path))
    a = hub.signup("A Traders", "Multan", "Ali", "0300-5550001", "1357", sample_data=True)["business"]["business_id"]
    b = hub.signup("B Traders", "Vehari", "Babar", "0300-5550002", "2468", sample_data=True)["business"]["business_id"]
    pa, pb = hub.platform(a), hub.platform(b)
    pa.handle_message("t", "owner", "doctor sahab matlab Haji Sons hai", user="Ali")
    assert customer_resolution("doctor sahab ko 5 urea", pa.repo).id == "C-009"
    assert customer_resolution("doctor sahab ko 5 urea", pb.repo).status == "none" and pb.repo.learned_aliases() == []
    hub.close()


def test_resolution_stays_fast_with_many_learned_names(repo):
    nick = [f"zq{chr(97 + i % 26)}{chr(97 + i // 26)}r sahab" for i in range(300)]
    for i, n in enumerate(nick):
        repo.learn_alias(phrase_key(n), n, "customer", "C-00" + str(1 + i % 9), "test")
    customer_resolution("Bhatti sahab ko 10 urea", repo)
    t0 = time.perf_counter()
    for _ in range(50):
        customer_resolution(f"{nick[250]} ko 10 urea aur 5 dap", repo)
    per = (time.perf_counter() - t0) / 50
    assert per < 0.03, f"{per * 1000:.1f} ms per resolution"
    assert customer_resolution(f"{nick[250]} ko 10 urea", repo).id == "C-00" + str(1 + 250 % 9)


# ====================================================================== the model guard agrees with the rules
AGREE = ["Bhatti sahab ko 10 urea bhej do", "bhatti sahab ko 10 urea bhej do", "Bhatti ko 10 urea bhej do", "Bhatti Traders ko 10 urea bhej do",
         "zamindar sahab ko 10 urea bhej do", "Malik ko 10 urea bhej do", "Malik Agro Store ko 10 urea bhej do", "Malik Seeds ko 10 urea bhej do",
         "Bhatti sahab aur Haji Sons ko 10 urea bhej do", "بھٹی صاحب کو 10 یوریا بھیج دو"]


def test_the_guard_accepts_a_learned_name_exactly_when_the_rules_do(repo):
    _bhatti_traders(repo)
    repo.learn_alias(phrase_key("Bhatti sahab"), "Bhatti sahab", "customer", "C-007", "test")
    repo.learn_alias(phrase_key("Malik"), "Malik", "customer", "C-012", "test")
    repo.learn_alias(phrase_key("zamindar sahab"), "zamindar sahab", "customer", "C-007", "test")
    repo.upsert_customer(Customer("C-014", "Zamindar Traders", "", "standard", 300_000, "R-VEHARI"))
    p = MunshiPlatform(repo)
    for n, text in enumerate(AGREE):
        items = analyse_order(text, repo).items or [{"sku": "UREA-50", "qty": 10}]
        rules = p.handle_message(f"r{n}", "clerk", text, user="Bilal")
        carded = rules.pending.args["customer_id"] if rules.pending else None
        for cid in ("C-001", "C-007", "C-009", "C-012", "C-013", "C-014"):
            q = guard.check_call("create_order", {"customer_id": cid, "items": items}, text, repo)
            assert (q is None) == (cid == carded), (text, cid, carded, q)


def test_a_model_card_from_a_learned_name_names_the_memory_and_teaches_nothing_new(repo):
    _bhatti_traders(repo)
    repo.learn_alias(phrase_key("Bhatti sahab"), "Bhatti sahab", "customer", "C-007", "test")
    text = "Bhatti sahab wala maal: 10 urea"
    items = analyse_order(text, repo).items
    fake = _fake(routes={text: "order"}, plan={text: [[("create_order", {"customer_id": "C-007", "items": items})]]}, final={text: "Ho gaya."})
    p = MunshiPlatform(repo, model=fake)
    p.manager = _NoRoute()
    r = p.handle_message("t", "clerk", text, user="Bilal")
    assert r.engine == "model" and r.pending and r.pending.args["customer_id"] == "C-007"
    assert "Bhatti sahab = Bhatti Kisan Store (remembered)" in r.text
    # the same message with the other Bhatti is refused by the guard, as the rules would never card it
    fake.plan[text + " "] = [[("create_order", {"customer_id": "C-013", "items": items})]]
    fake.routes[text + " "] = "order"
    q = p.handle_message("t2", "clerk", text + " ", user="Bilal")
    assert q.pending is None and "Which customer" in q.text
