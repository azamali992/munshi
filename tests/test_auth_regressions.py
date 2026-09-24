"""Regression tests for four reproduced auth bugs:

1. switch_business handed out a session for any business where the caller's
   phone number had a row — no PIN for the target business was needed, so
   signing up a new business with a victim's phone was a full takeover.
2. The shared SQLite connection was read without holding the registry lock;
   concurrent requests raised sqlite3.InterfaceError / returned garbage.
3. The login/sign-up rate limiters keyed on the leftmost X-Forwarded-For
   element, which the client controls, from any peer.
4. The failed-PIN counter was read, then (after the slow hash) written back,
   so concurrent wrong PINs overwrote each other and the lock never tripped.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from munshi.auth import AuthError, LockedError, Registry
from munshi.auth.registry import LOCK_AFTER
from munshi.web.app import build_app
from munshi.web.routes.auth import _client

VICTIM_PHONE = "0300-5550001"
VICTIM_PIN = "7391"
ATTACKER_PIN = "4826"


def _run_parallel(n: int, fn) -> list:
    """Run fn(i) on n threads released at the same instant; return each result or exception."""
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results[i] = fn(i)
        except BaseException as e:  # noqa: BLE001 - the test inspects what was raised
            results[i] = e

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=120)
    assert not any(t.is_alive() for t in threads), "a worker hung"
    return results


# ---------------------------------------------------------------- bug 1
def test_switch_business_requires_the_target_business_pin():
    reg = Registry()
    a = reg.create_business("Victim Traders", "Lahore")
    reg.create_user(a["business_id"], "Victim", VICTIM_PHONE, "owner", VICTIM_PIN)
    b = reg.create_business("Attacker Traders", "Karachi")
    reg.create_user(b["business_id"], "Attacker", VICTIM_PHONE, "owner", ATTACKER_PIN)

    _, p, _ = reg.authenticate(VICTIM_PHONE, ATTACKER_PIN)
    assert p.business_id == b["business_id"]

    with pytest.raises(TypeError):                      # no PIN at all: not even callable
        reg.switch_business(p, a["business_id"])        # type: ignore[call-arg]
    with pytest.raises(AuthError):                      # the attacker's own PIN is not the victim's
        reg.switch_business(p, a["business_id"], pin=ATTACKER_PIN)
    with pytest.raises(AuthError):
        reg.switch_business(p, "B-NOPE", pin=VICTIM_PIN)
    t, p2 = reg.switch_business(p, a["business_id"], pin=VICTIM_PIN)
    assert p2.business_id == a["business_id"] and reg.resolve(t).business_id == a["business_id"]


def test_signup_with_victims_phone_then_switch_is_refused_over_http():
    app = build_app(in_memory=True, demo=False, scheduler=False)
    app.state.signup_open = True
    c = TestClient(app)
    ra = c.post("/api/signup", json={"business_name": "Victim Traders", "owner_name": "Victim", "phone": VICTIM_PHONE, "pin": VICTIM_PIN})
    assert ra.status_code == 201, ra.text
    victim_biz = ra.json()["me"]["business"]["id"]
    rb = c.post("/api/signup", json={"business_name": "Attacker Traders", "owner_name": "Mallory", "phone": VICTIM_PHONE, "pin": ATTACKER_PIN})
    assert rb.status_code == 201, rb.text
    h = {"Authorization": f"Bearer {rb.json()['token']}"}

    r = c.post("/api/session/switch", json={"business_id": victim_biz}, headers=h)
    assert r.status_code in (403, 422), r.text             # no PIN: refused
    assert "token" not in r.json()
    r = c.post("/api/session/switch", json={"business_id": victim_biz, "pin": ATTACKER_PIN}, headers=h)
    assert r.status_code == 403, r.text                    # wrong PIN for the target: refused
    assert "token" not in r.json()

    # sign-in with the attacker's PIN must not reveal the victim's business either
    r = c.post("/api/session", json={"phone": VICTIM_PHONE, "pin": ATTACKER_PIN})
    assert r.status_code == 200 and r.json()["other_businesses"] == []

    # the legitimate path still works: the right PIN for the target business
    r = c.post("/api/session/switch", json={"business_id": victim_biz, "pin": VICTIM_PIN}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["me"]["business"]["id"] == victim_biz


# ---------------------------------------------------------------- bug 2
def test_registry_survives_concurrent_readers_and_writers(tmp_path):
    reg = Registry(str(tmp_path / "registry.db"))
    b = reg.create_business("Busy Traders", "Multan")
    u = reg.create_user(b["business_id"], "Ali", "0300-1234567", "owner", "2580")
    tokens = [reg._issue(u["user_id"], b["business_id"], f"dev{i}") for i in range(8)]
    n_threads, rounds = 64, 60

    def hammer(i: int) -> int:
        ok = 0
        for k in range(rounds):
            p = reg.resolve(tokens[(i + k) % len(tokens)])
            assert p.user_id == u["user_id"] and p.business_id == b["business_id"] and p.name == "Ali"
            assert reg.get_user(u["user_id"])["phone"] == "03001234567"
            assert reg.get_business(b["business_id"])["name"] == "Busy Traders"
            assert len(reg.list_users(b["business_id"])) == 1
            assert reg.owners_count(b["business_id"]) == 1
            assert len(reg.list_businesses()) == 1
            assert len(reg.sessions_for(u["user_id"])) >= len(tokens)
            if k % 10 == 0:                           # interleave writes with the reads
                reg.revoke(reg._issue(u["user_id"], b["business_id"], "tmp"))
            ok += 1
        return ok

    results = _run_parallel(n_threads, hammer)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"{len(errors)} of {n_threads} threads failed, first: {errors[0]!r}"
    assert results == [rounds] * n_threads


# ---------------------------------------------------------------- bug 3
def _req(peer: str, xff: str | None = None):
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_client_ip_ignores_forwarded_for_from_untrusted_peers(monkeypatch):
    monkeypatch.delenv("MUNSHI_TRUSTED_PROXY_IPS", raising=False)
    assert _client(_req("203.0.113.9", "1.2.3.4")) == "203.0.113.9"
    monkeypatch.setenv("MUNSHI_TRUSTED_PROXY_IPS", "10.0.0.0/8, 127.0.0.1")
    assert _client(_req("203.0.113.9", "1.2.3.4")) == "203.0.113.9"          # peer is not a proxy
    # peer is a trusted proxy: take the rightmost hop that is not itself trusted
    assert _client(_req("10.0.0.5", "6.6.6.6, 198.51.100.7")) == "198.51.100.7"
    assert _client(_req("10.0.0.5", "6.6.6.6, 198.51.100.7, 10.1.2.3")) == "198.51.100.7"
    assert _client(_req("127.0.0.1")) == "127.0.0.1"


def test_rotating_spoofed_forwarded_for_does_not_bypass_login_rate_limit(monkeypatch):
    monkeypatch.delenv("MUNSHI_TRUSTED_PROXY_IPS", raising=False)
    c = TestClient(build_app(in_memory=True, demo=False, scheduler=False))
    codes = [c.post("/api/session", json={"phone": "0300-9990000", "pin": "4826"},
                    headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code for i in range(15)]
    assert 429 in codes, codes
    assert codes[-1] == 429


def test_rotating_spoof_behind_trusted_proxy_does_not_bypass_either(monkeypatch):
    monkeypatch.setenv("MUNSHI_TRUSTED_PROXY_IPS", "10.0.0.1")
    c = TestClient(build_app(in_memory=True, demo=False, scheduler=False), client=("10.0.0.1", 40000))
    # the attacker controls the left of the chain; the proxy appends the real peer on the right
    codes = [c.post("/api/session", json={"phone": "0300-9990000", "pin": "4826"},
                    headers={"X-Forwarded-For": f"198.51.100.{i}, 203.0.113.50"}).status_code for i in range(15)]
    assert codes[-1] == 429, codes


def test_cli_does_not_trust_forwarded_headers_from_everyone(monkeypatch):
    import munshi.cli as cli
    seen = {}
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", SimpleNamespace(run=lambda *a, **k: seen.update(k)))
    monkeypatch.delenv("MUNSHI_TRUSTED_PROXY_IPS", raising=False)
    cli.cmd_serve(SimpleNamespace(host="127.0.0.1", port="8000"))
    assert seen.get("forwarded_allow_ips") != "*" and not seen.get("proxy_headers")
    monkeypatch.setenv("MUNSHI_TRUSTED_PROXY_IPS", "10.0.0.1")
    cli.cmd_serve(SimpleNamespace(host="127.0.0.1", port="8000"))
    assert seen["proxy_headers"] is True and seen["forwarded_allow_ips"] == "10.0.0.1"


# ---------------------------------------------------------------- bug 4
def test_parallel_wrong_pins_lock_the_account():
    reg = Registry()
    b = reg.create_business("Target Traders", "Lahore")
    u = reg.create_user(b["business_id"], "Ali", "0300-1234567", "owner", "2580")
    n = 40
    results = _run_parallel(n, lambda i: reg.authenticate("03001234567", "9137"))

    assert all(isinstance(r, AuthError) for r in results), results
    locked = [r for r in results if isinstance(r, LockedError)]
    counted = [r for r in results if not isinstance(r, LockedError)]
    # exactly LOCK_AFTER attempts were allowed to try a PIN; every one of them was counted
    assert len(counted) == LOCK_AFTER, f"{len(counted)} attempts reached the PIN check, expected {LOCK_AFTER}"
    assert len(locked) == n - LOCK_AFTER
    assert reg.get_user(u["user_id"])["locked"] is True
    with pytest.raises(LockedError):
        reg.authenticate("03001234567", "2580")      # even the right PIN waits


def test_success_on_one_business_does_not_leave_phantom_failures_on_another():
    reg = Registry()
    a = reg.create_business("First", "Lahore")
    ua = reg.create_user(a["business_id"], "Ali", "0300-1234567", "owner", "2580")
    b = reg.create_business("Second", "Karachi")
    reg.create_user(b["business_id"], "Ali", "0300-1234567", "owner", "9137")
    for _ in range(LOCK_AFTER + 3):                  # repeated good sign-ins to the second business
        reg.authenticate("03001234567", "9137")
    assert reg.get_user(ua["user_id"])["failed_attempts"] == 0
    assert reg.get_user(ua["user_id"])["locked"] is False
