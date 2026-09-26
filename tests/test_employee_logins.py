"""Employee app logins (owner decision 5, plan §4.3): one employee master; a login is optional; the owner types a PIN
or gets a random non-weak one shown ONCE; the PIN is never stored in clear anywhere -- proven by grepping the actual
database files (registry and business, WAL included) and the captured logs; first sign-in must change it; ending
employment ends every session and blocks sign-in while keeping the history."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from munshi.auth import validate_pin
from munshi.auth.ratelimit import RateLimiter
from munshi.auth.registry import Registry, generate_pin
from munshi.tenancy.hub import DEMO_BUSINESS_ID
from munshi.web.app import build_app
from tests.test_payroll_support import enable_payroll

OWNER = ("0300-0000001", "1111")
CLERK = ("0300-0000002", "2222")


@pytest.fixture
def env(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    app = build_app(data_dir=str(tmp_path), demo=True, scheduler=False)
    app.state.login_limiter = RateLimiter(rate_per_minute=10_000, burst=10_000)
    c = TestClient(app)
    enable_payroll(app.state.hub.platform(DEMO_BUSINESS_ID).repo)
    return c, app, tmp_path


def _login(c, phone, pin):
    r = c.post("/api/session", json={"phone": phone, "pin": pin})
    assert r.status_code == 200, r.text
    return {"X-Session": r.json()["token"]}, r.json()["me"]


def _files(tmp: Path) -> bytes:
    """Every byte Munshi has written to disk: registry + business DBs, their WAL/SHM, the agents checkpoint DB."""
    return b"".join(p.read_bytes() for p in tmp.rglob("*") if p.is_file())


def _pin_count(tmp: Path, pin: str) -> int:
    return _files(tmp).count(pin.encode())


def test_generated_pin_is_random_non_weak_and_never_repeats_a_pattern():
    pins = {generate_pin() for _ in range(300)}
    assert len(pins) > 290                                   # random, not a sequence
    for p in pins:
        assert len(p) == 6 and p.isdigit() and len(set(p)) >= 4
        assert validate_pin(p) == p                          # passes the weak-PIN rules
    for weak in ("123456", "987654", "121212", "505050", "123123", "456789", "890123", "1313"):
        with pytest.raises(ValueError):
            validate_pin(weak)


def test_registry_flag_and_hash_only():
    reg = Registry()
    b = reg.create_business("T Traders")
    u = reg.create_user(b["business_id"], "Rafiq", "0301-7654321", "driver", "739218", must_change_pin=True)
    row = reg._conn.execute("SELECT * FROM users WHERE user_id=?", (u["user_id"],)).fetchone()
    assert row["must_change_pin"] == 1 and "739218" not in " ".join(str(v) for v in dict(row).values())
    tok, p, _ = reg.authenticate("03017654321", "739218")
    assert p.must_change_pin and reg.resolve(tok).must_change_pin
    reg.set_pin(u["user_id"], "5829", must_change=False)           # the user's own choice clears the flag (and signs out)
    assert not reg.authenticate("03017654321", "5829")[1].must_change_pin
    Registry(":memory:")                                          # the ALTER is idempotent on a fresh registry


def test_add_employee_with_generated_login_shows_the_pin_once_and_nowhere_else(env, caplog, capsys):
    c, app, tmp = env
    O, _ = _login(c, *OWNER)
    r = c.post("/api/employees", json={"name": "Asif Loader", "role_hint": "helper", "phone": "0301-5550001",
                                       "app_login": {"role": "driver", "generate": True}}, headers=O)
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    login = r.json()["login"]
    pin = login["pin_once"]
    assert len(pin) == 6 and login["must_change_pin"] is True and pin in login["whatsapp_text"] and "0301-5550001" in login["whatsapp_text"]
    assert r.json()["employee"]["has_login"] is True

    # ---- the PIN is nowhere at rest: not in any database file (registry, business, WAL), audit, chat, outbox, approvals
    repo = app.state.hub.platform(DEMO_BUSINESS_ID).repo
    for t in ("audit", "chat", "outbox", "approvals", "employees", "employee_events"):
        for row in repo._all(f"SELECT * FROM {t}"):
            assert pin not in " ".join(str(v) for v in tuple(row)), t
    audit = [r for r in repo.audit_log(20) if r["action"] == "employee_login_created"]
    assert audit and audit[0]["payload"] == {"role": "driver", "generated": True}
    with repo._lock:
        repo._conn.execute("PRAGMA wal_checkpoint(FULL)")
    assert _pin_count(tmp, pin) == 0, "the generated PIN reached a file on disk"
    # ---- and not in the logs
    assert pin not in caplog.text and pin not in capsys.readouterr().err

    # ---- a second look at the employee never shows it again
    emp_id = r.json()["employee"]["employee_id"]
    assert pin not in c.get(f"/api/employees/{emp_id}", headers=O).text

    # ---- first sign-in: only /api/me, /api/me/pin and logout until the PIN is changed
    H, me = _login(c, "0301-5550001", pin)
    assert me["must_change_pin"] is True
    blocked = c.get("/api/orders", headers=H)
    assert blocked.status_code == 403 and blocked.json()["detail"]["error"] == "pin_change_required"
    assert c.get("/api/me/payslips", headers=H).status_code == 403
    # a Host header crafted so a URL rebuilt from it would read /api/me (the BadHost pattern) changes nothing
    assert c.get("/api/orders", headers=H | {"host": "testserver/api/me?"}).status_code == 403
    assert c.get("/api/me", headers=H).json()["must_change_pin"] is True
    assert c.post("/api/me/pin", json={"old_pin": pin, "new_pin": pin}, headers=H).status_code == 400    # must actually change
    assert c.post("/api/me/pin", json={"old_pin": pin, "new_pin": "7294"}, headers=H).json()["signed_out"] is True
    H, me = _login(c, "0301-5550001", "7294")
    assert me["must_change_pin"] is False and c.get("/api/me/payslips", headers=H).status_code == 200
    assert c.get("/api/orders", headers=H).status_code == 200


def test_typed_pin_is_never_echoed_and_still_must_be_changed(env):
    c, app, tmp = env
    O, _ = _login(c, *OWNER)
    r = c.post("/api/employees", json={"name": "Sajid", "phone": "0301-5550002", "app_login": {"role": "salesman", "pin": "8316"}}, headers=O)
    assert r.status_code == 201 and "8316" not in r.text and "pin_once" not in r.json()["login"]
    _, me = _login(c, "0301-5550002", "8316")
    assert me["must_change_pin"] is True
    # weak typed PIN refused; both ways at once refused
    assert c.post("/api/employees", json={"name": "Weak", "phone": "0301-5550003", "app_login": {"role": "driver", "pin": "123456"}}, headers=O).status_code == 400
    assert c.post("/api/employees", json={"name": "Both", "phone": "0301-5550003", "app_login": {"role": "driver", "pin": "8316", "generate": True}},
                  headers=O).status_code == 422


def test_a_failed_login_leaves_no_orphan_and_the_phone_is_free(env):
    c, app, tmp = env
    O, _ = _login(c, *OWNER)
    before = len(c.get("/api/employees?status=all", headers=O).json()["employees"])
    # the phone belongs to a demo user already: the login fails first, and no employee row is written
    r = c.post("/api/employees", json={"name": "Dup", "phone": "0300-0000003", "app_login": {"role": "driver", "generate": True}}, headers=O)
    assert r.status_code == 400
    assert len(c.get("/api/employees?status=all", headers=O).json()["employees"]) == before


def test_reset_pin_generates_a_new_one_and_ends_sessions(env):
    c, app, tmp = env
    O, _ = _login(c, *OWNER)
    r = c.post("/api/employees", json={"name": "Tariq", "phone": "0301-5550004"}, headers=O).json()      # no login (a loader)
    emp_id = r["employee"]["employee_id"]
    assert r["employee"]["has_login"] is False and "login" not in r
    first = c.post(f"/api/employees/{emp_id}/login", json={"role": "driver", "generate": True}, headers=O).json()["login"]["pin_once"]
    H, _ = _login(c, "0301-5550004", first)
    again = c.post(f"/api/employees/{emp_id}/login", json={"role": "driver", "generate": True}, headers=O)
    assert again.status_code == 409                                                                   # one login per employee
    reset = c.post(f"/api/employees/{emp_id}/login/reset-pin", json={"generate": True}, headers=O)
    assert reset.status_code == 200 and reset.headers["cache-control"] == "no-store"
    new = reset.json()["login"]["pin_once"]
    assert new != first and c.get("/api/me", headers=H).status_code == 401                           # old session gone
    assert c.post("/api/session", json={"phone": "0301-5550004", "pin": first}).status_code == 401
    _, me = _login(c, "0301-5550004", new)
    assert me["must_change_pin"] is True
    repo = app.state.hub.platform(DEMO_BUSINESS_ID).repo
    kinds = [e["kind"] for e in repo.employee_events(emp_id)]
    assert kinds == ["joined", "login_created", "pin_reset"]
    assert [a["payload"] for a in repo.audit_log(50) if a["action"] == "employee_pin_reset"] == [{"generated": True}]


def test_ending_employment_kills_sessions_blocks_sign_in_and_keeps_history(env):
    c, app, tmp = env
    O, _ = _login(c, *OWNER)
    r = c.post("/api/employees", json={"name": "Waqas", "phone": "0301-5550005", "joined_on": "2026-01-02",
                                       "app_login": {"role": "clerk", "generate": True}}, headers=O).json()
    emp_id, pin = r["employee"]["employee_id"], r["login"]["pin_once"]
    H, _ = _login(c, "0301-5550005", pin)
    c.post("/api/me/pin", json={"old_pin": pin, "new_pin": "6082"}, headers=H)
    H, _ = _login(c, "0301-5550005", "6082")
    H2, _ = _login(c, "0301-5550005", "6082")                           # a second phone
    end = c.post(f"/api/employees/{emp_id}/end", json={"left_on": "2026-09-20", "reason": "resigned"}, headers=O)
    assert end.status_code == 200 and end.json()["login_disabled"] is True and end.json()["employee"]["status"] == "left"
    assert c.get("/api/me", headers=H).status_code == 401 and c.get("/api/me", headers=H2).status_code == 401
    assert c.post("/api/session", json={"phone": "0301-5550005", "pin": "6082"}).status_code == 401
    # history kept: the employee row, its events and the login row all stay
    emp = c.get(f"/api/employees/{emp_id}", headers=O).json()
    assert emp["left_on"] == "2026-09-20" and [e["kind"] for e in emp["events"]][-2:] == ["left", "login_disabled"]
    assert c.get("/api/employees?status=left", headers=O).json()["count"] >= 1
    # no reset for someone who has left; rehire can restore the login with a NEW PIN shown once
    assert c.post(f"/api/employees/{emp_id}/login/reset-pin", json={"generate": True}, headers=O).status_code == 409
    back = c.post(f"/api/employees/{emp_id}/rehire", json={"rejoined_on": "2026-10-01", "restore_login": True}, headers=O).json()
    _, me = _login(c, "0301-5550005", back["login"]["pin_once"])
    assert me["must_change_pin"] is True


def test_only_the_owner_manages_logins(env):
    c, app, tmp = env
    O, _ = _login(c, *OWNER)
    K, _ = _login(c, *CLERK)
    emp_id = c.post("/api/employees", json={"name": "Zahid", "phone": "0301-5550006"}, headers=O).json()["employee"]["employee_id"]
    assert c.post(f"/api/employees/{emp_id}/login", json={"role": "driver", "generate": True}, headers=K).status_code == 403
    assert c.post(f"/api/employees/{emp_id}/login/reset-pin", json={"generate": True}, headers=K).status_code == 403
    assert c.post("/api/employees", json={"name": "X Y", "phone": "0301-5550007", "app_login": {"role": "owner", "generate": True}},
                  headers=K).status_code == 403
    # the last owner cannot be ended out of the business
    owner_emp = next(e for e in c.get("/api/employees", headers=O).json()["employees"] if e["name"] == "Sultan Ahmed")
    assert c.post(f"/api/employees/{owner_emp['employee_id']}/end", json={"left_on": "2026-09-20", "reason": "test"}, headers=O).status_code == 409


def test_legacy_staff_route_also_writes_the_employee_master(env):
    c, app, tmp = env
    O, _ = _login(c, *OWNER)
    u = c.post("/api/staff", json={"name": "Sana Malik", "phone": "0301-2223334", "role": "clerk", "pin": "2580"}, headers=O).json()
    emps = c.get("/api/employees", headers=O).json()["employees"]
    sana = next(e for e in emps if e["name"] == "Sana Malik")
    assert sana["user_id"] == u["user_id"] and sana["role_hint"] == "clerk"
    _, me = _login(c, "0301-2223334", "2580")
    assert me["must_change_pin"] is False                    # legacy behaviour unchanged
    c.patch(f"/api/staff/{u['user_id']}", json={"active": False}, headers=O)
    repo = app.state.hub.platform(DEMO_BUSINESS_ID).repo
    assert [e["kind"] for e in repo.employee_events(sana["employee_id"])][-1] == "login_disabled"
