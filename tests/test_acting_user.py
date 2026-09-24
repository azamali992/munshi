"""Who did this? Attribution of writes to the right human.

Two bugs shared one root cause: the repository kept "the current user" in a
single mutable attribute on the (shared, per-business) repository object.

Bug 1 (approve-then-resume): platform.resolve() set that slot to the
APPROVER just before resuming the gated tool, so an order Bilal asked for in
chat and Sana approved was stamped created_by = Sana, and the create_order
audit row said user = Sana. The direct routes' four-eyes check reads exactly
those two facts to decide who asked for the order, so afterwards Sana could
not confirm it and Bilal -- the real requester -- could.

Bug 2 (concurrency): direct API routes set the slot per request without the
platform lock, so concurrent requests overwrote each other's identity and
audit rows / created_by were stamped with another user's name.

The fix scopes the acting user per logical operation (a ContextVar, set for
exactly the duration of a request / chat turn / resumed tool call), runs a
resumed tool as the ORIGINAL requester, and records the approver separately
(approvals.resolved_by, the approval_granted audit row, and approved_by on the
resumed tool's own audit rows).

Demo staff: owner Sultan Ahmed, clerk Bilal Hussain, salesman Imran Khan.
"""
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from munshi.auth.ratelimit import RateLimiter
from munshi.domain.repository import MunshiRepository
from munshi.platform import MunshiPlatform
from munshi.web.app import build_app

DEMO = {"owner": ("0300-0000001", "1111"), "clerk": ("0300-0000002", "2222"), "salesman": ("0300-0000004", "4444")}
ORD = re.compile(r"ORD-[0-9A-F]+")


def _login(c, phone, pin):
    r = c.post("/api/session", json={"phone": phone, "pin": pin})
    assert r.status_code == 200, r.text
    return {"X-Session": r.json()["token"]}


def _add_staff(c, owner, name, phone, role):
    r = c.post("/api/staff", json={"name": name, "phone": phone, "role": role, "pin": "2580"}, headers=owner)
    assert r.status_code == 201, r.text
    return _login(c, phone, "2580")


@pytest.fixture
def api():
    app = build_app(in_memory=True, demo=True, scheduler=False)
    app.state.login_limiter = RateLimiter(rate_per_minute=10_000, burst=10_000)   # this file signs in a lot of staff
    c = TestClient(app)
    h = {role: _login(c, *cred) for role, cred in DEMO.items()}
    h["clerk2"] = _add_staff(c, h["owner"], "Sana Malik", "0301-2223334", "clerk")
    return c, h


def _order(c, h, oid):
    return c.get(f"/api/orders/{oid}", headers=h["owner"]).json()


def _create_row(audit, oid):
    return next(a for a in audit if a["action"] == "create_order" and a["entity_id"] == oid)


# ---------------------------------------------------------------- bug 1: the resumed tool runs as the requester
def test_chat_order_is_created_by_the_requester_not_the_approver():
    """The bug report's repro: a clerk asks in chat, the owner approves. Before the fix
    the order read created_by = the owner (the approver)."""
    p = MunshiPlatform()
    r = p.handle_message("t", "clerk", "Chaudhry Farms ko 2 urea bhej do", user="Bilal Hussain")
    assert r.pending and r.pending.tool == "create_order" and r.pending.requested_by == "Bilal Hussain"
    out = p.resolve(r.pending.approval_id, True, "owner", user="Sultan Ahmed")
    oid = ORD.search(out.text).group(0)

    assert p.repo.get_order(oid).created_by == "Bilal Hussain"               # requested by Bilal ...
    audit = p.repo.audit_log(100)
    row = _create_row(audit, oid)
    assert row["user"] == "Bilal Hussain"
    assert row["approved_by"] == "owner:Sultan Ahmed"                         # ... approved by Sultan, recorded separately
    granted = next(a for a in audit if a["action"] == "approval_granted" and a["entity_id"] == r.pending.approval_id)
    assert granted["user"] == "Sultan Ahmed"                                  # the decision itself is the approver's act
    hist = p.repo.approval_history()[0]
    assert hist["requested_by"] == "Bilal Hussain" and hist["resolved_by"] == "Sultan Ahmed"


def test_an_explicit_approval_signature_on_a_resumed_tool_is_kept():
    """A resumed tool's audit rows carry the named approver instead of the tool's bare-role default,
    but a value that is not a bare role (e.g. a customer's otp:NNNN) is never overwritten."""
    repo = MunshiRepository()
    with repo.acting_as("Bilal", approved_by="clerk:Sana"):
        assert repo.audit("x", "a", "e", "1", {}).approved_by == "clerk:Sana"
        assert repo.audit("x", "a", "e", "2", {}, approved_by="clerk").approved_by == "clerk:Sana"
        assert repo.audit("x", "a", "e", "3", {}, approved_by="otp:1234").approved_by == "otp:1234"
    assert repo.audit("x", "a", "e", "4", {}, approved_by="clerk").approved_by == "clerk"      # no approval in scope: as given


def test_requester_of_a_chat_order_is_still_blocked_from_confirming_or_allocating_it_directly(api):
    """The security half of the bug: Bilal asks in chat, Sana approves. Before the fix the order
    remembered Sana as its author, so Sana was refused and Bilal could confirm his own request."""
    c, h = api
    r = c.post("/api/chat", json={"thread_id": "b", "text": "Chaudhry Farms ko 2 urea bhej do"}, headers=h["clerk"]).json()
    assert r["pending"]["requested_by"] == "Bilal Hussain"
    d = c.post(f"/api/approvals/{r['pending']['approval_id']}", json={"approve": True}, headers=h["clerk2"])
    assert d.status_code == 200, d.text
    oid = ORD.search(d.json()["text"]).group(0)
    o = _order(c, h, oid)
    assert o["created_by"] == "Bilal Hussain"
    assert _create_row(o["audit"], oid)["user"] == "Bilal Hussain"

    own = c.post(f"/api/orders/{oid}/confirm", headers=h["clerk"])
    assert own.status_code == 403 and "someone else" in own.json()["detail"]
    assert _order(c, h, oid)["status"] == "draft"
    assert c.post(f"/api/orders/{oid}/confirm", headers=h["clerk2"]).json()["status"] == "confirmed"

    own = c.post(f"/api/orders/{oid}/allocate", json={}, headers=h["clerk"])
    assert own.status_code == 403 and "someone else" in own.json()["detail"]
    assert _order(c, h, oid)["status"] == "confirmed"


def test_solo_owner_still_approves_and_confirms_their_own_request(api):
    c, h = api
    r = c.post("/api/chat", json={"thread_id": "o", "text": "Chaudhry Farms ko 2 urea bhej do"}, headers=h["owner"]).json()
    assert r["pending"]["requested_by"] == "Sultan Ahmed"
    d = c.post(f"/api/approvals/{r['pending']['approval_id']}", json={"approve": True}, headers=h["owner"])
    assert d.status_code == 200, d.text
    oid = ORD.search(d.json()["text"]).group(0)
    o = _order(c, h, oid)
    assert o["created_by"] == "Sultan Ahmed"
    assert _create_row(o["audit"], oid)["approved_by"] == "owner:Sultan Ahmed"
    assert c.post(f"/api/orders/{oid}/confirm", headers=h["owner"]).json()["status"] == "confirmed"
    assert c.post(f"/api/orders/{oid}/allocate", json={}, headers=h["owner"]).json()["status"] == "allocated"


def test_a_rejected_card_still_resumes_as_the_requester():
    p = MunshiPlatform()
    r = p.handle_message("t", "clerk", "expense diesel 5000 for V-01", user="Bilal")
    p.resolve(r.pending.approval_id, False, "clerk", "typo", user="Sana")
    rejected = next(a for a in p.repo.audit_log(50) if a["action"] == "approval_rejected")
    assert rejected["user"] == "Sana" and rejected["approved_by"] == "clerk"


def test_identity_does_not_outlive_the_operation():
    """After a chat turn or a resume returns, the next un-attributed write on that thread is
    anonymous -- it doesn't silently inherit whoever spoke last (the old slot did)."""
    p = MunshiPlatform()
    r = p.handle_message("t", "clerk", "expense diesel 5000 for V-01", user="Bilal")
    assert p.repo.audit("system", "tick", "x", "1", {}).user == ""
    p.resolve(r.pending.approval_id, True, "clerk", user="Sana")
    assert p.repo.audit("system", "tick", "x", "2", {}).user == ""


def test_record_expense_paid_by_is_the_requester():
    p = MunshiPlatform()
    r = p.handle_message("t", "clerk", "expense diesel 5000 for V-01", user="Bilal")
    p.resolve(r.pending.approval_id, True, "clerk", user="Sana")
    e = p.repo.expenses_between("2000-01-01", "2999-01-01")
    assert any(x.paid_by == "Bilal" for x in e) and not any(x.paid_by == "Sana" for x in e)


# ---------------------------------------------------------------- bug 2: concurrency
def test_repository_identity_is_per_thread_not_shared():
    """The minimal repro of the shared slot: every thread names itself, waits for the others to
    do the same, then writes. With a shared attribute every row but one carries the last name set."""
    repo = MunshiRepository()
    n = 24
    gate = threading.Barrier(n)

    def work(i):
        repo.set_current_user(f"user-{i}")
        gate.wait()
        time.sleep(0.01)
        repo.audit("test", "tick", "thread", str(i), {})

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in threads: t.start()
    for t in threads: t.join()
    wrong = [a for a in repo.audit_log(100) if a["action"] == "tick" and a["user"] != f"user-{a['entity_id']}"]
    assert not wrong, f"{len(wrong)} of {n} audit rows attributed to another thread's user"


def test_concurrent_requests_and_chat_approvals_never_borrow_another_users_identity(api):
    """Real threads against one app/platform/repository: 48 direct order entries by 12 different
    people racing 12 salesmen's chat requests, then those 12 cards approved by 4 clerks racing 48
    more direct entries. Every created_by and every audit row must name its own actor."""
    c, h = api
    reg = c.app.state.hub.registry
    for b in reg.list_businesses():
        reg.update_business(b["business_id"], plan="self-hosted")                         # room for 12 more staff
    names = {}
    for i in range(6):
        names[f"s{i}"] = (f"Salesman {i}", _add_staff(c, h["owner"], f"Salesman {i}", f"0302-10000{i:02d}", "salesman"))
        names[f"k{i}"] = (f"Clerk {i}", _add_staff(c, h["owner"], f"Clerk {i}", f"0303-10000{i:02d}", "clerk"))
    requesters = [(f"Salesman {i % 6}", names[f"s{i % 6}"][1]) for i in range(12)]
    approvers = [names[f"k{i % 4}"] for i in range(12)]
    direct = [names[k] for k in sorted(names)] * 4                                          # 48 entries, 12 people
    customers = ["C-001", "C-003", "C-005", "C-008"]
    mistakes: list[str] = []
    lock = threading.Lock()

    def bad(msg):
        with lock: mistakes.append(msg)

    def direct_entry(i, who, hdr):
        r = c.post("/api/orders", json={"customer_id": customers[i % 4], "items": [{"sku": "UREA-50", "qty": 1}]}, headers=hdr)
        assert r.status_code == 201, r.text
        o = r.json()
        if o["created_by"] != who: bad(f"direct {o['order_id']}: created_by {o['created_by']!r}, expected {who!r}")
        row = _create_row(_order(c, h, o["order_id"])["audit"], o["order_id"])
        if row["user"] != who: bad(f"direct {o['order_id']}: audit user {row['user']!r}, expected {who!r}")

    cards: dict[int, str] = {}

    def chat_request(i, who, hdr):
        r = c.post("/api/chat", json={"thread_id": f"t{i}", "text": f"{['Malik Agro Store', 'Rana Brothers'][i % 2]} ko 1 urea bhej do"}, headers=hdr).json()
        assert r["pending"] and r["pending"]["tool"] == "create_order", r
        if r["pending"]["requested_by"] != who: bad(f"card {i}: requested_by {r['pending']['requested_by']!r}, expected {who!r}")
        cards[i] = r["pending"]["approval_id"]

    def approve(i, approver, hdr):
        d = c.post(f"/api/approvals/{cards[i]}", json={"approve": True}, headers=hdr)
        assert d.status_code == 200, d.text
        oid = ORD.search(d.json()["text"]).group(0)
        who = requesters[i][0]
        o = _order(c, h, oid)
        if o["created_by"] != who: bad(f"chat {oid}: created_by {o['created_by']!r}, expected requester {who!r} (approver {approver!r})")
        row = _create_row(o["audit"], oid)
        if row["user"] != who: bad(f"chat {oid}: audit user {row['user']!r}, expected requester {who!r}")
        if row["approved_by"] != f"clerk:{approver}": bad(f"chat {oid}: approved_by {row['approved_by']!r}, expected clerk:{approver}")
        granted = next(a for a in c.get(f"/api/audit?entity_id={cards[i]}", headers=h["owner"]).json() if a["action"] == "approval_granted")
        if granted["user"] != approver: bad(f"approval {cards[i]}: user {granted['user']!r}, expected approver {approver!r}")

    def race(jobs):
        gate = threading.Barrier(len(jobs))
        errors = []

        def run(fn, *a):
            try:
                gate.wait(); fn(*a)
            except Exception as e:           # noqa: BLE001 -- surfaced below with the thread's context
                errors.append(repr(e))
        threads = [threading.Thread(target=run, args=job) for job in jobs]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors, errors

    half = len(direct) // 2
    race([(direct_entry, i, *direct[i]) for i in range(half)] + [(chat_request, i, *requesters[i]) for i in range(12)])
    race([(direct_entry, half + i, *direct[half + i]) for i in range(half)] + [(approve, i, *approvers[i]) for i in range(12)])
    checked = 2 * len(direct) + 1 * 12 + 4 * 12
    assert not mistakes, f"{len(mistakes)} of {checked} attributions named the wrong person:\n" + "\n".join(mistakes[:20])
