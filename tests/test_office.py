"""The desktop office console (/office) and its API (web/routes/office.py, domain/repository/office.py):
permissions per role, bulk price preview vs save, price history on every path, physical stock counts through the
stock ledger (never below zero, all or nothing), the stock matrix against the ledger replay, and the client list
against outstanding()/aging()."""
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from munshi.domain import migrations
from munshi.domain.models import business_today, to_paisa
from munshi.domain.repository import MunshiRepository
from munshi.web.app import build_app

DEMO = {"owner": ("0300-0000001", "1111"), "clerk": ("0300-0000002", "2222"), "driver": ("0300-0000003", "3333"), "salesman": ("0300-0000004", "4444")}


@pytest.fixture
def app():
    return build_app(in_memory=True, demo=True, scheduler=False)


@pytest.fixture
def c(app):
    return TestClient(app)


@pytest.fixture
def H(c):
    tokens = {}
    for role, (phone, pin) in DEMO.items():
        r = c.post("/api/session", json={"phone": phone, "pin": pin}); assert r.status_code == 200, r.text
        tokens[role] = {"X-Session": r.json()["token"]}
    return tokens


@pytest.fixture
def repo(app, c) -> MunshiRepository:
    return app.state.hub.platform("demo").repo


def _price(repo, sku) -> int:
    return int(repo._one("SELECT unit_price FROM products WHERE sku=?", (sku,))["unit_price"])


PRICE = {"skus": ["UREA-50", "DAP-50"], "mode": "pct", "value": 5}
COUNT = {"warehouse_id": "WH-MULTAN", "lines": [{"sku": "UREA-50", "counted": 410}]}
READS = ["/api/office/products", "/api/office/products/UREA-50/price-history", "/api/office/stock", "/api/office/stock/moves?sku=UREA-50", "/api/office/clients"]


# ====================================================================== the page + permissions
def test_the_page_is_served_and_holds_no_data(c):
    r = c.get("/office")
    assert r.status_code == 200 and "/static/office/app.js" in r.text and "text/html" in r.headers["content-type"]
    assert c.get("/office/").status_code == 200
    for f in ("app.js", "lib.js", "products.js", "inventory.js", "clients.js", "suppliers.js", "import.js", "office.css"):
        assert c.get(f"/static/office/{f}").status_code == 200, f
    # the page carries the app's CSP: scripts from this origin only (the console uses no inline script)
    assert "script-src 'self'" in r.headers["content-security-policy"]


def test_every_office_route_needs_a_session(c):
    for path in READS:
        assert c.get(path).status_code == 401, path
    assert c.post("/api/office/prices/preview", json=PRICE).status_code == 401
    assert c.post("/api/office/stock-count/preview", json=COUNT).status_code == 401


@pytest.mark.parametrize("role", ["salesman", "driver"])
def test_field_roles_get_403_everywhere(c, H, repo, role):
    before = _price(repo, "UREA-50"), repo.get_stock("WH-MULTAN", "UREA-50").on_hand
    for path in READS:
        assert c.get(path, headers=H[role]).status_code == 403, path
    assert c.post("/api/office/prices/preview", json=PRICE, headers=H[role]).status_code == 403
    assert c.post("/api/office/prices/apply", json=PRICE | {"expected": {"UREA-50": 3850, "DAP-50": 6250}}, headers=H[role]).status_code == 403
    assert c.post("/api/office/stock-count/preview", json=COUNT, headers=H[role]).status_code == 403
    assert c.post("/api/office/stock-count/post", json={**COUNT, "lines": [{"sku": "UREA-50", "counted": 410, "system": 420}]}, headers=H[role]).status_code == 403
    assert (_price(repo, "UREA-50"), repo.get_stock("WH-MULTAN", "UREA-50").on_hand) == before


def test_clerk_reads_and_prepares_but_prices_and_posting_are_the_owners(c, H, repo):
    K, O = H["clerk"], H["owner"]
    for path in READS:
        assert c.get(path, headers=K).status_code == 200, path
    # price changes: owner only, like the existing product form (PATCH /api/products is setup:write)
    assert c.post("/api/office/prices/preview", json=PRICE, headers=K).status_code == 403
    assert c.post("/api/office/prices/apply", json=PRICE | {"expected": {"UREA-50": 3850, "DAP-50": 6250}}, headers=K).status_code == 403
    assert c.patch("/api/products/UREA-50", json={"name": "Urea 50kg", "unit_price": 1}, headers=K).status_code == 403    # unchanged existing rule
    # counts: a clerk may preview; posting is a batch of adjustments, and /api/stock/adjust is owner-only
    assert c.post("/api/office/stock-count/preview", json=COUNT, headers=K).status_code == 200
    post = {**COUNT, "lines": [{"sku": "UREA-50", "counted": 410, "system": 420}]}
    assert c.post("/api/office/stock-count/post", json=post, headers=K).status_code == 403
    assert c.post("/api/stock/adjust", json={"warehouse_id": "WH-MULTAN", "sku": "UREA-50", "delta": -1, "reason": "damaged"}, headers=K).status_code == 403
    assert repo.get_stock("WH-MULTAN", "UREA-50").on_hand == 420 and _price(repo, "UREA-50") == 385000
    assert c.post("/api/office/stock-count/post", json=post, headers=O).status_code == 201


def test_cost_and_value_are_the_owners_only(c, H):
    K, O = H["clerk"], H["owner"]
    kp, op = c.get("/api/office/products", headers=K).json(), c.get("/api/office/products", headers=O).json()
    assert all("cost_price" not in p and "avg_cost" not in p and "stock_value" not in p for p in kp)
    assert all("cost_price" in p and "avg_cost" in p for p in op)
    ks, os_ = c.get("/api/office/stock", headers=K).json(), c.get("/api/office/stock", headers=O).json()
    assert "value" not in ks and all("value" not in cell for r in ks["rows"] for cell in r["cells"].values()) and "value" not in ks["rows"][0]
    assert os_["value"] > 0
    assert all("value" not in m for m in c.get("/api/office/stock/moves?sku=UREA-50", headers=K).json()["moves"])
    assert all("value" in m for m in c.get("/api/office/stock/moves?sku=UREA-50", headers=O).json()["moves"])
    # a cost change (a purchase at a new cost) is in the owner's price history only
    assert c.post("/api/purchases", json={"supplier_id": "S-001", "warehouse_id": "WH-MULTAN", "items": [{"sku": "UREA-50", "qty": 10, "unit_cost": 3700}]}, headers=O).status_code == 201
    assert [h["field"] for h in c.get("/api/office/products/UREA-50/price-history", headers=O).json()] == ["cost_price"]
    assert c.get("/api/office/products/UREA-50/price-history", headers=K).json() == []
    pv = c.post("/api/office/stock-count/preview", json=COUNT, headers=K).json()
    assert "value_change" not in pv and "value_change" not in pv["rows"][0]


# ====================================================================== prices
def test_bulk_preview_saves_nothing_then_apply_saves_exactly_the_preview(c, H, repo):
    O = H["owner"]
    audit_before = len(repo.audit_log(1000))
    pv = c.post("/api/office/prices/preview", json=PRICE, headers=O).json()
    assert [(r["sku"], r["old"], r["new"]) for r in pv["rows"]] == [("UREA-50", 3850.0, 4043.0), ("DAP-50", 6250.0, 6563.0)]   # +5%, to the rupee
    assert pv["changes"] == 2 and pv["errors"] == 0
    assert _price(repo, "UREA-50") == 385000 and _price(repo, "DAP-50") == 625000                     # a preview changes nothing
    assert repo._one("SELECT COUNT(*) n FROM price_history")["n"] == 0 and len(repo.audit_log(1000)) == audit_before
    r = c.post("/api/office/prices/apply", json=PRICE | {"expected": {"UREA-50": 3850, "DAP-50": 6250}, "reason": "FFC rate card"}, headers=O)
    assert r.status_code == 200, r.text
    assert [(x["sku"], x["new"]) for x in r.json()["rows"]] == [(x["sku"], x["new"]) for x in pv["rows"]] and r.json()["saved"] == 2
    assert _price(repo, "UREA-50") == 404300 and _price(repo, "DAP-50") == 656300
    # price history: old, new, when, who -- the real user
    h = c.get("/api/office/products/UREA-50/price-history", headers=O).json()
    assert len(h) == 1 and h[0] | {"at": None, "history_id": None} == {"history_id": None, "field": "unit_price", "old": 3850.0, "new": 4043.0, "change": 193.0,
                                                                      "at": None, "by": "Sultan Ahmed (owner)", "source": "office: bulk"}
    # audited with the signed-in user and the named approver
    a = [x for x in repo.audit_log(50) if x["action"] == "price_changed" and x["entity_id"] == "UREA-50"][0]
    assert a["user"] == "Sultan Ahmed" and a["approved_by"] == "owner:Sultan Ahmed" and a["payload"]["old"] == 3850 and a["payload"]["new"] == 4043
    assert a["payload"]["reason"] == "FFC rate card"
    # a new order is priced at the new list price
    o = repo.create_order("C-002", [{"sku": "UREA-50", "qty": 1}], "app", "", "clerk")
    assert o.items[0].unit_price == 4043.0


def test_inline_single_price_set_and_modes(c, H, repo):
    O = H["owner"]
    assert c.post("/api/office/prices/apply", json={"skus": ["NPK-25"], "mode": "set", "value": 4150.5, "expected": {"NPK-25": 4100}}, headers=O).status_code == 200
    assert _price(repo, "NPK-25") == 415050
    h = c.get("/api/office/products/NPK-25/price-history", headers=O).json()[0]
    assert h["source"] == "office" and h["by"] == "Sultan Ahmed (owner)"
    pv = c.post("/api/office/prices/preview", json={"skus": ["NPK-25", "SOP-50"], "mode": "add", "value": -150}, headers=O).json()["rows"]
    assert [(r["old"], r["new"]) for r in pv] == [(4150.5, 4000.5), (5900.0, 5750.0)]
    assert repo.new_price_paisa(385000, "pct", 2.5, 10) == 395000         # 3946.25 -> nearest Rs 10
    assert repo.new_price_paisa(385000, "pct", 2.5, 0) == 394625          # to the paisa
    assert repo.new_price_paisa(385000, "pct", -10, 1) == 346500


def test_apply_is_all_or_nothing_and_refuses_a_stale_preview(c, H, repo):
    O = H["owner"]
    # someone changed Urea's price after the preview was taken: nothing is saved, not even DAP
    r = c.post("/api/office/prices/apply", json=PRICE | {"expected": {"UREA-50": 3800, "DAP-50": 6250}}, headers=O)
    assert r.status_code == 409 and "preview again" in r.json()["detail"]
    assert _price(repo, "UREA-50") == 385000 and _price(repo, "DAP-50") == 625000
    assert repo._one("SELECT COUNT(*) n FROM price_history")["n"] == 0
    assert c.post("/api/office/prices/apply", json=PRICE | {"expected": {"UREA-50": 3850}}, headers=O).status_code == 400    # an old price is missing
    # a price that would reach zero or below is refused, for the whole batch
    bad = {"skus": ["IMIDA-250", "UREA-50"], "mode": "add", "value": -1000}
    pv = c.post("/api/office/prices/preview", json=bad, headers=O).json()
    assert pv["errors"] == 1 and "above zero" in pv["rows"][0]["error"]
    assert c.post("/api/office/prices/apply", json=bad | {"expected": {"IMIDA-250": 950, "UREA-50": 3850}}, headers=O).status_code == 400
    assert _price(repo, "UREA-50") == 385000 and _price(repo, "IMIDA-250") == 95000
    assert c.post("/api/office/prices/preview", json={"skus": ["UREA-50"], "mode": "pct", "value": -95}, headers=O).status_code == 422
    assert c.post("/api/office/prices/preview", json={"skus": ["NOPE"], "mode": "pct", "value": 5}, headers=O).status_code == 404


def test_price_history_records_every_path_with_who(c, H, repo):
    O = H["owner"]
    # the phone app's product form (existing PATCH route): attributed from the audit row written with it
    body = {"name": "DAP 50kg", "unit_price": 6300, "cost_price": 5900, "unit": "bag", "category": "fertilizer", "aliases": ["dap"], "min_stock": 10}
    assert c.patch("/api/products/DAP-50", json=body, headers=O).status_code == 200
    h = c.get("/api/office/products/DAP-50/price-history", headers=O).json()
    assert [(x["field"], x["old"], x["new"], x["by"], x["source"]) for x in h] == [("unit_price", 6250.0, 6300.0, "Sultan Ahmed (owner)", "product form")]
    # a purchase at a new cost moves the reference cost: recorded, attributed to the purchase
    p = c.post("/api/purchases", json={"supplier_id": "S-002", "warehouse_id": "WH-VEHARI", "items": [{"sku": "DAP-50", "qty": 5, "unit_cost": 6000}]}, headers=H["clerk"]).json()
    h = c.get("/api/office/products/DAP-50/price-history", headers=O).json()
    assert (h[0]["field"], h[0]["old"], h[0]["new"], h[0]["by"], h[0]["source"]) == ("cost_price", 5900.0, 6000.0, "Bilal Hussain (clerk)", f"purchase {p['purchase_id']}")
    # saving the form without a price change records nothing
    body["unit_price"] = 6300; body["cost_price"] = 6000
    c.patch("/api/products/DAP-50", json=body, headers=O)
    assert len(c.get("/api/office/products/DAP-50/price-history", headers=O).json()) == 2
    # the office list shows when the list price last changed
    assert next(x for x in c.get("/api/office/products", headers=O).json() if x["sku"] == "DAP-50")["price_changed_at"]
    # an Excel import that re-prices a product: recorded, attributed to the import (written in the same second as
    # everything above -- the audit sequence, not the clock, decides who)
    import io

    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "Products"
    ws.append(["sku", "name", "unit", "category", "unit_price", "cost_price", "aliases", "units_per_load", "min_stock"])
    ws.append(["DAP-50", "DAP 50kg", "bag", "fertilizer", 6400, 6000, "dap", 1, 10])
    buf = io.BytesIO(); wb.save(buf)
    assert c.post("/api/import", files={"file": ("p.xlsx", buf.getvalue())}, headers=O).json()["products"] == 1
    h = c.get("/api/office/products/DAP-50/price-history", headers=O).json()
    assert (h[0]["field"], h[0]["old"], h[0]["new"], h[0]["by"], h[0]["source"]) == ("unit_price", 6300.0, 6400.0, "Sultan Ahmed (owner)", "Excel import")
    assert [x["source"] for x in h] == ["Excel import", f"purchase {p['purchase_id']}", "product form"]


def test_price_history_is_append_only(repo):
    repo.apply_price_change(["UREA-50"], "set", 3900, {"UREA-50": 3850}, 1, "", "owner", "owner:T", who="T (owner)")
    hid = repo._one("SELECT history_id FROM price_history")["history_id"]
    for sql in ("UPDATE price_history SET new_paisa = 1", "DELETE FROM price_history", "UPDATE price_history SET changed_by = 'someone else'"):
        with pytest.raises(sqlite3.IntegrityError):
            with repo._tx() as cur: cur.execute(sql)
    # an unattributed row (a change made outside the console) may be stamped with who, once
    with repo._tx() as cur: cur.execute("UPDATE products SET unit_price = 400000 WHERE sku='UREA-50'")
    new = repo._one("SELECT history_id, changed_by FROM price_history WHERE history_id > ?", (hid,))
    assert new["changed_by"] == ""
    assert repo._stamp_price_history(hid, "X (owner)", "test") == 1
    assert repo._stamp_price_history(hid, "Y (owner)", "test") == 0


def test_v7_applies_to_a_v6_file_and_captures_changes_from_then_on(tmp_path, monkeypatch):
    path = str(tmp_path / "v6.db")
    conn = sqlite3.connect(path, isolation_level=None)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:6])
    assert migrations.migrate(conn) == [1, 2, 3, 4, 5, 6]
    conn.execute("INSERT INTO products (sku, name, unit_price, aliases, units_per_load, cost_price) VALUES ('X-1','X',10000,'[]',1,9000)")
    conn.close(); monkeypatch.undo()
    r = MunshiRepository(path)
    assert migrations.current_version(r._conn) == 7 and r._one("SELECT COUNT(*) n FROM price_history")["n"] == 0   # nothing invented for the past
    p = r.get_product("X-1"); p.unit_price = 120; r.upsert_product(p)
    assert [(x["old"], x["new"]) for x in r.price_history("X-1", include_cost=True)] == [(100.0, 120.0)]
    r.close()
    assert migrations.migrate(MunshiRepository(path)._conn) == []


# ====================================================================== physical count
def _replay_ok(repo):
    replay = repo.replay_stock_ledger()
    return all(replay.get((s.warehouse_id, s.sku), 0) == s.on_hand for s in repo.list_stock())


def test_physical_count_posts_the_differences_as_ledger_adjustments(c, H, repo):
    O = H["owner"]
    pool_before = repo._pool("UREA-50")[1]
    lines = [{"sku": "UREA-50", "counted": 410}, {"sku": "DAP-50", "counted": 185}, {"sku": "NPK-25", "counted": 95}]
    pv = c.post("/api/office/stock-count/preview", json={"warehouse_id": "WH-MULTAN", "lines": lines}, headers=O).json()
    assert [(r["sku"], r["system"], r["diff"]) for r in pv["rows"]] == [("UREA-50", 420, -10), ("DAP-50", 180, 5), ("NPK-25", 95, 0)]
    assert (pv["differences"], pv["units_up"], pv["units_down"], pv["errors"]) == (2, 5, 10, 0)
    assert pv["value_change"] == -10 * 3600 + 5 * 5900
    assert repo.get_stock("WH-MULTAN", "UREA-50").on_hand == 420                              # preview posts nothing
    moves_before = repo._one("SELECT COUNT(*) n FROM stock_moves")["n"]
    day = business_today().isoformat()
    r = c.post("/api/office/stock-count/post", json={"warehouse_id": "WH-MULTAN", "count_date": day, "lines": [dict(ln, system=row["system"]) for ln, row in zip(lines, pv["rows"], strict=True)]}, headers=O)
    assert r.status_code == 201, r.text
    assert r.json()["reason"] == f"stock count {day}" and [(p["sku"], p["from"], p["to"]) for p in r.json()["posted"]] == [("UREA-50", 420, 410), ("DAP-50", 180, 185)]
    new = repo._all("SELECT sku, delta, kind, ref, value_paisa FROM stock_moves ORDER BY rowid DESC LIMIT 2")
    assert repo._one("SELECT COUNT(*) n FROM stock_moves")["n"] == moves_before + 2       # the unchanged line posts nothing
    assert {(m["sku"], m["delta"], m["kind"], m["ref"]) for m in new} == {("UREA-50", -10, "adjust", f"stock count {day}"), ("DAP-50", 5, "adjust", f"stock count {day}")}
    assert repo.get_stock("WH-MULTAN", "UREA-50").on_hand == 410 and repo.get_stock("WH-MULTAN", "DAP-50").on_hand == 185
    assert repo._pool("UREA-50")[1] == pool_before - 10 * 360000                            # issued at the moving average
    assert _replay_ok(repo)
    # audited with the real user: one adjustment per line plus the count itself
    acts = [a for a in repo.audit_log(20) if a["action"] in ("adjust_stock", "stock_count_posted")]
    assert {a["action"] for a in acts} == {"adjust_stock", "stock_count_posted"} and all(a["user"] == "Sultan Ahmed" and a["approved_by"] == "owner:Sultan Ahmed" for a in acts)


def test_a_count_never_takes_stock_negative_and_is_all_or_nothing(c, H, repo):
    O = H["owner"]
    # a negative count is refused at the door
    assert c.post("/api/office/stock-count/preview", json={"warehouse_id": "WH-MULTAN", "lines": [{"sku": "UREA-50", "counted": -1}]}, headers=O).status_code == 422
    # counting 0 of something on hand is allowed (down to exactly zero, never below)
    r = c.post("/api/office/stock-count/post", json={"warehouse_id": "WH-MULTAN", "lines": [{"sku": "CYPER-1L", "counted": 0, "system": 8}]}, headers=O)
    assert r.status_code == 201 and repo.get_stock("WH-MULTAN", "CYPER-1L").on_hand == 0
    # counting below what is reserved for allocated orders is refused -- and one bad line stops the whole count
    o = repo.create_order("C-002", [{"sku": "SOP-50", "qty": 20}], "app", "", "clerk")
    repo.confirm_order(o.order_id, "owner", "owner:T"); repo.allocate_order(o.order_id, "WH-MULTAN", "owner", "owner:T")
    assert repo.get_stock("WH-MULTAN", "SOP-50").reserved == 20
    lines = [{"sku": "UREA-50", "counted": 400, "system": 420}, {"sku": "SOP-50", "counted": 10, "system": 60}]
    pv = c.post("/api/office/stock-count/preview", json={"warehouse_id": "WH-MULTAN", "lines": lines}, headers=O).json()
    assert pv["errors"] == 1 and "reserved" in pv["rows"][1]["error"]
    r = c.post("/api/office/stock-count/post", json={"warehouse_id": "WH-MULTAN", "lines": lines}, headers=O)
    assert r.status_code == 400 and "reserved" in r.json()["detail"]
    assert repo.get_stock("WH-MULTAN", "UREA-50").on_hand == 420 and repo.get_stock("WH-MULTAN", "SOP-50").on_hand == 60
    # stock moved since the preview: nothing posted, preview again
    stale = [{"sku": "UREA-50", "counted": 400, "system": 419}]
    r = c.post("/api/office/stock-count/post", json={"warehouse_id": "WH-MULTAN", "lines": stale}, headers=O)
    assert r.status_code == 409 and "preview" in r.json()["detail"] and repo.get_stock("WH-MULTAN", "UREA-50").on_hand == 420
    # no system quantity at all is treated the same way (the post must be of a preview)
    assert c.post("/api/office/stock-count/post", json={"warehouse_id": "WH-MULTAN", "lines": [{"sku": "UREA-50", "counted": 400}]}, headers=O).status_code == 409
    # a future count date, a product counted twice, an unknown godown or product
    tomorrow = (business_today() + timedelta(days=1)).isoformat()
    assert c.post("/api/office/stock-count/post", json={"warehouse_id": "WH-MULTAN", "count_date": tomorrow, "lines": [{"sku": "UREA-50", "counted": 400, "system": 420}]}, headers=O).status_code == 400
    assert c.post("/api/office/stock-count/preview", json={"warehouse_id": "WH-MULTAN", "lines": [{"sku": "UREA-50", "counted": 1}, {"sku": "urea-50", "counted": 2}]}, headers=O).status_code == 400
    assert c.post("/api/office/stock-count/preview", json={"warehouse_id": "WH-NOPE", "lines": [{"sku": "UREA-50", "counted": 1}]}, headers=O).status_code == 404
    assert c.post("/api/office/stock-count/preview", json={"warehouse_id": "WH-MULTAN", "lines": [{"sku": "NOPE", "counted": 1}]}, headers=O).status_code == 404
    assert repo.get_stock("WH-MULTAN", "UREA-50").on_hand == 420 and _replay_ok(repo)


def test_post_stock_count_in_the_repository_cannot_go_below_zero(repo):
    with pytest.raises(ValueError):
        repo.post_stock_count("WH-MULTAN", [{"sku": "UREA-50", "counted": -5, "system": 420}], None, "owner", "owner:T")
    assert repo.get_stock("WH-MULTAN", "UREA-50").on_hand == 420


# ====================================================================== stock matrix + movements
def test_stock_matrix_and_valuation_agree_with_the_ledger(c, H, repo):
    O = H["owner"]
    c.post("/api/stock/transfer", json={"from_warehouse": "WH-MULTAN", "to_warehouse": "WH-VEHARI", "sku": "UREA-50", "qty": 20}, headers=O)
    c.post("/api/stock/adjust", json={"warehouse_id": "WH-VEHARI", "sku": "ZINC-10", "delta": -3, "reason": "damaged"}, headers=O)
    c.post("/api/purchases", json={"supplier_id": "S-001", "warehouse_id": "WH-MULTAN", "items": [{"sku": "DAP-50", "qty": 50, "unit_cost": 6000}]}, headers=O)
    s = c.get("/api/office/stock", headers=O).json()
    assert s["ledger_ok"] and s["ledger_mismatches"] == []
    replay = repo.replay_stock_ledger()
    for row in s["rows"]:
        for wh, cell in row["cells"].items():
            assert cell["on_hand"] == replay.get((wh, row["sku"]), 0) == repo.get_stock(wh, row["sku"]).on_hand
        assert row["on_hand"] == sum(cell["on_hand"] for cell in row["cells"].values())
    assert s["units"] == sum(r["on_hand"] for r in s["rows"]) == sum(w["units"] for w in s["warehouses"])
    val = repo.stock_valuation()
    assert to_paisa(s["value"]) == to_paisa(s["pool_value"]) == to_paisa(val["at_cost"])
    assert to_paisa(s["value"]) == sum(to_paisa(w["value"]) for w in s["warehouses"]) == sum(to_paisa(r["value"]) for r in s["rows"])
    # a holding that disagrees with its movements is flagged, not hidden
    with repo._tx() as cur: cur.execute("INSERT INTO stock (warehouse_id, sku, on_hand, reserved) VALUES ('WH-VEHARI', 'GHOST', 0, 0)")
    with repo._tx() as cur: cur.execute("UPDATE stock SET on_hand = on_hand + 1 WHERE warehouse_id='WH-MULTAN' AND sku='NPK-25'")
    s = c.get("/api/office/stock", headers=O).json()
    assert not s["ledger_ok"] and s["ledger_mismatches"] == [{"warehouse_id": "WH-MULTAN", "sku": "NPK-25", "on_hand": 96, "ledger": 95}]


def test_movements_show_the_balance_after_each_move(c, H, repo):
    O = H["owner"]
    c.post("/api/stock/transfer", json={"from_warehouse": "WH-MULTAN", "to_warehouse": "WH-VEHARI", "sku": "UREA-50", "qty": 20}, headers=O)
    c.post("/api/stock/adjust", json={"warehouse_id": "WH-MULTAN", "sku": "UREA-50", "delta": -2, "reason": "torn bags"}, headers=O)
    d = c.get("/api/office/stock/moves?sku=UREA-50&warehouse_id=WH-MULTAN", headers=O).json()
    m = d["moves"]
    assert (m[0]["kind"], m[0]["delta"], m[0]["ref"], m[0]["balance_after"]) == ("adjust", -2, "torn bags", 398)
    assert (m[1]["kind"], m[1]["balance_after"]) == ("transfer_out", 400)
    for newer, older in zip(m, m[1:], strict=False):
        assert older["balance_after"] == newer["balance_after"] - newer["delta"]
    assert m[-1]["balance_after"] == m[-1]["delta"]            # the first move ever starts from nothing
    assert d["total_moves"] == len(m) and d["levels"] == [{"warehouse_id": "WH-MULTAN", "on_hand": 398, "reserved": 0}]
    assert c.get("/api/office/stock/moves?sku=NOPE", headers=O).status_code == 404


# ====================================================================== clients
def test_client_list_balances_match_outstanding_and_aging(c, H, repo):
    K = H["clerk"]
    c.post("/api/payments", json={"customer_id": "C-004", "amount": 5000, "method": "cash"}, headers=K)
    c.post("/api/customers", json={"name": "Faisal Agro", "phone": "0301-2223334", "credit_limit": 150000, "opening_balance": 25000}, headers=K)
    rows = c.get("/api/office/clients", headers=K).json()
    aging = {a["customer_id"]: a for a in repo.aging()}
    assert len(rows) == len(repo.list_customers(include_inactive=True))
    for r in rows:
        assert to_paisa(r["balance"]) == repo.outstanding_paisa(r["customer_id"]), r["name"]
        assert r["days_overdue"] == (aging[r["customer_id"]]["days_overdue"] if r["customer_id"] in aging else 0), r["name"]
        limit = repo.customer_credit_limit_paisa(r["customer_id"])
        assert r["over_limit"] == (limit > 0 and repo.outstanding_paisa(r["customer_id"]) > limit)
    by = {r["customer_id"]: r for r in rows}
    assert by["C-004"]["balance"] == 120000.0 and by["C-004"]["route_name"] == "Multan South"
    assert next(r for r in rows if r["name"] == "Faisal Agro")["balance"] == 25000.0
    assert [r["name"] for r in c.get("/api/office/clients?q=vehari", headers=K).json()] == sorted(r["name"] for r in rows if r["route_name"] == "Vehari Road" or "vehari" in r["address"].lower())


def test_client_list_is_a_fixed_number_of_queries(repo):
    """No query per customer: the statement count is the same for 10 customers as for 40."""
    from munshi.domain.models import Customer

    def statements() -> int:
        n = [0]
        repo._conn.set_trace_callback(lambda _s: n.__setitem__(0, n[0] + 1))
        try:
            repo.client_rows()
        finally:
            repo._conn.set_trace_callback(None)
        return n[0]
    few = statements()
    for i in range(30):
        cu = repo.upsert_customer(Customer("", f"Extra {i:02d}", "", "standard", 1000))
        repo.opening_balance(cu.customer_id, 100 + i, "owner")
    assert statements() == few


def test_clerk_still_cannot_raise_a_credit_limit(c, H, repo):
    """The console edits clients through the existing PATCH route, whose rule is unchanged."""
    body = {"name": "Al-Barakah Traders", "phone": "0300-1111004", "credit_limit": 900000, "route_id": "R-MULTAN-S"}
    r = c.patch("/api/customers/C-004", json=body, headers=H["clerk"])
    assert r.status_code == 403 and r.json()["detail"] == "raising or removing a customer's credit limit needs the owner"
    assert repo.get_customer("C-004").credit_limit == 300000


def test_phone_app_links_to_the_console_for_the_office_only():
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "src" / "munshi" / "web" / "static"
    views, i18n = (static / "views.js").read_text(encoding="utf-8"), (static / "i18n.js").read_text(encoding="utf-8")
    assert "can('reports:read') ? `<a class=\"item tap\" href=\"/office\">" in views
    assert i18n.count("office_console:") == 2          # English and Urdu
