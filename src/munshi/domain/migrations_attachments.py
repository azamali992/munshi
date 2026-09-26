"""Migration V10: payment proofs -- a photo or PDF (bank-transfer screenshot, JazzCash / Easypaisa receipt, cheque
photo) attached in chat or a form and linked to the money entry it proves. OWNED BY STREAM E.

Registration pattern: exactly as documented in migrations_finance.py -- v10 is written; the lead sets `STEP = v10` at
merge, and migrations.extension_steps() registers V10 only once V8 and V9 are registered too (contiguity), so no file
is ever stamped 10 before 8 and 9 ran. V10 does not depend on their tables; until then tests apply it unstamped:
    from munshi.domain.migrations import run_step_unstamped; run_step_unstamped(repo._conn, v10)

Design (Stream E decided):
  * The BYTES are not in this database. They live in the business's own directory, next to its SQLite file
    (<data>/files/<business_id>/<yyyy>/<mm>/<random hex>.<ext>), never under a statically served path; the row keeps
    the relative stored name and the sha256 of exactly those bytes (checked on every read). A thumbnail (images only)
    sits beside it. Why not a BLOB: 5 MB photos would bloat every VACUUM INTO backup and the WAL; the trade-off is
    that nightly_backup (web/app.py, Stream 0) must copy <data>/files/<business_id>/ as well -- see the report.
  * attachments: one row per upload. `sha256` is of the STORED bytes (images are re-encoded: EXIF/GPS stripped);
    `source_sha256` is of what the client sent (dedupe: the same person sending the same bytes twice gets the same
    row back; another person's identical upload gets its own row but shares the stored file).
    `status` pending -> linked exactly once (entity, entity_id, linked_by, linked_at = the FIRST record it proves);
    every other column is immutable and rows are never deleted (triggers).
  * attachment_links: the append-only history of every link, one row per (attachment, record). A proof may prove more
    than one record ONLY when every such record comes from the same approved action (one bank transfer paying a batch
    of salaries: `approval_id` equal); a pending card is linked as entity 'approval' first. The repository enforces
    that; the table records it. Unlinking, if ever needed, is a new row kind -- never a DELETE."""
from __future__ import annotations

import sqlite3
from typing import Callable

VERSION = 10


def _sql() -> tuple[str, list[str]]:
    from munshi.domain import accounts
    types = ", ".join(f"'{t}'" for t in accounts.ATTACHMENT_CONTENT_TYPES)
    ents = ", ".join(f"'{e}'" for e in accounts.ATTACHMENT_ENTITIES)
    mx = int(accounts.ATTACHMENT_MAX_BYTES)
    tables = f"""
CREATE TABLE IF NOT EXISTS attachments (
 att_id TEXT PRIMARY KEY CHECK (att_id LIKE 'ATT-%'),
 sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
 source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
 stored_name TEXT NOT NULL CHECK (length(stored_name) BETWEEN 1 AND 80),
 thumb_name TEXT,
 filename TEXT NOT NULL DEFAULT '' CHECK (length(filename) <= 120),
 content_type TEXT NOT NULL CHECK (content_type IN ({types})),
 size_bytes INTEGER NOT NULL CHECK (typeof(size_bytes) = 'integer' AND size_bytes BETWEEN 1 AND {mx}),
 source_size INTEGER NOT NULL CHECK (typeof(source_size) = 'integer' AND source_size BETWEEN 1 AND {mx}),
 width INTEGER,
 height INTEGER,
 uploaded_by TEXT NOT NULL CHECK (length(uploaded_by) >= 1),
 uploaded_by_name TEXT NOT NULL DEFAULT '',
 uploaded_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'linked')),
 entity TEXT CHECK (entity IS NULL OR entity IN ({ents})),
 entity_id TEXT,
 linked_by TEXT,
 linked_at TEXT,
 CHECK ((status = 'pending' AND entity IS NULL AND entity_id IS NULL AND linked_by IS NULL AND linked_at IS NULL)
     OR (status = 'linked' AND entity IS NOT NULL AND entity_id IS NOT NULL AND linked_by IS NOT NULL AND linked_at IS NOT NULL)));
CREATE INDEX IF NOT EXISTS ix_attachments_source ON attachments(source_sha256, uploaded_by);
CREATE INDEX IF NOT EXISTS ix_attachments_sha ON attachments(sha256);
CREATE INDEX IF NOT EXISTS ix_attachments_uploader ON attachments(uploaded_by, uploaded_at);
CREATE INDEX IF NOT EXISTS ix_attachments_entity ON attachments(entity, entity_id);
CREATE TABLE IF NOT EXISTS attachment_links (
 link_id INTEGER PRIMARY KEY,
 att_id TEXT NOT NULL REFERENCES attachments(att_id),
 entity TEXT NOT NULL CHECK (entity IN ({ents})),
 entity_id TEXT NOT NULL CHECK (length(entity_id) BETWEEN 1 AND 80),
 approval_id TEXT,
 linked_by TEXT NOT NULL,
 linked_at TEXT NOT NULL,
 UNIQUE (att_id, entity, entity_id));
CREATE INDEX IF NOT EXISTS ix_attachment_links_entity ON attachment_links(entity, entity_id)
"""
    triggers = [
        """CREATE TRIGGER IF NOT EXISTS attachments_append_only_delete BEFORE DELETE ON attachments
           BEGIN SELECT RAISE(ABORT, 'attachments is append-only: a proof is never deleted'); END""",
        """CREATE TRIGGER IF NOT EXISTS attachments_append_only_update BEFORE UPDATE ON attachments
           WHEN NOT (OLD.status = 'pending' AND NEW.status = 'linked'
                     AND NEW.att_id IS OLD.att_id AND NEW.sha256 IS OLD.sha256 AND NEW.source_sha256 IS OLD.source_sha256
                     AND NEW.stored_name IS OLD.stored_name AND NEW.thumb_name IS OLD.thumb_name AND NEW.filename IS OLD.filename
                     AND NEW.content_type IS OLD.content_type AND NEW.size_bytes IS OLD.size_bytes
                     AND NEW.source_size IS OLD.source_size AND NEW.width IS OLD.width AND NEW.height IS OLD.height
                     AND NEW.uploaded_by IS OLD.uploaded_by AND NEW.uploaded_by_name IS OLD.uploaded_by_name
                     AND NEW.uploaded_at IS OLD.uploaded_at)
           BEGIN SELECT RAISE(ABORT, 'attachments is append-only (only pending -> linked, once)'); END""",
        """CREATE TRIGGER IF NOT EXISTS attachment_links_append_only_delete BEFORE DELETE ON attachment_links
           BEGIN SELECT RAISE(ABORT, 'attachment_links is append-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS attachment_links_append_only_update BEFORE UPDATE ON attachment_links
           BEGIN SELECT RAISE(ABORT, 'attachment_links is append-only'); END""",
    ]
    return tables, triggers


def v10(conn: sqlite3.Connection) -> None:
    # not at module level: migrations.py imports this module
    from munshi.domain.migrations import _run_sql
    tables, triggers = _sql()
    _run_sql(conn, tables)
    for stmt in triggers:           # trigger bodies contain ';': executed one by one
        conn.execute(stmt)


# v10 is WRITTEN. The lead sets `STEP = v10` at merge, after V8 and V9 are registered (SEAMS §6); extension_steps()
# would refuse to register it before them anyway (contiguity).
STEP: Callable[[sqlite3.Connection], None] | None = None
