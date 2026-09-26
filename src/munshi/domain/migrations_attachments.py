"""Migration V10: payment proofs -- a photo or PDF (bank-transfer screenshot, JazzCash / Easypaisa receipt, cheque
photo) attached in chat or a form and linked to the money entry it proves. OWNED BY STREAM E.

Registration pattern: exactly as documented in migrations_finance.py -- `STEP` stays None until the schema is
written, then `STEP = v10`. V10 is registered only after V8 and V9 (contiguity). V10 does not depend on their tables,
so while they are unregistered Stream E's tests apply it by hand, unstamped:
    from munshi.domain.migrations import run_step_unstamped; run_step_unstamped(repo._conn, v10)

Suggested schema (Stream E decides; constraints from domain/accounts.py):
  attachments (att_id TEXT PRIMARY KEY  -- 'ATT-XXXXXXXX' (accounts.ID_PREFIX['attachment']), random: not a document
               sha256 TEXT NOT NULL, content_type TEXT NOT NULL CHECK (content_type IN accounts.ATTACHMENT_CONTENT_TYPES),
               size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 1 AND accounts.ATTACHMENT_MAX_BYTES),
               filename TEXT NOT NULL DEFAULT '' (display only, sanitised; never used as a path),
               data BLOB NOT NULL      -- in the tenant file: backups, export and tenancy isolation cover it for free
               uploaded_by TEXT NOT NULL (registry user_id), uploaded_by_name TEXT, uploaded_at TEXT NOT NULL)
  attachment_links (att_id REFERENCES attachments, entity TEXT CHECK (entity IN accounts.ATTACHMENT_ENTITIES),
               entity_id TEXT NOT NULL, linked_by TEXT NOT NULL, linked_at TEXT NOT NULL, approval_id TEXT,
               PRIMARY KEY (att_id, entity, entity_id))
  Both append-only (triggers, the V5 wording). Unlinking, if ever needed, is a new row kind -- never a DELETE."""
from __future__ import annotations

import sqlite3
from typing import Callable

VERSION = 10


def v10(conn: sqlite3.Connection) -> None:
    raise NotImplementedError("V10 (payment proofs) is Stream E's; it is not registered until STEP is set")


STEP: Callable[[sqlite3.Connection], None] | None = None     # Stream E: STEP = v10
