"""Connection, transactions, ids, audit and settings — the plumbing every
bounded context in this package builds on. SQLite via the standard
library; one connection per business file, guarded by a re-entrant lock so
the web server's worker threads never interleave a transaction."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from munshi.domain.migrations import migrate
from munshi.domain.models import AuditRow, Notification, now_iso


class NotFoundError(Exception): ...
class InsufficientStockError(Exception): ...
class CapacityError(Exception): ...
class CreditHoldError(Exception): ...
class OtpError(Exception): ...
class StateError(Exception): ...


DOMAIN_ERRORS = (NotFoundError, InsufficientStockError, CapacityError, CreditHoldError, OtpError, StateError, ValueError, KeyError)

DEFAULT_SETTINGS = {
    "business_name": "My Business",
    "city": "",
    "phone": "",
    "currency": "Rs",
    "credit_days": "30",
    "invoice_prefix": "INV",
    "language": "en",
    "digest_time": "20:00",
    "owner_phone": "",
    "default_warehouse": "",
    "big_order_limit": "500000",   # orders above this need the owner even for a clerk
}


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


class RepositoryBase:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            migrate(self._conn)

    # ------------------------------------------------------------ plumbing
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        """One transaction. Re-entrant: an inner _tx inside an outer one joins it."""
        with self._lock:
            outer = not self._conn.in_transaction
            cur = self._conn.cursor()
            if outer:
                cur.execute("BEGIN")
            try:
                yield cur
                if outer:
                    self._conn.commit()
            except Exception:
                if outer:
                    self._conn.rollback()
                raise

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ settings
    def setting(self, key: str, default: str | None = None) -> str:
        r = self._one("SELECT value FROM settings WHERE key=?", (key,))
        if r: return r["value"]
        return DEFAULT_SETTINGS.get(key, default) if default is None else default

    def set_setting(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))

    def settings(self) -> dict[str, str]:
        out = dict(DEFAULT_SETTINGS)
        out.update({r["key"]: r["value"] for r in self._all("SELECT key, value FROM settings")})
        return out

    @property
    def business_name(self) -> str:
        return self.setting("business_name")

    # ------------------------------------------------------------ audit
    def audit(self, actor: str, action: str, entity: str, entity_id: str, payload: dict,
              approved_by: str | None = None, user: str = "") -> AuditRow:
        row = AuditRow(new_id("AUD"), actor, action, entity, entity_id, approved_by, payload, user=user or self._current_user())
        with self._tx() as c:
            c.execute("INSERT INTO audit (audit_id, actor, action, entity, entity_id, approved_by, payload, created_at, user) VALUES (?,?,?,?,?,?,?,?,?)",
                      (row.audit_id, actor, action, entity, entity_id, approved_by, json.dumps(payload, default=str), row.created_at, row.user))
        return row

    def audit_log(self, limit: int = 50, entity_id: str | None = None) -> list[dict]:
        if entity_id:
            rows = self._all("SELECT * FROM audit WHERE entity_id=? ORDER BY created_at DESC LIMIT ?", (entity_id, limit))
        else:
            rows = self._all("SELECT * FROM audit ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    # The signed-in human behind the current operation, set by the platform /
    # web layer before each unit of work (the platform lock serialises work per
    # business, and agent tool calls may run on worker threads, so this is an
    # instance attribute rather than a thread-local). Every audit row written
    # during that work carries their name.
    _user: str = ""

    def set_current_user(self, user: str | None) -> None:
        self._user = user or ""

    def _current_user(self) -> str:
        return self._user

    # ------------------------------------------------------------ notifications
    def notify(self, for_role: str, kind: str, text: str, ref: str = "") -> Notification:
        n = Notification(new_id("NTF"), for_role, kind, text, ref)
        with self._tx() as c:
            c.execute("INSERT INTO notifications VALUES (?,?,?,?,?,?,?)", (n.notif_id, for_role, kind, text, ref, 0, n.created_at))
        return n

    def notifications(self, role: str, unread_only: bool = False, limit: int = 50) -> list[dict]:
        q = "SELECT * FROM notifications WHERE (for_role=? OR for_role='all')" + (" AND read=0" if unread_only else "") + " ORDER BY created_at DESC LIMIT ?"
        return [dict(r) | {"read": bool(r["read"])} for r in self._all(q, (role, limit))]

    def mark_notifications_read(self, role: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE notifications SET read=1 WHERE for_role=? OR for_role='all'", (role,))

    # ------------------------------------------------------------ outbox (messages to customers)
    def queue_message(self, channel: str, to_phone: str, text: str, ref: str = "") -> dict:
        m = {"msg_id": new_id("OUT"), "channel": channel, "to_phone": to_phone, "text": text, "status": "queued", "ref": ref, "created_at": now_iso(), "sent_at": None, "error": None}
        with self._tx() as c:
            c.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?)", tuple(m.values()))
        return m

    def mark_message(self, msg_id: str, status: str, error: str | None = None) -> None:
        with self._tx() as c:
            c.execute("UPDATE outbox SET status=?, sent_at=?, error=? WHERE msg_id=?", (status, now_iso() if status == "sent" else None, error, msg_id))

    def outbox(self, status: str | None = None, limit: int = 100) -> list[dict]:
        rows = self._all("SELECT * FROM outbox WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)) if status else self._all("SELECT * FROM outbox ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ chat log
    def add_chat(self, thread_id: str, role: str, text: str, meta: dict | None = None) -> dict:
        m = {"msg_id": new_id("MSG"), "thread_id": thread_id, "role": role, "text": text, "meta": meta or {}, "created_at": now_iso()}
        with self._tx() as c:
            c.execute("INSERT INTO chat VALUES (?,?,?,?,?,?)", (m["msg_id"], thread_id, role, text, json.dumps(m["meta"], default=str), m["created_at"]))
        return m

    def chat_history(self, thread_id: str, limit: int = 60) -> list[dict]:
        rows = self._all("SELECT * FROM chat WHERE thread_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?", (thread_id, limit))
        return [dict(r) | {"meta": json.loads(r["meta"])} for r in reversed(rows)]

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _j(v: Any) -> str:
        return json.dumps(v, default=str)
