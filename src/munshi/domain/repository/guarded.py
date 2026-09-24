"""Write-serialised transactions and the stop-close idempotency record.

`immediate_tx` takes SQLite's write lock up front (BEGIN IMMEDIATE) while
holding the repository's re-entrant lock, so a read-check-write sequence
inside it cannot interleave with another thread on this connection or with
another process on the same database file. Nested `_tx()` calls made inside
it (audit, move_stock, notify) join the same transaction.

`stop_closes` is the durable idempotency record for delivery-stop closes:
one row per closed stop (PRIMARY KEY stop_id — a second close of the same
stop cannot be recorded even if the status guard were bypassed) and the
client's idempotency key (UNIQUE client_ref — a key belongs to one close).
The table itself is created by migration V4 (domain/migrations.py), which
every repository connection applies on construction.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def immediate_tx(repo) -> Iterator[sqlite3.Cursor]:
    """Like RepositoryBase._tx, but acquires the database write lock before the first read."""
    with repo._lock:
        conn: sqlite3.Connection = repo._conn
        cur = conn.cursor()
        if conn.in_transaction:          # already inside a caller's transaction: join it
            yield cur
            return
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def request_hash(delivered: dict[str, int], returned: dict[str, int], cash: float) -> str:
    """Fingerprint of what a close request would post; an idempotent retry must match it exactly."""
    body = json.dumps({"d": sorted(delivered.items()), "r": sorted(returned.items()), "c": round(float(cash), 2)}, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()
