"""The registry: which businesses exist, who works at each, and who is
signed in. One SQLite file for the whole installation; each business's
operational data lives in its own file (see tenancy/hub.py).

Security decisions, in one place:
- PINs are never stored. scrypt (n=2^14, r=8, p=1) with a 16-byte per-user salt.
- Sessions are 32 random bytes; only the SHA-256 of the token is stored, so a
  copy of the registry cannot be used to sign in.
- Five wrong PINs lock the user for 15 minutes (per user, not per device).
- Sessions expire after 30 days of inactivity; every request extends them.
- Weak PINs (0000, 1234, 1111, …) are refused outside demo data.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

from munshi.auth.principal import ROLES, Principal

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (business_id TEXT PRIMARY KEY, name TEXT, city TEXT, phone TEXT, plan TEXT, created_at TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, business_id TEXT, name TEXT, phone TEXT, role TEXT, pin_hash TEXT, salt TEXT,
    active INTEGER DEFAULT 1, failed_attempts INTEGER DEFAULT 0, locked_until TEXT, created_at TEXT, last_login TEXT,
    UNIQUE(business_id, phone));
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id TEXT, business_id TEXT, device TEXT, created_at TEXT, expires_at TEXT, last_seen TEXT);
CREATE INDEX IF NOT EXISTS ix_users_phone ON users(phone);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
"""

WEAK_PINS = {"0000", "1111", "2222", "3333", "4444", "5555", "6666", "7777", "8888", "9999", "1234", "4321", "0123", "1212",
             "000000", "111111", "123456", "654321", "112233"}
PLAN_USER_LIMITS = {"trial": 6, "munshi": 6, "distributor": 20, "self-hosted": None, "demo": 10}
LOCK_AFTER = 5
LOCK_MINUTES = 15
SESSION_DAYS = 30


class AuthError(Exception): ...
class LockedError(AuthError): ...


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def normalize_phone(phone: str) -> str:
    """Pakistani numbers in any of the usual spellings → 03XXXXXXXXX."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("92") and len(digits) == 12: digits = "0" + digits[2:]
    if len(digits) == 10 and digits.startswith("3"): digits = "0" + digits
    if not (len(digits) == 11 and digits.startswith("03")):
        raise ValueError("enter a mobile number like 0300-1234567")
    return digits


def validate_pin(pin: str, allow_weak: bool = False) -> str:
    pin = pin.strip()
    if not pin.isdigit() or not 4 <= len(pin) <= 6:
        raise ValueError("PIN must be 4 to 6 digits")
    if not allow_weak and (pin in WEAK_PINS or len(set(pin)) == 1):
        raise ValueError("that PIN is too easy to guess — pick another")
    return pin


def hash_pin(pin: str, salt: bytes) -> str:
    return hashlib.scrypt(pin.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


class Registry:
    def __init__(self, db_path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------ businesses
    def create_business(self, name: str, city: str = "", phone: str = "", plan: str = "trial", business_id: str | None = None) -> dict:
        name = name.strip()
        if len(name) < 2: raise ValueError("business name is too short")
        bid = business_id or ("B-" + secrets.token_hex(4).upper())
        with self._lock:
            self._conn.execute("INSERT INTO businesses VALUES (?,?,?,?,?,?,1)", (bid, name, city.strip(), phone.strip(), plan, _iso(_now())))
        return self.get_business(bid)

    def get_business(self, business_id: str) -> dict:
        r = self._conn.execute("SELECT * FROM businesses WHERE business_id=?", (business_id,)).fetchone()
        if not r: raise AuthError(f"no such business: {business_id}")
        return dict(r)

    def list_businesses(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM businesses ORDER BY created_at")]

    def update_business(self, business_id: str, **fields) -> dict:
        allowed = {k: v for k, v in fields.items() if k in ("name", "city", "phone", "plan", "active")}
        if allowed:
            sets = ", ".join(f"{k}=?" for k in allowed)
            with self._lock:
                self._conn.execute(f"UPDATE businesses SET {sets} WHERE business_id=?", (*allowed.values(), business_id))
        return self.get_business(business_id)

    # ------------------------------------------------------------ users
    def create_user(self, business_id: str, name: str, phone: str, role: str, pin: str, allow_weak_pin: bool = False, user_id: str | None = None) -> dict:
        if role not in ROLES: raise ValueError(f"role must be one of {', '.join(ROLES)}")
        name = name.strip()
        if len(name) < 2: raise ValueError("name is too short")
        phone = normalize_phone(phone)
        pin = validate_pin(pin, allow_weak_pin)
        biz = self.get_business(business_id)
        limit = PLAN_USER_LIMITS.get(biz["plan"], 6)
        if limit is not None and len(self.list_users(business_id, include_inactive=False)) >= limit:
            raise ValueError(f"the {biz['plan']} plan allows {limit} active users — deactivate someone or upgrade")
        salt = secrets.token_bytes(16)
        uid = user_id or ("U-" + secrets.token_hex(4).upper())
        with self._lock:
            if self._conn.execute("SELECT 1 FROM users WHERE business_id=? AND phone=?", (business_id, phone)).fetchone():
                raise ValueError("someone with that phone number already works here")
            self._conn.execute("INSERT INTO users (user_id, business_id, name, phone, role, pin_hash, salt, active, failed_attempts, locked_until, created_at, last_login) VALUES (?,?,?,?,?,?,?,1,0,NULL,?,NULL)",
                               (uid, business_id, name, phone, role, hash_pin(pin, salt), salt.hex(), _iso(_now())))
        return self.get_user(uid)

    def get_user(self, user_id: str) -> dict:
        r = self._conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not r: raise AuthError(f"no such user: {user_id}")
        return self._public(r)

    def list_users(self, business_id: str, include_inactive: bool = True) -> list[dict]:
        q = "SELECT * FROM users WHERE business_id=?" + ("" if include_inactive else " AND active=1") + " ORDER BY role, name"
        return [self._public(r) for r in self._conn.execute(q, (business_id,))]

    def update_user(self, user_id: str, *, name: str | None = None, role: str | None = None, active: bool | None = None, phone: str | None = None) -> dict:
        u = self.get_user(user_id)
        sets, vals = [], []
        if name is not None: sets.append("name=?"); vals.append(name.strip())
        if role is not None:
            if role not in ROLES: raise ValueError("bad role")
            sets.append("role=?"); vals.append(role)
        if phone is not None: sets.append("phone=?"); vals.append(normalize_phone(phone))
        if active is not None:
            sets.append("active=?"); vals.append(int(active))
        if sets:
            with self._lock:
                self._conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id=?", (*vals, user_id))
            if active is False:
                self.revoke_all(user_id)
        return self.get_user(u["user_id"])

    def set_pin(self, user_id: str, new_pin: str, allow_weak: bool = False) -> None:
        new_pin = validate_pin(new_pin, allow_weak)
        salt = secrets.token_bytes(16)
        with self._lock:
            self._conn.execute("UPDATE users SET pin_hash=?, salt=?, failed_attempts=0, locked_until=NULL WHERE user_id=?", (hash_pin(new_pin, salt), salt.hex(), user_id))
        self.revoke_all(user_id)

    def owners_count(self, business_id: str) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM users WHERE business_id=? AND role='owner' AND active=1", (business_id,)).fetchone()[0])

    @staticmethod
    def _public(r) -> dict:
        d = dict(r); d.pop("pin_hash", None); d.pop("salt", None); d["active"] = bool(d["active"])
        d["locked"] = bool(d["locked_until"] and datetime.fromisoformat(d["locked_until"]) > _now())
        return d

    # ------------------------------------------------------------ sign in
    def authenticate(self, phone: str, pin: str, device: str = "") -> tuple[str, Principal, list[dict]]:
        """Returns (token, principal, other_businesses_for_this_phone). Never says which part was wrong."""
        try:
            phone = normalize_phone(phone)
        except ValueError:
            raise AuthError("wrong phone number or PIN")
        rows = self._conn.execute("SELECT u.*, b.active AS b_active FROM users u JOIN businesses b ON b.business_id=u.business_id WHERE u.phone=? AND u.active=1 ORDER BY u.created_at", (phone,)).fetchall()
        if not rows:
            hash_pin(pin, b"x" * 16)     # same cost whether or not the phone exists
            raise AuthError("wrong phone number or PIN")
        matched = None
        for r in rows:
            if r["locked_until"] and datetime.fromisoformat(r["locked_until"]) > _now():
                raise LockedError("too many wrong PINs — try again in a few minutes")
            if not r["b_active"]:
                continue
            if secrets.compare_digest(hash_pin(pin, bytes.fromhex(r["salt"])), r["pin_hash"]):
                matched = r; break
        if matched is None:
            with self._lock:
                for r in rows:
                    n = r["failed_attempts"] + 1
                    lock = _iso(_now() + timedelta(minutes=LOCK_MINUTES)) if n >= LOCK_AFTER else None
                    self._conn.execute("UPDATE users SET failed_attempts=?, locked_until=? WHERE user_id=?", (0 if lock else n, lock, r["user_id"]))
            raise AuthError("wrong phone number or PIN")
        with self._lock:
            self._conn.execute("UPDATE users SET failed_attempts=0, locked_until=NULL, last_login=? WHERE user_id=?", (_iso(_now()), matched["user_id"]))
        token = self._issue(matched["user_id"], matched["business_id"], device)
        principal = Principal(matched["user_id"], matched["business_id"], matched["role"], matched["name"], phone)
        others = [self.get_business(r["business_id"]) for r in rows if r["user_id"] != matched["user_id"] and r["b_active"]]
        return token, principal, others

    def switch_business(self, principal: Principal, business_id: str, device: str = "") -> tuple[str, Principal]:
        r = self._conn.execute("SELECT * FROM users WHERE phone=? AND business_id=? AND active=1", (principal.phone, business_id)).fetchone()
        if not r: raise AuthError("you don't work at that business")
        token = self._issue(r["user_id"], business_id, device)
        return token, Principal(r["user_id"], business_id, r["role"], r["name"], principal.phone)

    def _issue(self, user_id: str, business_id: str, device: str) -> str:
        token = "ms_" + secrets.token_urlsafe(32)
        now = _now()
        with self._lock:
            self._conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                               (self._h(token), user_id, business_id, device[:80], _iso(now), _iso(now + timedelta(days=SESSION_DAYS)), _iso(now)))
        return token

    def resolve(self, token: str | None) -> Principal:
        if not token or not token.startswith("ms_"):
            raise AuthError("sign in with your phone and PIN")
        r = self._conn.execute("SELECT s.*, u.role, u.name, u.phone, u.active AS u_active, b.active AS b_active FROM sessions s JOIN users u ON u.user_id=s.user_id JOIN businesses b ON b.business_id=s.business_id WHERE s.token_hash=?", (self._h(token),)).fetchone()
        if not r or not r["u_active"] or not r["b_active"]:
            raise AuthError("session ended — sign in again")
        if datetime.fromisoformat(r["expires_at"]) < _now():
            self.revoke(token)
            raise AuthError("session expired — sign in again")
        now = _now()
        if (now - datetime.fromisoformat(r["last_seen"])).total_seconds() > 300:   # slide the expiry, at most every 5 min
            with self._lock:
                self._conn.execute("UPDATE sessions SET last_seen=?, expires_at=? WHERE token_hash=?", (_iso(now), _iso(now + timedelta(days=SESSION_DAYS)), r["token_hash"]))
        return Principal(r["user_id"], r["business_id"], r["role"], r["name"], r["phone"])

    def revoke(self, token: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token_hash=?", (self._h(token),))

    def revoke_all(self, user_id: str) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return cur.rowcount

    def sessions_for(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT device, created_at, last_seen, expires_at FROM sessions WHERE user_id=? ORDER BY last_seen DESC", (user_id,))]

    def purge_expired(self) -> int:
        with self._lock:
            return self._conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(_now()),)).rowcount

    @staticmethod
    def _h(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
