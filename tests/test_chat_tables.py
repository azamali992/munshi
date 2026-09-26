"""Structured tables on list-shaped chat answers (llm/answers.make_table, carried by platform.Reply.tables).

Each table is built by the SAME formatter run as its sentence, from the same rows: its totals are the sentence's totals and
the underlying report's totals. Labels follow the question's language (English / Roman Urdu / Urdu script); rows are named
by names, never by internal codes. The plain sentence (and the folded details block) stays in `text` for clients that
ignore tables; the table rides along on the HTTP reply and in the chat row's meta, so a reloaded thread shows it again."""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from munshi.domain.seed import seeded_repository
from munshi.llm import answers
from munshi.platform import DETAILS, MunshiPlatform, visible
from munshi.web.app import build_app

CODES = re.compile(r"\b[CS]-\d{3,}\b|\bWH-[A-Z]+|\b(?:ORD|STP|DSP|REM|PRM|DEP|RCP|EXP)-[A-Z0-9]{4,}\b")
KINDS = {"text", "money", "qty", "date", "pct", "days"}


@pytest.fixture
def p():
    plat = MunshiPlatform(seeded_repository())
    yield plat
    plat.close()


def full(tool, data, repo, text, args=None):
    s, t = answers.render_full(tool, data, repo, text, args or {})
    assert s is not None
    return s, t


def check_shape(t: dict) -> None:
    """The schema every table keeps, and no internal code shown as a label or a cell."""
    assert set(t) >= {"title", "lead", "columns", "rows", "totals", "note", "count", "lang", "text"}
    json.dumps(t)
    keys = [c["key"] for c in t["columns"]]
    for c in t["columns"]:
        assert c["kind"] in KINDS and c["align"] == ("right" if c["kind"] in ("money", "qty", "days", "pct") else "left")
        assert not CODES.search(c["label"]), c["label"]
    for r in t["rows"] + ([t["totals"]] if t["totals"] else []):
        assert set(r) - {"_em"} <= set(keys)
        for v in r.values():
            assert not (isinstance(v, str) and CODES.search(v)), v
    assert t["count"] == len(t["rows"])
    assert t["lead"] and len(t["lead"]) < len(t["text"]) + 60


def col(t, key):
    return [r.get(key) for r in t["rows"]]


def total(rows, key):
    return round(sum(float(r.get(key) or 0) for r in rows), 2)


# ------------------------------------------------------------------ one list answer at a time, against the books
def test_who_owes_is_a_table_whose_total_is_the_sentence_and_the_aging_total(p):
    rows = p.ops.aging_report()
    s, t = full("aging_report", rows, p.repo, "who owes us")
    check_shape(t)
    assert [c["key"] for c in t["columns"]] == ["name", "balance", "days", "bucket", "limit", "last_paid"]
    assert col(t, "name") == [r["name"] for r in rows]
    assert t["totals"]["balance"] == total(rows, "balance") == p.repo.aging_summary()["total"]
    assert answers.rs(t["totals"]["balance"]) in s and answers.rs(t["totals"]["balance"]) in t["lead"]
    assert t["totals"]["name"] == "Total" and t["lead"].endswith(":") and t["text"] == s
    assert col(t, "days") == [r["days_overdue"] or None for r in rows]
    assert set(col(t, "bucket")) <= {"Not due", "1-30 days", "31-60 days", "60+ days"}


def test_labels_follow_the_question_language(p):
    rows = p.ops.aging_report()
    _, ru = full("aging_report", rows, p.repo, "kis costumer se kitne paise lene hein?")
    _, ur = full("aging_report", rows, p.repo, "وصولی کی لسٹ دکھائیں")
    assert ru["lang"] == "ru" and ru["totals"]["name"] == "Kul" and "Baqi" in [c["label"] for c in ru["columns"]]
    assert ur["lang"] == "ur" and ur["totals"]["name"] == "کل" and "باقی" in [c["label"] for c in ur["columns"]]
    assert ru["totals"]["balance"] == ur["totals"]["balance"]


def test_stock_has_a_column_per_godown_by_name_and_a_total(p):
    levels = p.ops.get_stock("")
    s, t = full("get_stock", levels, p.repo, "stocks kitne baqi hein?")
    check_shape(t)
    labels = [c["label"] for c in t["columns"]]
    names = {w.name for w in p.repo.list_warehouses()}
    assert names <= set(labels)                                   # godowns by name, never WH-...
    assert col(t, "name") == sorted(col(t, "name"))
    sku_of = {pr.name: pr.sku for pr in p.repo.list_products()}
    for r in t["rows"]:
        want = sum(x["available"] for x in levels if x["sku"] == sku_of[r["name"]])
        assert r["available"] == want
        assert sum(v or 0 for k, v in r.items() if k.startswith("g")) == want
        assert r["name"] in s                                      # the sentence names the same products


def test_low_stock_rows_carry_the_flag(p):
    levels = p.ops.get_stock("")
    s, t = full("get_stock", levels, p.repo, "Vehari godown mein kya kam hai")
    check_shape(t)
    assert all(r["low"].startswith("Kam") for r in t["rows"]) and any(c.get("badge") for c in t["columns"])
    assert "Vehari Godown" in t["lead"] and all(r["name"] in s for r in t["rows"])


def test_todays_payments_total_is_the_sentence_and_leaves_out_a_reversal(p):
    custs = [c.customer_id for c in p.repo.list_customers()][:3]
    p.ops.record_payment(custs[0], 20000, "cash")
    p.ops.record_payment(custs[1], 15000, "bank")
    bounced = p.ops.record_payment(custs[2], 5000, "cheque")
    p.ops.reverse_ledger_entry(bounced["entry_id"], "cheque bounced")
    data = p.ops.collection_report(answers._bdate(bounced["created_at"]), answers._bdate(bounced["created_at"]))
    s, t = full("collection_report", data, p.repo, "aaj kis kis ne payment ki?")
    check_shape(t)
    assert t["totals"]["amount"] == 35000 and "Rs 35,000" in s and "Rs 35,000" in t["lead"]
    assert len(t["rows"]) == 2 and set(col(t, "method")) == {"Cash", "Bank"}
    assert all(re.fullmatch(r"\d\d:\d\d", x) for x in col(t, "time"))
    assert t["note"] and "reverse" in t["note"]


def test_cashbook_lines_run_to_the_books_net(p):
    cid = p.repo.list_customers()[0].customer_id
    p.ops.record_payment(cid, 12000, "cash")
    p.repo.record_expense("fuel", 3000, "loader fuel", "cash", "owner", "owner")
    d = p.ops.cashbook("")
    s, t = full("cashbook", d, p.repo, "aaj ka cashbook")
    check_shape(t)
    assert t["totals"]["in"] == round(d["total_in"] + d["total_handins"], 2)
    assert t["totals"]["out"] == d["total_out"] and t["totals"]["running"] == d["net"]
    assert t["rows"][-1]["running"] == d["net"]
    assert total(t["rows"], "in") - total(t["rows"], "out") == pytest.approx(d["net"])
    assert answers.rs(d["net"]) in s and t["lead"] == visible(s).split(" Cash payments:")[0]


def test_profit_is_a_statement_ending_on_the_net(p):
    d = p.ops.profit_summary()
    s, t = full("profit_summary", d, p.repo, "is mahine ka munafa")
    check_shape(t)
    assert [c["kind"] for c in t["columns"]] == ["text", "money"]
    amounts = col(t, "amount")
    assert amounts[0] == d["revenue"] and amounts[1] == d["cost_of_goods"] and amounts[2] == d["gross_margin"]
    cats = [r["amount"] for r in t["rows"][3:-1]]
    assert round(sum(cats), 2) == d["expenses"] == t["rows"][-1]["amount"]
    assert t["totals"]["amount"] == d["net"] and t["totals"]["line"] == "Net munafa"
    assert answers.rs(d["net"]) in s and answers.rs(d["net"]) in t["lead"]


def test_profit_caveat_is_the_table_note(p):
    d = {"start": "2026-09-01", "end": "2026-09-26", "revenue": 1085000, "cost_of_goods": 0, "gross_margin": 1085000, "expenses": 16100,
         "expenses_by_category": {"fuel": 16100}, "net": 1068900, "margin_pct": 100.0, "margin_reliable": False, "costed_margin_pct": 7.4,
         "cost_missing": {"count": 3, "revenue": 1085000.0}, "caveat": "Rs 1,085,000 of sales (3 invoices) have no cost on record, so the margin is overstated"}
    _, t = full("profit_summary", d, p.repo, "profit this month")
    assert "no cost on record" in t["note"] and "7.4%" in t["note"] and not t["note"].startswith("Note")


def test_sales_by_product_and_by_customer(p):
    d = p.ops.sales_report()
    s, t = full("sales_report", d, p.repo, "sales by product this month")
    check_shape(t)
    assert t["totals"]["sales"] == total(d["by_product"], "revenue") and t["totals"]["cost"] == total(d["by_product"], "cost")
    assert t["totals"]["margin"] == pytest.approx(t["totals"]["sales"] - t["totals"]["cost"])
    _, tc = full("sales_report", d, p.repo, "sales this month")
    check_shape(tc)
    assert len(d["by_customer"]) < 20 and tc["totals"]["sales"] == d["revenue"]         # every customer: the rows add up to the revenue


def test_other_lists_have_tables_with_matching_totals(p):
    top = p.ops.top_customers()
    s, t = full("top_customers", top, p.repo, "top customers")
    check_shape(t)
    assert t["totals"]["sales"] == total(top, "revenue")
    slow = p.ops.slow_stock()
    s, t = full("slow_stock", slow, p.repo, "slow stock 30 days")
    check_shape(t)
    assert t["totals"]["value"] == total(slow, "value_at_cost") and col(t, "name") == [r["name"] for r in slow]
    sups = p.ops.list_suppliers()
    if len(sups) >= 2:
        _, t = full("list_suppliers", sups, p.repo, "suppliers")
        check_shape(t)
        assert t["totals"]["balance"] == total(sups, "balance")


def test_payables_and_orders_and_stops(p):
    pay = [{"supplier_id": "S-901", "name": "Fauji Fertilizer", "balance": 540000.0}, {"supplier_id": "S-902", "name": "Engro", "balance": 60000.0}]
    s, t = full("payables_report", pay, p.repo, "what do we owe suppliers")
    check_shape(t)
    assert t["totals"]["balance"] == 600000 and "Rs 600,000" in s
    custs = p.repo.list_customers()
    for cst in custs[:3]:
        p.repo.create_order(cst.customer_id, [{"sku": p.repo.list_products()[0].sku, "qty": 2}], "chat", "", "order_munshi")
    orders = p.ops.list_orders()
    s, t = full("list_orders", orders, p.repo, "orders dikhao")
    check_shape(t)
    assert t["totals"]["total"] == total(orders, "total") and set(col(t, "status")) <= {"Draft", "Confirmed", "Reserved", "On the way", "Delivered",
                                                                                       "Short", "Cancel", "Cancelled"}
    stops = [{"sequence": k + 1, "customer_id": c.customer_id, "customer_name": c.name, "status": "pending", "order_total": 1000.0 * (k + 1),
              "items": [{"sku": p.repo.list_products()[0].sku, "qty": 2}], "cash_collected": 0} for k, c in enumerate(custs[:3])]
    s, t = full("list_stops", stops, p.repo, "stops for today")
    check_shape(t)
    assert t["totals"]["bill"] == 6000 and col(t, "stop") == [1, 2, 3]


def test_one_row_or_none_is_a_sentence_not_a_table(p):
    rows = p.ops.aging_report()[:1]
    s, t = full("aging_report", rows, p.repo, "who owes us")
    assert t is None and rows[0]["name"] in s
    s, t = full("aging_report", [], p.repo, "who owes us")
    assert t is None
    s, t = full("get_customer_khata", {"customer": {"name": "Haji Sons"}, "outstanding": 100}, p.repo, "haji sons ka khata")
    assert t is None


# ------------------------------------------------------------------ the reply, the chat log, HTTP
def test_the_reply_carries_the_table_and_the_full_sentence_stays(p):
    r = p.handle_message("t1", "owner", "kis costumer se kitne paise lene hein?", "Sultan")
    assert r.tool == "aging_report" and len(r.tables) == 1 and r.table is r.tables[0]
    t = r.table
    assert t["text"] in r.text and DETAILS in r.text                        # the complete sentence and the details block stay
    last = [m for m in p.repo.chat_history("t1") if m["role"] == "munshi"][-1]
    assert last["meta"]["tables"] == r.tables                              # reloading the thread shows it again


def test_split_reads_keep_each_table(p):
    r = p.handle_message("t2", "owner", "who owes us aur stock kitna hai", "Sultan")
    if len(visible(r.text).split("\n")) < 2:
        pytest.skip("not split into two reads")
    assert len(r.tables) >= 1 and all(t["text"] in r.text for t in r.tables)


def test_grounding_carries_the_table_on_the_model_path(p):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from munshi.llm import grounding as GR
    rows = p.ops.aging_report()
    msgs = [HumanMessage("who owes us"), AIMessage("", tool_calls=[{"id": "1", "name": "aging_report", "args": {}}]),
            ToolMessage(json.dumps(rows), tool_call_id="1", name="aging_report")]
    said, raw, tables = GR.render_turn(GR.turn_results(msgs, 0), p.repo, "who owes us", lambda t: False)
    assert len(tables) == 1 and tables[0]["text"] == said[0] and tables[0]["totals"]["balance"] == total(rows, "balance")
    assert GR.render_results(GR.turn_results(msgs, 0), p.repo, "who owes us", lambda t: False) == (said, raw)


def test_learned_names_and_pending_approvals_are_tables(p):
    custs = p.repo.list_customers()
    for c, phrase in zip(custs[:2], ("bhatti sahab", "haji ji"), strict=True):
        p.repo.learn_alias(phrase, phrase, "customer", c.customer_id, "chat", taught_by="owner", confirmed_by="owner")
    r = p.handle_message("t3", "owner", "kya kya yaad hai", "Sultan")
    assert r.table and len(r.table["rows"]) >= 2 and r.table["columns"][0]["label"] == "Bola gaya naam"
    check_shape(r.table)
    p.handle_message("t4", "clerk", "Chaudhry Farms ko 20 urea bhej do", "Bilal")
    p.handle_message("t5", "clerk", "Haji Sons ko 10 dap bhej do", "Bilal")
    r = p.handle_message("t6", "owner", "koi approval pending he?", "Sultan")
    if len(p.pending_items()) >= 2:
        assert r.table and len(r.table["rows"]) == len(p.pending_items()) and r.table["text"] == r.text
        check_shape(r.table)


def H(c, role="owner"):
    phones = {"owner": ("0300-0000001", "1111")}
    r = c.post("/api/session", json=dict(zip(("phone", "pin"), phones[role], strict=True)))
    return {"X-Session": r.json()["token"]}


def test_http_chat_returns_the_table_and_the_thread_keeps_it():
    c = TestClient(build_app(in_memory=True, demo=True, scheduler=False))
    h = H(c)
    r = c.post("/api/chat", json={"thread_id": "tbl", "text": "kis costumer se kitne paise lene hein?"}, headers=h).json()
    t = r["table"]
    assert t and r["tables"] == [t] and t["text"] in r["text"]
    assert t["totals"]["balance"] == round(sum(x["balance"] for x in t["rows"]), 2)
    assert answers.rs(t["totals"]["balance"]) in r["text"]
    hist = c.get("/api/chat/tbl", headers=h).json()
    assert hist[-1]["role"] == "munshi" and hist[-1]["meta"]["tables"] == [t]
    plain = c.post("/api/chat", json={"thread_id": "tbl", "text": "hello"}, headers=h).json()
    assert plain["table"] is None and plain["tables"] == []
