"""Registry: PIN hashing, phone normalisation, lockout, sessions, business switching."""
from datetime import UTC, datetime, timedelta

import pytest

from munshi.auth import AuthError, LockedError, Registry, normalize_phone, validate_pin
from munshi.auth.principal import PERMISSIONS, Principal, roles_with
from munshi.auth.registry import hash_pin


@pytest.fixture
def reg():
    r = Registry()
    b = r.create_business("Test Traders", "Lahore")
    r.create_user(b["business_id"], "Ali", "0300 1234567", "owner", "2580")
    return r


def test_phone_normalisation():
    assert normalize_phone("0300-1234567") == "03001234567"
    assert normalize_phone("+92 300 1234567") == "03001234567"
    assert normalize_phone("3001234567") == "03001234567"
    with pytest.raises(ValueError):
        normalize_phone("12345")


def test_pin_policy():
    assert validate_pin("2580") == "2580" and validate_pin("739124") == "739124"
    for weak in ("1234", "0000", "1111", "123456", "8888"):
        with pytest.raises(ValueError):
            validate_pin(weak)
    assert validate_pin("1111", allow_weak=True) == "1111"
    with pytest.raises(ValueError):
        validate_pin("12a4")
    with pytest.raises(ValueError):
        validate_pin("123")


def test_pins_are_never_stored_in_clear(reg):
    row = reg._conn.execute("SELECT pin_hash, salt FROM users").fetchone()
    assert "2580" not in row["pin_hash"] and len(row["pin_hash"]) == 64 and len(row["salt"]) == 32
    assert hash_pin("2580", bytes.fromhex(row["salt"])) == row["pin_hash"]
    assert hash_pin("2580", b"other-salt-16by!") != row["pin_hash"]


def test_authenticate_resolve_and_revoke(reg):
    token, p, others = reg.authenticate("03001234567", "2580", device="pytest")
    assert token.startswith("ms_") and p.role == "owner" and p.name == "Ali" and others == []
    assert reg.resolve(token).user_id == p.user_id
    assert reg.sessions_for(p.user_id)[0]["device"] == "pytest"
    # the raw token is not in the registry
    assert not reg._conn.execute("SELECT 1 FROM sessions WHERE token_hash=?", (token,)).fetchone()
    reg.revoke(token)
    with pytest.raises(AuthError):
        reg.resolve(token)


def test_lockout_and_unlock(reg):
    for _ in range(4):
        with pytest.raises(AuthError):
            reg.authenticate("03001234567", "0000")
    with pytest.raises(AuthError):
        reg.authenticate("03001234567", "0000")          # fifth failure locks
    with pytest.raises(LockedError):
        reg.authenticate("03001234567", "2580")          # even the right PIN waits
    u = reg.get_user(reg.list_users(reg.list_businesses()[0]["business_id"])[0]["user_id"])
    assert u["locked"] is True
    # simulate the lock expiring
    reg._conn.execute("UPDATE users SET locked_until=? WHERE user_id=?", ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), u["user_id"]))
    token, _, _ = reg.authenticate("03001234567", "2580")
    assert token


def test_session_expiry(reg):
    token, p, _ = reg.authenticate("03001234567", "2580")
    reg._conn.execute("UPDATE sessions SET expires_at=?", ((datetime.now(UTC) - timedelta(days=1)).isoformat(),))
    with pytest.raises(AuthError):
        reg.resolve(token)
    assert reg.purge_expired() == 0            # already purged by resolve


def test_set_pin_revokes_sessions_and_unknown_phone_costs_the_same(reg):
    token, p, _ = reg.authenticate("03001234567", "2580")
    reg.set_pin(p.user_id, "9137")
    with pytest.raises(AuthError):
        reg.resolve(token)
    with pytest.raises(AuthError):
        reg.authenticate("03001234567", "2580")
    assert reg.authenticate("03001234567", "9137")[0]
    with pytest.raises(AuthError):
        reg.authenticate("03009999999", "9137")        # unknown phone: same error text


def test_same_phone_two_businesses_and_switch(reg):
    b2 = reg.create_business("Second Traders", "Karachi")
    reg.create_user(b2["business_id"], "Ali", "03001234567", "clerk", "2580")
    token, p, others = reg.authenticate("03001234567", "2580")
    assert p.role == "owner" and [o["name"] for o in others] == ["Second Traders"]
    t2, p2 = reg.switch_business(p, b2["business_id"])
    assert p2.role == "clerk" and reg.resolve(t2).business_id == b2["business_id"]
    with pytest.raises(AuthError):
        reg.switch_business(p, "B-NOPE")


def test_deactivated_user_and_business(reg):
    bid = reg.list_businesses()[0]["business_id"]
    reg.create_user(bid, "Driver", "03007777777", "driver", "4826")
    token, p, _ = reg.authenticate("03007777777", "4826")
    reg.update_user(p.user_id, active=False)
    with pytest.raises(AuthError):
        reg.resolve(token)
    with pytest.raises(AuthError):
        reg.authenticate("03007777777", "4826")
    reg.update_business(bid, active=0)
    with pytest.raises(AuthError):
        reg.authenticate("03001234567", "2580")


def test_duplicate_phone_in_one_business_refused(reg):
    bid = reg.list_businesses()[0]["business_id"]
    with pytest.raises(ValueError):
        reg.create_user(bid, "Twin", "0300-1234567", "clerk", "4826")


def test_permission_matrix_is_coherent():
    p = Principal("u", "b", "driver", "D")
    assert p.can("stops:close") and not p.can("staff:manage")
    assert roles_with("staff:manage") == ["owner"]
    assert set(roles_with("chat")) == {"owner", "clerk", "salesman", "driver"}
    for perm, roles in PERMISSIONS.items():
        assert "owner" in roles, f"the owner must hold every permission ({perm})"
    with pytest.raises(ValueError):
        p.can("teleport")


def test_plan_user_limit(reg):
    bid = reg.list_businesses()[0]["business_id"]
    for i in range(5):
        reg.create_user(bid, f"U{i}", f"0300-100000{i}", "driver", "2580")
    with pytest.raises(ValueError):
        reg.create_user(bid, "One more", "0300-1000099", "driver", "2580")
    reg.update_business(bid, plan="distributor")
    assert reg.create_user(bid, "One more", "0300-1000099", "driver", "2580")["role"] == "driver"
