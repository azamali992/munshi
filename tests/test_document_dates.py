"""Printed documents show the Pakistan business date, not the UTC date prefix of the stored timestamp:
a receipt issued at 02:30 Pakistan time (21:30 UTC the day before) is dated the Pakistan day."""
from __future__ import annotations

from munshi.documents.invoice import invoice_data, render_html
from munshi.domain.seed import seeded_repository


def test_a_receipt_issued_after_midnight_pakistan_carries_the_pakistan_date():
    repo = seeded_repository()
    e = repo.record_payment("C-002", 5000, "cash", "test", "hisaab_munshi")
    d = invoice_data(repo, e.entry_id)
    d["entry"] = d["entry"] | {"created_at": "2026-09-25T21:30:00+00:00"}      # 02:30 PKT on 26 Sep
    page = render_html(d)
    assert "2026-09-26" in page and "2026-09-25" not in page
