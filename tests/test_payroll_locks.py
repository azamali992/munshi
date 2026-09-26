"""The locks around payroll: the Punjab Labour Code 2026 refusals (owner decision 1), the owner's per-employee cash
exemption (decision 2 of the brief), salaries visible to the owner only -- every payroll route for every role
(decision 3) -- idempotent money writes, and the statutory figures the plan could not verify."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from munshi.auth.ratelimit import RateLimiter
from munshi.tenancy.hub import DEMO_BUSINESS_ID
from munshi.web.app import build_app
from munshi.web.routes import payroll as payroll_routes
from tests.test_payroll_support import enable_payroll

DEMO = {"owner": ("0300-0000001", "1111"), "clerk": ("0300-0000002", "2222"), "driver": ("0300-0000003", "3333"),
        "salesman": ("0300-0000004", "4444")}
ROLES = tuple(DEMO)


def _login(c, phone, pin):
    r = c.post("/api/session", json={"phone": phone, "pin": pin})
    assert r.status_code == 200, r.text
    return {"X-Session": r.json()["token"]}


@pytest.fixture(scope="module")
def world():
    """Demo business with payroll enabled; Rafiq (driver) and Imran (salesman) on monthly pay, September approved."""
    app = build_app(in_memory=True, demo=True, scheduler=False)
    app.state.login_limiter = RateLimiter(rate_per_minute=10_000, burst=10_000)
    c = TestClient(app)
    repo = app.state.hub.platform(DEMO_BUSINESS_ID).repo
    enable_payroll(repo)
    h = {role: _login(c, *cred) for role, cred in DEMO.items()}
    emps = {e["name"]: e for e in c.get("/api/employees", headers=h["owner"]).json()["employees"]}      # synced from the logins
    ids = {"rafiq": emps["Rafiq Masih"]["employee_id"], "imran": emps["Imran Khan"]["employee_id"], "bilal": emps["Bilal Hussain"]["employee_id"]}
    for who in ("rafiq", "imran"):
        c.patch(f"/api/employees/{ids[who]}", json={"joined_on": "2025-01-01", "pay_method": "bank", "pay_account_id": "ACC-HBL"}, headers=h["owner"])
        r = c.post(f"/api/employees/{ids[who]}/pay-structure", json={"effective_from": "2025-01-01", "pay_basis": "monthly", "basic": 40_000},
                   headers=h["owner"])
        assert r.status_code == 201, r.text
    r = c.put("/api/payroll/2026-08/attendance", json={"rows": [{"employee_id": ids["rafiq"], "days_worked": 26, "trips": 2},
                                                               {"employee_id": ids["imran"], "days_worked": 26}]}, headers=h["clerk"])
    assert r.status_code == 200, r.text
    fp = c.get("/api/payroll/2026-08/preview", headers=h["owner"]).json()["fingerprint"]
    reg = c.post("/api/payroll/2026-08/approve", json={"fingerprint": fp}, headers=h["owner"])
    assert reg.status_code == 200, reg.text
    reg = reg.json()
    slips = {s["employee_id"]: s["slip_id"] for s in reg["payslips"]}
    return {"c": c, "h": h, "ids": ids, "repo": repo, "run_id": reg["run"]["run_id"], "slips": slips, "app": app}


# ============================================================================ who may call what
OWNER_ONLY, OWNER_CLERK, EVERYONE = {"owner"}, {"owner", "clerk"}, set(ROLES)


def _matrix(w):
    i, run, s = w["ids"], w["run_id"], w["slips"]
    emp = i["rafiq"]
    return [
        ("GET", "/api/employees", "/api/employees", None, OWNER_CLERK),
        ("POST", "/api/employees", "/api/employees", {"name": "Nobody New"}, OWNER_ONLY),
        ("GET", "/api/employees/{employee_id}", f"/api/employees/{emp}", None, OWNER_CLERK),
        ("PATCH", "/api/employees/{employee_id}", f"/api/employees/{emp}", {"designation": "x"}, OWNER_ONLY),
        ("POST", "/api/employees/{employee_id}/pay-structure", f"/api/employees/{emp}/pay-structure",
         {"effective_from": "2026-10-01", "pay_basis": "monthly", "basic": 1}, OWNER_ONLY),
        ("POST", "/api/employees/{employee_id}/commission-rules", f"/api/employees/{emp}/commission-rules", {"basis": "booked_sales", "rate_pct": 50}, OWNER_ONLY),
        ("POST", "/api/employees/{employee_id}/login", f"/api/employees/{emp}/login", {"role": "owner", "generate": True}, OWNER_ONLY),
        ("POST", "/api/employees/{employee_id}/login/reset-pin", f"/api/employees/{emp}/login/reset-pin", {"generate": True}, OWNER_ONLY),
        ("POST", "/api/employees/{employee_id}/end", f"/api/employees/{emp}/end", {"left_on": "2026-09-01", "reason": "xyz"}, OWNER_ONLY),
        ("POST", "/api/employees/{employee_id}/rehire", f"/api/employees/{emp}/rehire", {"rejoined_on": "2026-09-01"}, OWNER_ONLY),
        ("GET", "/api/payroll/settings", "/api/payroll/settings", None, OWNER_ONLY),
        ("PUT", "/api/payroll/settings", "/api/payroll/settings", {"payroll_profile": "legacy_1969"}, OWNER_ONLY),
        ("GET", "/api/payroll/runs/{run_id}", f"/api/payroll/runs/{run}", None, OWNER_ONLY),
        ("POST", "/api/payroll/runs/{run_id}/pay", f"/api/payroll/runs/{run}/pay", {"payments": [{"employee_id": emp, "method": "cash"}]}, OWNER_ONLY),
        ("POST", "/api/payroll/runs/{run_id}/reverse", f"/api/payroll/runs/{run}/reverse", {"reason": "xyz"}, OWNER_ONLY),
        ("DELETE", "/api/payroll/adjustments/{adj_id}", "/api/payroll/adjustments/ADJ-NOPE", None, OWNER_ONLY),
        ("GET", "/api/payroll/{period}/attendance", "/api/payroll/2026-09/attendance", None, OWNER_CLERK),
        ("PUT", "/api/payroll/{period}/attendance", "/api/payroll/2026-09/attendance", {"rows": [{"employee_id": emp, "days_worked": 1}]}, OWNER_CLERK),
        ("GET", "/api/payroll/{period}/adjustments", "/api/payroll/2026-09/adjustments", None, OWNER_ONLY),
        ("POST", "/api/payroll/{period}/adjustments", "/api/payroll/2026-09/adjustments",
         {"employee_id": emp, "code": "bonus", "amount": 1, "note": "xyz"}, OWNER_ONLY),
        ("GET", "/api/payroll/{period}/preview", "/api/payroll/2026-09/preview", None, OWNER_ONLY),
        ("POST", "/api/payroll/{period}/approve", "/api/payroll/2026-09/approve", {"fingerprint": "0" * 64}, OWNER_ONLY),
        ("GET", "/api/payroll/{period}/register", "/api/payroll/2026-08/register", None, OWNER_ONLY),
        ("GET", "/api/payroll/{period}/statutory", "/api/payroll/2026-08/statutory?kind=eobi", None, OWNER_ONLY),
        ("POST", "/api/salary-payments/{payment_id}/reverse", "/api/salary-payments/SPM-NOPE/reverse", {"reason": "xyz"}, OWNER_ONLY),
        ("GET", "/api/payslips/{slip_id}", f"/api/payslips/{s[i['rafiq']]}", None, {"owner", "driver"}),     # Rafiq's own slip
        ("GET", "/api/me/payslips", "/api/me/payslips", None, EVERYONE),
        ("GET", "/api/staff-advances", "/api/staff-advances", None, OWNER_ONLY),
        ("POST", "/api/staff-advances", "/api/staff-advances", {"employee_id": emp, "amount": 1, "method": "bank"}, OWNER_ONLY),
        ("POST", "/api/staff-advances/repay", "/api/staff-advances/repay", {"employee_id": emp, "amount": 1, "method": "bank"}, OWNER_ONLY),
        ("POST", "/api/staff-advances/{advance_id}/reverse", "/api/staff-advances/ADV-NOPE/reverse", {"reason": "xyz"}, OWNER_ONLY),
        ("POST", "/api/statutory-payments", "/api/statutory-payments", {"kind": "eobi", "period": "2026-08", "amount": 1, "method": "bank"}, OWNER_ONLY),
        ("GET", "/api/statutory-rates", "/api/statutory-rates", None, OWNER_ONLY),
        ("POST", "/api/statutory-rates", "/api/statutory-rates",
         {"key": "eobi_wage_base", "value": "1", "effective_from": "2026-01-01", "source": "xyz"}, OWNER_ONLY),
    ]


def test_the_matrix_covers_every_payroll_route(world):
    routes = {(m, r.path) for r in payroll_routes.router.routes for m in r.methods}
    assert routes == {(m, p) for m, p, *_ in _matrix(world)}


@pytest.mark.parametrize("role", ["clerk", "salesman", "driver"])
def test_every_payroll_route_refuses_roles_without_the_permission(world, role):
    c, h = world["c"], world["h"]
    for method, path, url, body, allowed in _matrix(world):
        r = c.request(method, url, json=body, headers=h[role])
        if role in allowed:
            assert r.status_code not in (401, 403, 404), (role, method, path, r.status_code, r.text)
        elif path == "/api/payslips/{slip_id}":
            assert r.status_code == 404, (role, path, r.status_code)          # someone else's slip: not found, never "forbidden"
        else:
            assert r.status_code == 403, (role, method, path, r.status_code, r.text)


def test_owner_reads_everything(world):
    c, h = world["c"], world["h"]
    for method, path, url, body, allowed in _matrix(world):
        if method == "GET":
            assert c.get(url, headers=h["owner"]).status_code == 200, path


def test_clerk_sees_names_and_attendance_but_never_a_rupee(world):
    c, h, ids = world["c"], world["h"], world["ids"]
    emps = c.get("/api/employees", headers=h["clerk"]).json()
    # user_id + login (role/active/must_change_pin, never a PIN) are additive: the employees screen shows a clerk
    # the same "no login" / role badge an owner sees, with no pay in it.
    public = {"employee_id", "emp_no", "name", "designation", "role_hint", "status", "joined_on", "left_on", "phone", "aliases", "has_login", "user_id", "login"}
    assert emps["employees"] and all(set(e) == public for e in emps["employees"])
    assert all(col["kind"] != "money" for col in emps["table"]["columns"])
    one = c.get(f"/api/employees/{ids['rafiq']}", headers=h["clerk"]).json()
    assert set(one) == public and "40000" not in c.get(f"/api/employees/{ids['rafiq']}", headers=h["clerk"]).text
    if one["login"]:
        assert set(one["login"]) == {"user_id", "role", "phone", "active", "must_change_pin"}
    att = c.get("/api/payroll/2026-08/attendance", headers=h["clerk"]).json()
    assert att["locked"] and all(col["kind"] != "money" for col in att["table"]["columns"])
    assert "40000" not in c.get("/api/payroll/2026-08/attendance", headers=h["clerk"]).text
    # the owner's view of the same employee carries the pay terms
    assert c.get(f"/api/employees/{ids['rafiq']}", headers=h["owner"]).json()["pay_structure"]["basic"] == 40_000.0


def test_each_employee_sees_only_their_own_slips(world):
    c, h, ids, slips = world["c"], world["h"], world["ids"], world["slips"]
    mine = c.get("/api/me/payslips", headers=h["driver"]).json()
    assert [p["slip_id"] for p in mine["payslips"]] == [slips[ids["rafiq"]]]
    assert c.get("/api/me/payslips", headers=h["clerk"]).json()["payslips"] == []                  # Bilal has no slip
    for fmt, kind in (("json", "application/json"), ("html", "text/html"), ("pdf", "application/pdf")):
        r = c.get(f"/api/payslips/{slips[ids['rafiq']]}?format={fmt}", headers=h["driver"])
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind)
    assert c.get(f"/api/payslips/{slips[ids['imran']]}", headers=h["driver"]).status_code == 404    # Imran's
    assert c.get(f"/api/payslips/{slips[ids['rafiq']]}", headers=h["salesman"]).status_code == 404
    assert c.get("/api/payslips/PSL-2099-999999", headers=h["driver"]).status_code == 404
    body = c.get(f"/api/payslips/{slips[ids['rafiq']]}", headers=h["driver"]).json()
    assert body["meta"]["leave_balances"] and "does not give tax or legal advice" in body["meta"]["boundary"]


# ============================================================================ Punjab Labour Code 2026 refusals
def test_plc_refusals_and_the_owners_cash_exemption(world):
    c, h, ids, repo = world["c"], world["h"], world["ids"], world["repo"]
    O, imran = h["owner"], ids["imran"]
    assert c.get("/api/payroll/settings", headers=O).json()["payroll_profile"] == "plc_2026"
    adv = {"employee_id": imran, "amount": 10_000, "method": "bank", "account_id": "ACC-HBL", "installment": 5_000, "start_period": "2026-09",
           "given_on": "2026-09-10"}
    r = c.post("/api/staff-advances", json=adv | {"method": "cash", "account_id": None}, headers=O)
    assert r.status_code == 409 and "Punjab Labour Code 2026" in r.json()["detail"] and "Settings" in r.json()["detail"]
    r = c.post("/api/staff-advances", json=adv | {"amount": 130_000}, headers=O)
    assert r.status_code == 409 and "3 x the monthly minimum wage" in r.json()["detail"]            # 130,000 > 3 x 40,000
    r = c.post("/api/staff-advances", json=adv | {"installment": 9_000}, headers=O)
    assert r.status_code == 409 and "20% of pay" in r.json()["detail"]                              # 9,000 > 8,000
    first = c.post("/api/staff-advances", json=adv, headers=O | {"Idempotency-Key": "adv-imran-0001"})
    assert first.status_code == 201, first.text
    replay = c.post("/api/staff-advances", json=adv, headers=O | {"Idempotency-Key": "adv-imran-0001"})       # a double tap
    assert replay.json()["advance_id"] == first.json()["advance_id"] and replay.json()["replayed"] is True
    assert c.post("/api/staff-advances", json=adv | {"amount": 9_999}, headers=O | {"Idempotency-Key": "adv-imran-0001"}).status_code == 409
    assert len(repo._all("SELECT 1 FROM staff_advances WHERE employee_id=?", (imran,))) == 1
    r = c.post("/api/staff-advances", json=adv | {"amount": 2_000}, headers=O)
    assert r.status_code == 409 and "earlier advance" in r.json()["detail"]                           # one open advance at a time

    # a fine above 3% of the month's pay: the preview shows the problem and approval is refused
    rafiq = ids["rafiq"]
    c.put("/api/payroll/2026-09/attendance", json={"rows": [{"employee_id": rafiq, "days_worked": 26}, {"employee_id": imran, "days_worked": 26}]},
          headers=h["clerk"])
    fine = c.post("/api/payroll/2026-09/adjustments", json={"employee_id": rafiq, "code": "fine", "amount": 1_500,
                                                            "note": "late thrice, show-cause 2026-09-12"}, headers=O).json()
    pv = c.get("/api/payroll/2026-09/preview", headers=O).json()
    assert not pv["can_approve"] and any("exceed 3%" in e for e in pv["errors"])                       # 1,500 > 1,200
    assert c.post("/api/payroll/2026-09/approve", json={"fingerprint": pv["fingerprint"]}, headers=O).status_code == 409
    assert c.delete(f"/api/payroll/adjustments/{fine['adj_id']}", headers=O).json()["voided"] is True
    pv = c.get("/api/payroll/2026-09/preview", headers=O).json()
    assert pv["can_approve"], pv["errors"]
    assert {ln["code"]: ln["amount"] for e in pv["employees"] if e["employee_id"] == imran for ln in e["lines"]}["ADV"] == 5_000.0

    # salary in cash: refused for Rafiq until the owner marks him cash-allowed (a clerk cannot)
    reg = c.post("/api/payroll/2026-09/approve", json={"fingerprint": pv["fingerprint"]}, headers=O).json()
    run = reg["run"]["run_id"]
    r = c.post(f"/api/payroll/runs/{run}/pay", json={"payments": [{"employee_id": rafiq, "method": "cash", "paid_on": "2026-09-30"}]}, headers=O)
    assert r.status_code == 409 and "cash allowed" in r.json()["detail"]
    assert c.patch(f"/api/employees/{rafiq}", json={"cash_allowed": True, "cash_allowed_note": "no bank account"}, headers=h["clerk"]).status_code == 403
    assert c.patch(f"/api/employees/{rafiq}", json={"cash_allowed": True}, headers=O).status_code == 400          # a reason is required
    assert c.patch(f"/api/employees/{rafiq}", json={"cash_allowed": True, "cash_allowed_note": "no bank account yet"}, headers=O).status_code == 200
    paid = c.post(f"/api/payroll/runs/{run}/pay", json={"payments": [{"employee_id": rafiq, "method": "cash", "paid_on": "2026-09-30"}]},
                  headers=O | {"Idempotency-Key": "pay-sep-rafiq-01"})
    assert paid.status_code == 200 and paid.json()["payments"][0]["cash_exemption"] is True and paid.json()["payments"][0]["account_id"] == "CASH"
    again = c.post(f"/api/payroll/runs/{run}/pay", json={"payments": [{"employee_id": rafiq, "method": "cash", "paid_on": "2026-09-30"}]},
                   headers=O | {"Idempotency-Key": "pay-sep-rafiq-01"})
    assert again.json()["payments"][0]["payment_id"] == paid.json()["payments"][0]["payment_id"]                 # not paid twice
    slip = next(s for s in reg["payslips"] if s["employee_id"] == rafiq)["slip_id"]
    shown = c.get(f"/api/payslips/{slip}?format=html", headers=h["driver"]).text
    assert "Paid in cash with the owner&#x27;s exemption" in shown and "no bank account yet" in shown
    audit = [a for a in repo.audit_log(200) if a["action"] == "update_employee" and a["entity_id"] == rafiq]
    assert audit[0]["payload"]["cash_allowed"] == {"allowed": True, "reason": "no bank account yet"}
    assert "cash_exemption" in [e["kind"] for e in repo.employee_events(rafiq)]
    pay_audit = next(a for a in repo.audit_log(200) if a["action"] == "pay_salaries")
    assert pay_audit["payload"]["cash_exemption_used"] == [rafiq]

    # the legacy profile is a per-business switch: cash advances are then allowed (owner only)
    assert c.put("/api/payroll/settings", json={"payroll_profile": "legacy_1969"}, headers=h["clerk"]).status_code == 403
    assert c.put("/api/payroll/settings", json={"payroll_profile": "legacy_1969"}, headers=O).json()["payroll_profile"] == "legacy_1969"
    bilal = ids["bilal"]
    c.post(f"/api/employees/{bilal}/pay-structure", json={"effective_from": "2025-01-01", "pay_basis": "monthly", "basic": 60_000}, headers=O)
    assert c.post("/api/staff-advances", json={"employee_id": bilal, "amount": 500_000, "method": "cash"}, headers=O).status_code == 201
    c.put("/api/payroll/settings", json={"payroll_profile": "plc_2026"}, headers=O)


def test_unverified_statutory_figures_are_refused_until_the_owner_sets_them(world):
    c, h, ids, repo = world["c"], world["h"], world["ids"], world["repo"]
    O = h["owner"]
    rates = {(r["key"], r["jurisdiction"]): r for r in c.get("/api/statutory-rates", headers=O).json()["rates"]}
    assert rates[("ss_wage_ceiling", "punjab")]["needs_verify"] and rates[("ss_wage_ceiling", "punjab")]["value"] == "unknown"
    assert rates[("eobi_wage_base", "pk")]["needs_verify"] and rates[("eobi_wage_base", "pk")]["grade"] == "U"
    assert rates[("min_wage_monthly", "punjab")]["value"] == "4000000" and rates[("min_wage_monthly", "punjab")]["verified_on"] == "2026-09-26"
    assert all("does not give tax or legal advice" in r["boundary"] for r in rates.values())
    # PESSI on for Imran: the October preview refuses the line until the owner sets the ceiling
    c.put("/api/payroll/settings", json={"ss_registered": True}, headers=O)
    c.patch(f"/api/employees/{ids['imran']}", json={"ss_covered": True}, headers=O)
    c.put("/api/payroll/2026-10/attendance", json={"rows": [{"employee_id": ids["imran"], "days_worked": 26}]}, headers=h["clerk"])
    pv = c.get("/api/payroll/2026-10/preview", headers=O).json()
    assert any("ceiling is not set" in e for e in pv["errors"])
    r = c.post("/api/statutory-rates", json={"key": "ss_wage_ceiling", "jurisdiction": "punjab", "value": "5000000", "effective_from": "2026-07-01",
                                             "source": "PESSI circular seen by owner", "grade": "B"}, headers=O)
    assert r.status_code == 201 and r.json()["needs_verify"] is False
    pv = c.get("/api/payroll/2026-10/preview", headers=O).json()
    imran = next(e for e in pv["employees"] if e["employee_id"] == ids["imran"])
    assert not any("ceiling" in e for e in pv["errors"])
    assert {ln["code"]: ln["amount"] for ln in imran["lines"]}["SS_ER"] == 2_400.0          # 6% of 40,000
    # a mistyped tax table is refused
    bad = {"key": "salary_tax_slabs", "value": {"tax_year": 2028, "bands": [{"over": 0, "upto": 100, "base": 0, "rate_bp": 0},
                                                                             {"over": 100, "upto": None, "base": 5, "rate_bp": 100}]},
           "effective_from": "2027-07-01", "source": "typo test"}
    assert c.post("/api/statutory-rates", json=bad, headers=O).status_code == 400
    c.put("/api/payroll/settings", json={"ss_registered": False}, headers=O)


def test_payroll_routes_answer_503_where_payroll_is_not_enabled():
    from munshi.domain.repository import MunshiRepository
    if MunshiRepository().payroll_ready():
        pytest.skip("V9 is registered: every business has payroll")
    c = TestClient(build_app(in_memory=True, demo=True, scheduler=False))
    O = _login(c, *DEMO["owner"])
    assert c.get("/api/employees", headers=O).status_code == 503
    assert c.get("/api/me/payslips", headers=O).status_code == 503
