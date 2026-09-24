"""Gapless, sequential document numbers (the FBR e-invoicing direction needs them).

Format: <PREFIX>-<YYYY>-<NNNNNN>, e.g. INV-2026-000001. One series per document
kind; each series restarts at 000001 every calendar year, the year being the
Pakistan (Asia/Karachi) business year the document is dated in. Six digits is
a display width, not a cap: the 1,000,000th document of a year prints with
seven.

Why gapless holds: the counter row is incremented by the same write
transaction that inserts the document (callers hold immediate_tx / BEGIN
IMMEDIATE, so no other writer - thread or process - can interleave). If that
transaction fails for any reason the increment rolls back with it, so a number
is never burnt, and two creates can never read the same value.

From V5 on, a new document's primary key (ledger.entry_id, purchases.purchase_id)
IS its number, so every existing surface (app, WhatsApp text, public links,
exports) shows the gapless number without change; `doc_no` holds the same
value as the explicit compliance field. Pre-V5 rows keep their random ids and
have doc_no NULL."""
from __future__ import annotations

import sqlite3

from munshi.domain.models import to_business_date
from munshi.domain.repository.base import StateError

# series -> fixed prefix (None: the business's configurable invoice_prefix setting)
DOC_SERIES: dict[str, str | None] = {
    "invoice": None,             # sales invoice, raised when a delivery stop closes (or by hand)
    "receipt": "RCP",            # money received from a customer
    "credit_note": "CRN",        # credit to a customer's khata
    "opening": "OPB",            # a customer's opening balance brought in from the paper khata (not a sale)
    "reversal": "REV",           # a reversing entry on the customer khata (e.g. bounced cheque)
    "purchase": "PUR",           # goods received from a supplier
    "purchase_return": "PRN",    # reversal of a purchase (goods and bill go back)
}
RESERVED_PREFIXES = {p for p in DOC_SERIES.values() if p}


def next_doc_no(repo, cur: sqlite3.Cursor, series: str, created_at: str) -> str:
    """Take the next number in `series` for a document dated `created_at` (UTC ISO).

    Must be called inside the write transaction that inserts the document."""
    if series not in DOC_SERIES:
        raise ValueError(f"unknown document series {series!r}")
    if not repo._conn.in_transaction:
        raise RuntimeError("a document number must be taken inside the transaction that creates the document")
    prefix = DOC_SERIES[series]
    if prefix is None:
        prefix = (repo.setting("invoice_prefix") or "INV").strip().upper() or "INV"
        if prefix in RESERVED_PREFIXES:
            raise StateError(f"invoice prefix {prefix!r} is used by another document series; choose a different one in Settings")
    period = str(to_business_date(created_at).year)
    cur.execute("INSERT INTO document_counters (kind, period, last_value) VALUES (?, ?, 1) "
                "ON CONFLICT(kind, period) DO UPDATE SET last_value = last_value + 1", (series, period))
    n = cur.execute("SELECT last_value FROM document_counters WHERE kind=? AND period=?", (series, period)).fetchone()[0]
    return f"{prefix}-{period}-{int(n):06d}"
