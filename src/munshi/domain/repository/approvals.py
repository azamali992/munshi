"""Pending approvals live in the business file, not in process memory, so a
restart never loses an action that was waiting for a human."""
from __future__ import annotations

import json

from munshi.domain.models import now_iso
from munshi.domain.repository.base import NotFoundError
from munshi.domain.repository.reports import ReportsMixin


class ApprovalsMixin(ReportsMixin):
    def save_approval(self, a: dict) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (a["approval_id"], a["thread_id"], a["specialist"], a["tool"], json.dumps(a["args"], default=str), a["tier"], a["needs_role"],
                       a["requested_by_role"], a.get("requested_by", ""), "pending", None, "", a["created_at"], None))

    def get_approval(self, approval_id: str) -> dict:
        r = self._one("SELECT * FROM approvals WHERE approval_id=?", (approval_id,))
        if not r: raise NotFoundError(f"no such approval: {approval_id}")
        d = dict(r); d["args"] = json.loads(d["args"]); return d

    def pending_approvals(self, thread_id: str | None = None) -> list[dict]:
        rows = self._all("SELECT * FROM approvals WHERE status='pending' AND thread_id=? ORDER BY created_at", (thread_id,)) if thread_id else self._all("SELECT * FROM approvals WHERE status='pending' ORDER BY created_at")
        return [dict(r) | {"args": json.loads(r["args"])} for r in rows]

    def resolve_approval(self, approval_id: str, approved: bool, by: str, note: str = "") -> None:
        with self._tx() as c:
            c.execute("UPDATE approvals SET status=?, resolved_by=?, note=?, resolved_at=? WHERE approval_id=?",
                      ("approved" if approved else "rejected", by, note, now_iso(), approval_id))

    def approval_history(self, limit: int = 100) -> list[dict]:
        return [dict(r) | {"args": json.loads(r["args"])} for r in self._all("SELECT * FROM approvals WHERE status!='pending' ORDER BY resolved_at DESC LIMIT ?", (limit,))]
