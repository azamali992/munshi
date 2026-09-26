"""Each person's chat is their own, and salaries stay the owner's even in the approval history.

Before: every phone sent thread "main", so the whole business shared one conversation -- a clerk opening chat read
what the owner typed ("Bilal ki salary 1,20,000 kar do"), and the topic memory ("uska balance") crossed people.
And /api/approvals/history returned a payroll card's arguments to a clerk."""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_attachments import CLERK, OWNER, login


def _client():
    from munshi.web.app import build_app
    app = build_app(in_memory=True, demo=True, scheduler=False)
    return TestClient(app), app


def test_each_person_reads_only_their_own_conversation():
    client, _ = _client()
    owner, clerk = login(client, OWNER), login(client, CLERK)
    assert client.post("/api/chat", json={"thread_id": "main", "text": "Bilal ki salary slip"}, headers=owner).status_code == 200
    mine = [m["text"] for m in client.get("/api/chat/main", headers=owner).json()]
    assert "Bilal ki salary slip" in mine
    theirs = [m["text"] for m in client.get("/api/chat/main", headers=clerk).json()]
    assert "Bilal ki salary slip" not in theirs and theirs == []


def test_a_clerk_sees_a_payroll_decision_but_not_its_figures():
    client, app = _client()
    repo = app.state.hub.platform("demo").repo
    repo.save_approval({"approval_id": "APR-PAYX", "thread_id": "U-1.main", "specialist": "tankhwa", "tool": "give_staff_advance",
                        "args": {"employee_id": "E-001", "amount": 25000, "installment": 5000, "method": "bank"}, "tier": "high_risk",
                        "needs_role": "owner", "requested_by_role": "owner", "requested_by": "Sultan Ahmed", "created_at": "2026-09-26T10:00:00"})
    repo.resolve_approval("APR-PAYX", True, "Sultan Ahmed", note="Rs 25,000 for his wedding")
    row = next(r for r in client.get("/api/approvals/history", headers=login(client, CLERK)).json() if r["approval_id"] == "APR-PAYX")
    assert row["status"] == "approved" and row["args"]["employee_id"] == "E-001"
    assert "amount" not in row["args"] and "installment" not in row["args"] and "25" not in str(row["args"]) and row["note"] == ""
    own = next(r for r in client.get("/api/approvals/history", headers=login(client, OWNER)).json() if r["approval_id"] == "APR-PAYX")
    assert own["args"]["amount"] == 25000 and "wedding" in own["note"]
