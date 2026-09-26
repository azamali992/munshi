"""Period close and lock (plan §3.4, §4.1): closing through a date refuses every dated write on or before it -- in the
repository (a clear message naming the date) and in the database (V8 triggers); reopening needs the owner and a reason
and is audited; the trial-balance snapshot catches a closed period whose numbers change ("books drift").
Then the web layer: every finance route under its SEAMS §5 permission, and the clerk's redacted book."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from munshi.domain.repository import MunshiRepository, StateError
from tests.test_ledger_projection import _biz
from tests.test_reconciliation_month import clock  # noqa: F401  (clock is the fixture)


def _closed(clock) -> tuple[MunshiRepository, dict]:  # noqa: F811
    r = _biz(MunshiRepository())
    clock.at(5, 10)
    r.record_capital(100_000, "cash", on_date="2026-09-05", actor="owner", approved_by="owner")
    r.record_expense("fuel", 1_000, "diesel", "cash", "B", "h", expense_date="2026-09-05")
    clock.at(20, 10)
    return r, r.close_period("2026-09-15", note="first half", actor="owner", approved_by="owner")


def test_close_refuses_today_the_future_and_an_unapproved_close(clock):  # noqa: F811
    r = _biz(MunshiRepository())
    clock.at(20, 10)
    with pytest.raises(StateError, match="before today"):
        r.close_period("2026-09-20", actor="owner", approved_by="owner")
    with pytest.raises(StateError, match="approved by the owner"):
        r.close_period("2026-09-15", actor="owner", approved_by=None)
    r.record_payment("C1", 500, "bank", "IBFT", "h")                          # unassigned money: an alarm
    clock.at(22, 10)
    with pytest.raises(StateError, match="alarms"):
        r.close_period("2026-09-21", actor="owner", approved_by="owner")
    forced = r.close_period("2026-09-21", actor="owner", approved_by="owner", force=True)
    assert forced["locked_through"] == "2026-09-21"
    assert r.audit_log(1)[0]["payload"]["forced_alarms"] == ["unassigned_money"]
    with pytest.raises(StateError, match="already closed through"):
        r.close_period("2026-09-10", actor="owner", approved_by="owner")


def test_the_lock_refuses_every_dated_write_in_a_closed_period(clock):  # noqa: F811
    r, closed = _closed(clock)
    assert closed["locked_through"] == "2026-09-15" and closed["sha256"]
    lines = [{"code": "7000", "debit": 10}, {"code": "1000", "account_id": "CASH", "credit": 10}]
    for write in (lambda: r.record_expense("fuel", 10, "late", "cash", "B", "h", expense_date="2026-09-15"),
                  lambda: r.post_journal("2026-09-15", "bank_charge", "late charge", lines, actor="owner", approved_by="owner"),
                  lambda: r.transfer("CASH", r.add_money_account("bank", "HBL", "HBL", actor="owner", approved_by="owner")["account_id"], 1, on_date="2026-09-01"),
                  lambda: r.set_method_route("cash", "CASH", "2026-09-10", "owner", "owner"),
                  lambda: r.record_drawing(10, "cash", on_date="2026-09-14", actor="owner", approved_by="owner"),
                  lambda: r.add_loan("Haji", "informal", 10, "cash", on_date="2026-09-01", actor="owner", approved_by="owner"),
                  lambda: r.add_fixed_asset("Scale", "equipment", 10, "2026-09-02", 12, method="cash", actor="owner", approved_by="owner"),
                  lambda: r.record_opening_balances("2026-09-01", [{"account_id": "CASH", "amount": 5}], [], [], actor="owner", approved_by="owner"),
                  lambda: r.assert_period_open("2026-09-15")):
        with pytest.raises(StateError, match="closed through 2026-09-15"):
            write()
    # the database refuses it too, behind the repository's back
    for sql in ("INSERT INTO expenses (expense_id, category, amount, note, method, paid_by, expense_date, created_at) VALUES ('EXP-L','fuel',1,'','cash','','2026-09-15','x')",
                "INSERT INTO account_transfers (transfer_id, from_account, to_account, amount_paisa, transfer_date, created_at) VALUES ('XFR-L','CASH','ACC-HBL',1,'2026-09-01','x')",
                "INSERT INTO method_routes (method, account_id, effective_from, set_at) VALUES ('cash','CASH','2026-09-01','x')"):
        with pytest.raises(sqlite3.IntegrityError, match="books are closed"):
            r._conn.execute(sql)
    # the day after the lock is open
    r.record_expense("fuel", 10, "fine", "cash", "B", "h", expense_date="2026-09-16")
    r.post_journal("2026-09-16", "bank_charge", "charge", lines, actor="owner", approved_by="owner")
    assert r.verify_books()["alarms"] == []


def test_depreciation_for_a_closed_unposted_month_is_refused(clock):  # noqa: F811
    r = _biz(MunshiRepository())
    clock.at(1, 10)
    r.add_fixed_asset("Loader", "vehicle", 120_000, "2026-08-01", 12, method="cash", actor="owner", approved_by="owner")
    r.run_depreciation("2026-08", "owner", "owner")
    clock.at(20, 10)
    r.close_period("2026-09-15", actor="owner", approved_by="owner", force=True)        # the drawer is negative: forced
    assert r.run_depreciation("2026-08", "owner", "owner")["posted"] == []                   # done months are simply skipped
    posted = r.run_depreciation("2026-09", "owner", "owner")["posted"]                       # dated Sep 30: open
    assert [x["period"] for x in posted] == ["2026-09"]


def test_reopen_needs_a_reason_is_audited_once_and_keeps_the_snapshot(clock):  # noqa: F811
    r, closed = _closed(clock)
    cid = closed["close_id"]
    with pytest.raises(ValueError, match="reason"):
        r.reopen_period(cid, "  ", "owner", "owner")
    with pytest.raises(StateError, match="approved by the owner"):
        r.reopen_period(cid, "found a bill", "owner", "")
    status = r.reopen_period(cid, "found a late bill", "owner", "owner")
    assert status["locked_through"] is None and status["closes"][0]["reopen_reason"] == "found a late bill"
    with pytest.raises(StateError, match="already reopened"):
        r.reopen_period(cid, "again", "owner", "owner")
    r.record_expense("fuel", 10, "late bill", "cash", "B", "h", expense_date="2026-09-10")   # open again
    for sql in ("UPDATE period_closes SET snapshot='{}'", "DELETE FROM period_closes", "UPDATE period_closes SET reopened_at=NULL"):
        with pytest.raises(sqlite3.IntegrityError):
            r._conn.execute(sql)
    acts = [a["action"] for a in r.audit_log(10)]
    assert acts.count("close_period") == 1 and acts.count("reopen_period") == 1


def test_a_closed_period_whose_numbers_change_raises_books_drift(clock):  # noqa: F811
    r, closed = _closed(clock)
    assert r.verify_books()["ok"]
    # a backdated khata row (no trigger guards the ledger's UTC stamp -- only a bug or a hand edit could do this)
    r._conn.execute("INSERT INTO ledger (entry_id, customer_id, kind, amount, ref, created_at, method, received_by) "
                    "VALUES ('RCP-BACK','C1','payment',-5000,'x','2026-09-10T08:00:00+00:00','cash','office')")
    alarms = r.verify_books()["alarms"]
    assert [a["code"] for a in alarms] == ["books_drift"] and "2026-09-15" in alarms[0]["message"]


# ====================================================================== web: permissions per SEAMS §5
@pytest.fixture
def client():
    from munshi.web.app import build_app
    return TestClient(build_app(in_memory=True, demo=True, scheduler=False))


def _token(client, phone, pin) -> dict:
    return {"X-Session": client.post("/api/session", json={"phone": phone, "pin": pin}).json()["token"]}


D = "2026-09-01"
ROUTES = [  # (method, path, json body, permission)
    ("get", "/api/accounts", None, "books:read"),
    ("get", "/api/accounts/method-routes", None, "books:read"),
    ("get", "/api/accounts/CASH/book", None, "books:read"),
    ("get", "/api/accounts/CASH/reconciliation", None, "books:read"),
    ("get", "/api/finance/periods", None, "books:read"),
    ("post", "/api/accounts/transfer", {"from_account": "CASH", "to_account": "ACC-NONE", "amount": 1}, "books:write"),
    ("post", "/api/accounts/CASH/cash-count", {"counted": 0}, "books:write"),
    ("post", "/api/accounts/CASH/clear", {"items": [{"source": "ledger", "source_id": "RCP-NONE"}], "cleared_on": D}, "books:write"),
    ("post", "/api/accounts/CASH/reconciliations", {"statement_date": D, "statement_balance": 0}, "books:write"),
    *[("get", f"/api/finance/{p}", None, "finance:read") for p in ("pnl", "balance-sheet", "cash-flow", "trial-balance", "kpis", "margins?by=route",
                                                                   "assets", "loans", "verify", "general-journal", "general-journal.csv", "journal/JV-NONE")],
    ("post", "/api/accounts", {"kind": "bank", "name": "HBL", "provider": "HBL"}, "finance:write"),
    ("put", "/api/accounts/method-routes", {"method": "bank", "account_id": "ACC-NONE", "effective_from": D}, "finance:write"),
    ("patch", "/api/accounts/CASH", {"name": "Galla"}, "finance:write"),
    ("post", "/api/accounts/transfers/XFR-NONE/reverse", {"reason": "wrong"}, "finance:write"),
    ("post", "/api/cash-counts/CC-NONE/post", None, "finance:write"),
    ("post", "/api/finance/capital", {"amount": 1, "method": "cash"}, "finance:write"),
    ("post", "/api/finance/drawings", {"amount": 1, "method": "cash"}, "finance:write"),
    ("post", "/api/finance/loans", {"lender": "Haji", "amount": 1, "method": "cash"}, "finance:write"),
    ("post", "/api/finance/loans/LN-NONE/repay", {"principal": 1, "method": "cash"}, "finance:write"),
    ("post", "/api/finance/assets", {"name": "Scale", "category": "equipment", "cost": 1, "acquired_on": D, "life_months": 12, "method": "cash"}, "finance:write"),
    ("post", "/api/finance/assets/FA-NONE/dispose", {"on_date": D}, "finance:write"),
    ("post", "/api/finance/depreciation/run", {"through_period": "2026-08"}, "finance:write"),
    ("post", "/api/finance/journal", {"entry_date": D, "kind": "bank_charge", "memo": "charges",
                                      "lines": [{"code": "7000", "debit": 1}, {"code": "1000", "account_id": "CASH", "credit": 1}]}, "finance:write"),
    ("post", "/api/finance/journal/JV-NONE/reverse", {"reason": "wrong"}, "finance:write"),
    ("post", "/api/finance/opening-balances", {"as_of": D, "money": [{"account_id": "CASH", "amount": 1}]}, "finance:write"),
    ("post", "/api/finance/periods/close", {"through_date": D}, "finance:write"),
    ("post", "/api/finance/periods/999/reopen", {"reason": "late bill"}, "finance:write"),
]
ROLE_LOGIN = {"owner": ("0300-0000001", "1111"), "clerk": ("0300-0000002", "2222"), "driver": ("0300-0000003", "3333")}
HOLDERS = {"books:read": {"owner", "clerk"}, "books:write": {"owner", "clerk"}, "finance:read": {"owner"}, "finance:write": {"owner"}}


def test_every_finance_route_is_under_its_permission(client):
    from munshi.auth.principal import roles_with
    for perm, roles in HOLDERS.items():
        assert set(roles_with(perm)) == roles                              # the matrix this test encodes is SEAMS §5's
    heads = {role: _token(client, *login) for role, login in ROLE_LOGIN.items()}
    for method, path, body, perm in ROUTES:
        for role, h in heads.items():
            resp = getattr(client, method)(path, headers=h, **({"json": body} if body is not None else {}))
            if role in HOLDERS[perm]:
                assert resp.status_code not in (401, 403, 405, 422, 500), (role, method, path, resp.status_code, resp.text[:200])
            else:
                assert resp.status_code == 403, (role, method, path, resp.status_code)
    assert client.get("/api/accounts").status_code == 401


def test_money_routes_take_an_account_and_the_clerk_book_is_redacted(client):
    owner, clerk = _token(client, *ROLE_LOGIN["owner"]), _token(client, *ROLE_LOGIN["clerk"])
    hbl = client.post("/api/accounts", headers=owner, json={"kind": "bank", "name": "HBL current", "provider": "HBL"}).json()["account_id"]
    r = client.post("/api/payments", headers=clerk, json={"customer_id": "C-001", "amount": 1000, "method": "bank", "account_id": hbl})
    assert r.status_code == 201 and r.json()["account_id"] == hbl
    assert client.post("/api/payments", headers=clerk, json={"customer_id": "C-001", "amount": 10, "method": "cash", "account_id": hbl}).status_code == 400
    assert client.post("/api/expenses", headers=clerk, json={"category": "fuel", "amount": 100, "method": "bank", "account_id": hbl}).status_code == 201
    p = client.post("/api/purchases", headers=clerk, json={"supplier_id": "S-001", "items": [{"sku": "UREA-50", "qty": 1, "unit_cost": 3600}],
                                                          "paid_amount": 3600, "method": "bank", "account_id": hbl})
    assert p.status_code == 201
    assert client.post("/api/suppliers/S-001/pay", headers=owner, json={"amount": 50, "method": "bank", "account_id": hbl}).status_code == 201
    book = client.get(f"/api/accounts/{hbl}/book", headers=owner).json()
    assert book["closing"] == 1000 - 100 - 3600 - 50 and not book["redacted"]
    assert client.get(f"/api/accounts/{hbl}/book", headers=clerk).json()["redacted"] is True
    csv = client.get("/api/finance/general-journal.csv", headers=owner)
    assert csv.status_code == 200 and csv.text.startswith("date,doc,source,code,account,party,debit,credit,memo")


def test_the_close_checklist_says_what_is_not_done_yet(clock):  # noqa: F811
    r = _biz(MunshiRepository())
    clock.at(2, 10)
    bank = r.add_money_account("bank", "HBL current", opening_balance=50_000, opening_date="2026-09-01", actor="owner", approved_by="owner")
    r.add_fixed_asset("Shehzore", "vehicle", 1_200_000, "2026-08-01", 120, funded_by="opening", actor="owner", approved_by="owner")
    clock.at(20, 10)
    items = {c["key"]: c for c in r.close_checklist("2026-09-15")}
    assert items["books"]["ok"]
    assert not items["depreciation"]["ok"] and "Shehzore" in items["depreciation"]["detail"]
    assert not items["reconciled"]["ok"] and "HBL current" in items["reconciled"]["detail"]
    assert not items["cash_count"]["ok"]
    # nothing is closed yet: the owner is offered last month's end
    st = r.period_status()
    assert st["suggested_through"] == "2026-08-31" and [c["key"] for c in st["checklist"]][0] == "books"

    r.run_depreciation("2026-09", "owner", "owner")
    r.save_reconciliation(bank["account_id"], "2026-09-15", 50_000, "owner")
    clock.at(14, 18)
    r.count_cash("CASH", r.account_balance_paisa("CASH") / 100, actor="owner")
    clock.at(20, 10)
    assert all(c["ok"] for c in r.close_checklist("2026-09-15")), r.close_checklist("2026-09-15")


def test_the_checklist_hides_payroll_totals_from_a_clerk(clock):  # noqa: F811
    r = _biz(MunshiRepository())
    clock.at(20, 10)
    alarm = {"code": "payroll_expense", "message": "payroll expense rows for Salaries (Rs 236,923.08) differ", "amount": 1.0}
    r.verify_books = lambda as_of=None: {"alarms": [alarm]}
    assert "236,923" not in r.close_checklist("2026-09-15")[0]["detail"]
    assert "236,923" in r.close_checklist("2026-09-15", redact_payroll=False)[0]["detail"]
