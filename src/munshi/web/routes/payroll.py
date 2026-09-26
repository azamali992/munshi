"""Employees, app logins for employees, attendance, payroll runs, payslips, salary payments, staff advances,
statutory summaries and rates. OWNED BY STREAM A (plan §4.3, §9 Stream A REST list).

Permissions (auth/principal.py; owner decision 2 -- salaries are the owner's alone):
  employees:read (owner, clerk)    GET /api/employees[/{id}] -- NO pay field for a caller without payroll:read
  attendance:write (owner, clerk)  GET/PUT /api/payroll/{period}/attendance -- days / leave / OT / trips only
  payroll:read / payroll:write     everything with a rupee in it: owner only
  payroll:approve                  POST /api/payroll/{period}/approve, /api/payroll/runs/{id}/reverse
  staff:manage                     app logins: POST /api/employees/{id}/login, /login/reset-pin (PIN shown ONCE)
  payroll:self (every role)        GET /api/me/payslips and GET /api/payslips/{own slip} -- resolved from
                                   principal.user_id ONLY; someone else's slip is a 404, not a 403 (no enumeration)
Every body is a pydantic model (extra fields refused where a typo could drop data). Domain errors map through app.py's
handlers (StateError 409, NotFoundError 404, ValueError 400, PermissionError 403). Money-moving POSTs honour an
`Idempotency-Key` header (a retried request returns the first result; the same key with another body is a 409).

EMPLOYEE LOGIN PIN HANDLING (owner decision 5): the owner types a PIN or asks for a generated one. The registry keeps
only its scrypt hash; the business database, audit payloads ({"generated": true}), chat, outbox, approval cards and
logs never see it. A GENERATED PIN is returned exactly once in this response (`pin_once` + a ready WhatsApp text for a
client-side wa.me link) with Cache-Control: no-store; a typed PIN is never echoed. Either way the user must change it
at first sign-in (web/deps.py). Ending employment disables the login (all sessions end) and keeps the history."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from munshi.auth import AuthError
from munshi.auth.registry import generate_pin
from munshi.domain.repository import NotFoundError
from munshi.web.deps import Ctx, context, hub_of

router = APIRouter(prefix="/api", tags=["payroll"])

PERIOD = r"^\d{4}-(0[1-9]|1[0-2])$"
DATE = r"^\d{4}-\d{2}-\d{2}$"
METHOD = "^(cash|bank|jazzcash|easypaisa|cheque)$"
ROLE = "^(owner|clerk|salesman|driver)$"
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def payroll_context(permission: str):
    """context(permission) + payroll must be enabled on this business (V9 applied), else 503 -- never a 500."""
    base = context(permission)

    async def dep(c: Ctx = Depends(base)) -> Ctx:
        if not await run_in_threadpool(c.repo.payroll_ready):
            raise HTTPException(503, "payroll is not enabled on this business yet")
        return c
    return dep


def _limit(request: Request, c: Ctx, what: str) -> None:
    if not request.app.state.login_limiter.allow(f"{what}:{c.principal.user_id}"):
        raise HTTPException(429, "too many login changes — wait a minute")


# ============================================================================ bodies
class AppLoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(pattern=ROLE)
    phone: str | None = Field(default=None, min_length=7, max_length=20)
    pin: str | None = Field(default=None, min_length=4, max_length=6)
    generate: bool = False

    @model_validator(mode="after")
    def _one_way(self):
        if bool(self.pin) == bool(self.generate):
            raise ValueError("either type a PIN or ask for a generated one (generate: true), not both")
        return self


class PinResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pin: str | None = Field(default=None, min_length=4, max_length=6)
    generate: bool = False

    @model_validator(mode="after")
    def _one_way(self):
        if bool(self.pin) == bool(self.generate):
            raise ValueError("either type a PIN or ask for a generated one (generate: true), not both")
        return self


class _EmployeeFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_no: str | None = Field(default=None, min_length=1, max_length=12)
    aliases: list[str] | None = Field(default=None, max_length=10)
    father_name: str | None = Field(default=None, max_length=60)
    cnic: str | None = Field(default=None, max_length=15)
    phone: str | None = Field(default=None, max_length=20)
    date_of_birth: str | None = Field(default=None, pattern=DATE)
    designation: str | None = Field(default=None, max_length=40)
    role_hint: str | None = Field(default=None, pattern="^(clerk|salesman|driver|helper|loader|godown|guard|accountant|other)$")
    joined_on: str | None = Field(default=None, pattern=DATE)
    province: str | None = Field(default=None, pattern="^(punjab|sindh|kp|balochistan|ict)$")
    eobi_covered: bool | None = None
    eobi_no: str | None = Field(default=None, max_length=20)
    ss_covered: bool | None = None
    ss_no: str | None = Field(default=None, max_length=20)
    tax_mode: str | None = Field(default=None, pattern="^(auto|off)$")
    opening_tax_year: int | None = Field(default=None, ge=2020, le=2100)
    opening_ytd_taxable: float | None = Field(default=None, ge=0)
    opening_ytd_tax: float | None = Field(default=None, ge=0)
    pay_method: str | None = Field(default=None, pattern=METHOD)
    pay_account_id: str | None = Field(default=None, max_length=40)
    payee_ref: str | None = Field(default=None, max_length=40)
    default_vehicle_id: str | None = Field(default=None, max_length=20)
    route_ids: list[str] | None = Field(default=None, max_length=20)
    cash_allowed: bool | None = None                 # owner-only, audited exemption from the bank/wallet rule
    cash_allowed_note: str | None = Field(default=None, max_length=200)


class EmployeeIn(_EmployeeFields):
    name: str = Field(min_length=2, max_length=60)
    app_login: AppLoginIn | None = None


class EmployeePatch(_EmployeeFields):
    name: str | None = Field(default=None, min_length=2, max_length=60)


class ComponentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(default="", max_length=20)
    label: str = Field(min_length=2, max_length=40)
    side: Literal["earning", "deduction"] = "earning"
    calc: Literal["fixed", "per_day", "per_trip", "pct_basic"] = "fixed"
    amount: float = Field(default=0, ge=0)
    rate_pct: float = Field(default=0, ge=0, le=100)
    taxable: bool = True
    prorate: bool = False


class PayStructureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effective_from: str = Field(pattern=DATE)
    pay_basis: Literal["monthly", "daily"]
    basic: float = Field(default=0, ge=0)
    daily_rate: float = Field(default=0, ge=0)
    components: list[ComponentIn] = Field(default_factory=list, max_length=20)
    ot_eligible: bool = True


class CommissionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    basis: Literal["booked_sales", "route_collections", "booked_qty"]
    rate_pct: float = Field(default=0, ge=0, le=100)
    per_unit: float = Field(default=0, ge=0)
    sku: str | None = Field(default=None, max_length=40)
    min_basis: float = Field(default=0, ge=0)
    effective_from: str | None = Field(default=None, pattern=DATE)


class EndIn(BaseModel):
    left_on: str = Field(pattern=DATE)
    reason: str = Field(min_length=3, max_length=200)


class RehireIn(BaseModel):
    rejoined_on: str = Field(pattern=DATE)
    restore_login: bool = False                      # re-enable the old login with a NEW generated PIN (shown once)


class AttendanceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: str = Field(min_length=1, max_length=40)
    days_worked: float = Field(default=0, ge=0, le=31)
    unpaid_absent: float = Field(default=0, ge=0, le=31)
    annual_leave: float = Field(default=0, ge=0, le=31)
    casual_leave: float = Field(default=0, ge=0, le=31)
    sick_leave: float = Field(default=0, ge=0, le=31)
    ot_hours: float | None = Field(default=None, ge=0, le=400)
    ot_minutes: int | None = Field(default=None, ge=0, le=24000)
    restday_ot_minutes: int = Field(default=0, ge=0, le=24000)
    holiday_ot_minutes: int = Field(default=0, ge=0, le=24000)
    trips: int = Field(default=0, ge=0, le=1000)
    source: Literal["register", "whatsapp", "app", "import"] = "register"


class AttendanceIn(BaseModel):
    rows: list[AttendanceRow] = Field(min_length=1, max_length=500)


class AdjustmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: str = Field(min_length=1, max_length=40)
    code: Literal["bonus", "arrears", "other_earning", "fine", "loss_recovery", "other_deduction"]
    amount: float = Field(gt=0)
    note: str = Field(min_length=3, max_length=200)
    ref: str | None = Field(default=None, max_length=40)
    taxable: bool = True


class ApproveIn(BaseModel):
    fingerprint: str = Field(min_length=64, max_length=64)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


class PaymentLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slip_id: str | None = Field(default=None, max_length=40)
    employee_id: str | None = Field(default=None, max_length=40)
    amount: float | None = Field(default=None, gt=0)
    method: str | None = Field(default=None, pattern=METHOD)
    account_id: str | None = Field(default=None, max_length=40)
    ref: str | None = Field(default=None, max_length=60)
    paid_on: str | None = Field(default=None, pattern=DATE)

    @model_validator(mode="after")
    def _who(self):
        if not (self.slip_id or self.employee_id):
            raise ValueError("each payment names a slip_id or an employee_id")
        return self


class PayIn(BaseModel):
    payments: list[PaymentLine] = Field(min_length=1, max_length=500)


class AdvanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: str = Field(min_length=1, max_length=40)
    amount: float = Field(gt=0)
    method: str = Field(pattern=METHOD)
    account_id: str | None = Field(default=None, max_length=40)
    kind: Literal["advance", "loan"] = "advance"
    installment: float = Field(default=0, ge=0)
    start_period: str | None = Field(default=None, pattern=PERIOD)
    note: str = Field(default="", max_length=200)
    given_on: str | None = Field(default=None, pattern=DATE)


class RepayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: str = Field(min_length=1, max_length=40)
    amount: float = Field(gt=0)
    method: str = Field(pattern=METHOD)
    account_id: str | None = Field(default=None, max_length=40)
    paid_on: str | None = Field(default=None, pattern=DATE)


class StatutoryPaymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["eobi", "pessi", "sessi", "kpessi", "income_tax"]
    period: str = Path(pattern=PERIOD)
    amount: float = Field(gt=0)
    method: str = Field(pattern=METHOD)
    account_id: str | None = Field(default=None, max_length=40)
    challan_ref: str = Field(default="", max_length=40)
    paid_on: str | None = Field(default=None, pattern=DATE)


class RateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=3, max_length=40)
    jurisdiction: str = Field(default="pk", max_length=20)
    value: str | dict = Field(...)
    effective_from: str = Field(pattern=DATE)
    source: str = Field(min_length=3, max_length=200)
    source_url: str = Field(default="", max_length=300)
    verified_on: str | None = Field(default=None, pattern=DATE)
    grade: Literal["A", "B", "C", "D", "U"] = "B"
    note: str = Field(default="", max_length=300)


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payroll_profile: Literal["plc_2026", "legacy_1969"] | None = None
    payroll_province: Literal["punjab", "sindh", "kp", "balochistan", "ict"] | None = None
    eobi_registered: bool | None = None
    ss_registered: bool | None = None
    payroll_pay_day: int | None = Field(default=None, ge=1, le=28)
    payroll_tax_round_rupee: bool | None = None
    payroll_enabled: bool | None = None
    finance_start_date: str | None = Field(default=None, pattern=DATE)
    ss_above_ceiling: Literal["exclude", "cap"] | None = None


# ============================================================================ employees and logins
def _pretty_phone(p: str) -> str:
    return f"{p[:4]}-{p[4:]}" if len(p) == 11 else p


def _login_out(request: Request, user: dict, name: str, pin_once: str | None) -> dict:
    out = {"user_id": user["user_id"], "phone": user["phone"], "role": user["role"], "must_change_pin": True}
    if pin_once:           # generated: shown to the owner exactly once, never stored in clear anywhere
        app_url = str(request.base_url).rstrip("/")
        out["pin_once"] = pin_once
        out["whatsapp_text"] = (f"Assalam o alaikum {name.split()[0]}, Munshi app par aap ka login: {_pretty_phone(user['phone'])}, PIN {pin_once}. "
                                f"Pehli dafa sign-in par naya PIN banayein. {app_url}")
    return out


def _user_here(request: Request, c: Ctx, user_id: str) -> dict:
    try:
        u = hub_of(request).registry.get_user(user_id)
    except AuthError:
        raise HTTPException(404, "no such login") from None
    if u["business_id"] != c.principal.business_id:
        raise HTTPException(404, "no such login")
    return u


def _issue_login(request: Request, c: Ctx, name: str, phone: str | None, login: AppLoginIn, make_employee) -> tuple[dict, dict]:
    """Create the registry login FIRST (the step that can fail on a taken phone number or the plan's user limit), then
    the employee (make_employee() -> employee dict) and the link; if either fails the fresh login is deleted again.
    Returns (employee, the once-only login answer)."""
    reg = hub_of(request).registry
    phone = login.phone or phone
    if not phone:
        raise HTTPException(400, "a login needs the employee's mobile number")
    pin = generate_pin() if login.generate else login.pin
    user = reg.create_user(c.principal.business_id, name, phone, login.role, pin, must_change_pin=True)
    try:
        emp = make_employee()
        emp = c.repo.link_login(emp["employee_id"], user["user_id"], login.role, c.role, generated=login.generate)
    except Exception:
        reg.delete_new_user(user["user_id"])
        raise
    return emp, _login_out(request, user, name, pin if login.generate else None)


@router.get("/employees")
def list_employees(request: Request, status: str = Query(default="active", pattern="^(active|left|all)$"), c: Ctx = Depends(payroll_context("employees:read"))):
    if c.principal.can("staff:manage"):       # the owner's screen picks up logins made before payroll existed
        c.repo.sync_employees_from_users(hub_of(request).registry.list_users(c.principal.business_id), c.role)
    return c.repo.list_employees(status, include_pay=c.principal.can("payroll:read"))


@router.get("/employees/{employee_id}")
def get_employee(employee_id: str, c: Ctx = Depends(payroll_context("employees:read"))):
    pay = c.principal.can("payroll:read")
    out = c.repo.get_employee(employee_id, include_pay=pay)
    if pay:
        out["events"] = c.repo.employee_events(employee_id)
    return out


@router.post("/employees", status_code=201)
def add_employee(body: EmployeeIn, request: Request, response: Response, c: Ctx = Depends(payroll_context("payroll:write"))):
    response.headers.update(NO_STORE)
    if body.app_login and not c.principal.can("staff:manage"):
        raise HTTPException(403, "only the owner can create app logins")
    if body.app_login:
        _limit(request, c, "login")
    data = body.model_dump(exclude_unset=True, exclude={"app_login"})
    if not body.app_login:
        return {"employee": c.repo.add_employee(data, c.role, c.signature)}
    emp, login = _issue_login(request, c, body.name, body.phone, body.app_login, lambda: c.repo.add_employee(data, c.role, c.signature))
    return {"employee": emp, "login": login}


@router.patch("/employees/{employee_id}")
def update_employee(employee_id: str, body: EmployeePatch, request: Request, c: Ctx = Depends(payroll_context("payroll:write"))):
    changes = body.model_dump(exclude_unset=True)
    emp = c.repo.update_employee(employee_id, changes, c.role, c.signature)
    if "name" in changes and emp.get("user_id"):
        hub_of(request).registry.update_user(emp["user_id"], name=changes["name"])
    return emp


@router.post("/employees/{employee_id}/pay-structure", status_code=201)
def set_pay_structure(employee_id: str, body: PayStructureIn, c: Ctx = Depends(payroll_context("payroll:write"))):
    return c.repo.set_pay_structure(employee_id, body.effective_from, body.pay_basis, body.basic, body.daily_rate,
                                    [x.model_dump() for x in body.components], body.ot_eligible, c.role, c.signature)


@router.post("/employees/{employee_id}/commission-rules", status_code=201)
def set_commission_rule(employee_id: str, body: CommissionIn, c: Ctx = Depends(payroll_context("payroll:write"))):
    return c.repo.set_commission_rule(employee_id, body.basis, body.rate_pct, body.per_unit, body.sku, body.min_basis,
                                      body.effective_from, c.role, c.signature)


@router.post("/employees/{employee_id}/login", status_code=201)
def create_login(employee_id: str, body: AppLoginIn, request: Request, response: Response, c: Ctx = Depends(payroll_context("staff:manage"))):
    response.headers.update(NO_STORE)
    _limit(request, c, "login")
    emp = c.repo.get_employee(employee_id, include_pay=True)
    if emp["status"] != "active":
        raise HTTPException(409, f"{emp['name']} has left; rehire first")
    if emp["has_login"]:
        raise HTTPException(409, f"{emp['name']} already has an app login; reset the PIN instead")
    _, login = _issue_login(request, c, emp["name"], emp["phone"], body, lambda: emp)
    return {"employee_id": emp["employee_id"], "login": login}


@router.post("/employees/{employee_id}/login/reset-pin")
def reset_login_pin(employee_id: str, body: PinResetIn, request: Request, response: Response, c: Ctx = Depends(payroll_context("staff:manage"))):
    response.headers.update(NO_STORE)
    _limit(request, c, "login")
    emp = c.repo.get_employee(employee_id, include_pay=True)
    if not emp["user_id"]:
        raise HTTPException(409, f"{emp['name']} has no app login")
    if emp["status"] != "active":
        raise HTTPException(409, f"{emp['name']} has left; their login stays disabled")
    u = _user_here(request, c, emp["user_id"])
    pin = generate_pin() if body.generate else body.pin
    hub_of(request).registry.set_pin(u["user_id"], pin, must_change=True)       # ends every session of that user
    c.repo.login_event(u["user_id"], "pin_reset", c.role, {"generated": body.generate})
    return {"employee_id": emp["employee_id"], "login": _login_out(request, u, emp["name"], pin if body.generate else None), "sessions_ended": True}


@router.post("/employees/{employee_id}/end")
def end_employment(employee_id: str, body: EndIn, request: Request, c: Ctx = Depends(payroll_context("payroll:write"))):
    reg = hub_of(request).registry
    emp = c.repo.get_employee(employee_id, include_pay=True)
    u = _user_here(request, c, emp["user_id"]) if emp.get("user_id") else None
    if u and u["role"] == "owner" and u["active"] and reg.owners_count(c.principal.business_id) <= 1:
        raise HTTPException(409, "a business needs at least one active owner")
    res = c.repo.end_employment(employee_id, body.left_on, body.reason, c.role, c.signature)
    disabled = False
    if u and u["active"]:
        reg.update_user(u["user_id"], active=False)                               # revoke_all: every session ends now
        c.repo.login_event(u["user_id"], "login_disabled", c.role, {"reason": "employment ended"})
        disabled = True
    return {"employee": res["employee"], "login_disabled": disabled}


@router.post("/employees/{employee_id}/rehire")
def rehire_employee(employee_id: str, body: RehireIn, request: Request, response: Response, c: Ctx = Depends(payroll_context("payroll:write"))):
    response.headers.update(NO_STORE)
    if body.restore_login and not c.principal.can("staff:manage"):
        raise HTTPException(403, "only the owner can restore app logins")
    res = c.repo.rehire_employee(employee_id, body.rejoined_on, c.role, c.signature)
    out = {"employee": res["employee"]}
    if body.restore_login and res.get("user_id"):
        _limit(request, c, "login")
        reg = hub_of(request).registry
        u = _user_here(request, c, res["user_id"])
        reg.update_user(u["user_id"], active=True)
        pin = generate_pin()
        reg.set_pin(u["user_id"], pin, must_change=True)
        c.repo.login_event(u["user_id"], "login_enabled", c.role, {"reason": "rehired"})
        c.repo.login_event(u["user_id"], "pin_reset", c.role, {"generated": True})
        out["login"] = _login_out(request, u, res["employee"]["name"], pin)
    return out


# ============================================================================ the month
@router.get("/payroll/settings")
def payroll_settings(c: Ctx = Depends(payroll_context("payroll:read"))):
    return c.repo.payroll_settings() | {"profile_limits_note": "Under plc_2026 the Punjab Labour Code 2026 limits are refused, not warned."}


@router.put("/payroll/settings")
def set_payroll_settings(body: SettingsIn, c: Ctx = Depends(payroll_context("payroll:write"))):
    changes = {k: (int(v) if isinstance(v, bool) else v) for k, v in body.model_dump(exclude_none=True).items()}
    return c.repo.set_payroll_settings(changes, c.role, c.signature)


@router.get("/payroll/runs/{run_id}")
def payroll_run(run_id: str, c: Ctx = Depends(payroll_context("payroll:read"))):
    return c.repo.payroll_register(run_id=run_id)


@router.post("/payroll/runs/{run_id}/pay")
def pay_salaries(run_id: str, body: PayIn, idem: str | None = Header(default=None, alias="Idempotency-Key"), c: Ctx = Depends(payroll_context("payroll:write"))):
    pays = [p.model_dump(exclude_none=True) for p in body.payments]
    return c.repo.payroll_idempotent(idem, "pay_salaries", {"run_id": run_id, "payments": pays},
                                     lambda: c.repo.pay_salaries(run_id, pays, c.role, c.signature))


@router.post("/payroll/runs/{run_id}/reverse")
def reverse_payroll_run(run_id: str, body: ReasonIn, c: Ctx = Depends(payroll_context("payroll:approve"))):
    return c.repo.reverse_payroll_run(run_id, body.reason, c.role, c.signature)


@router.delete("/payroll/adjustments/{adj_id}")
def void_adjustment(adj_id: str, c: Ctx = Depends(payroll_context("payroll:write"))):
    return c.repo.void_payroll_adjustment(adj_id, c.role, c.signature)


@router.get("/payroll/{period}/attendance")
def get_attendance(period: str = Path(pattern=PERIOD), c: Ctx = Depends(payroll_context("attendance:write"))):
    return c.repo.attendance(period)


@router.put("/payroll/{period}/attendance")
def put_attendance(body: AttendanceIn, period: str = Path(pattern=PERIOD), c: Ctx = Depends(payroll_context("attendance:write"))):
    return c.repo.set_attendance(period, [r.model_dump() for r in body.rows], c.role, c.signature)


@router.get("/payroll/{period}/adjustments")
def list_adjustments(period: str = Path(pattern=PERIOD), c: Ctx = Depends(payroll_context("payroll:read"))):
    return c.repo.payroll_adjustments(period)


@router.post("/payroll/{period}/adjustments", status_code=201)
def add_adjustment(body: AdjustmentIn, period: str = Path(pattern=PERIOD), idem: str | None = Header(default=None, alias="Idempotency-Key"),
                   c: Ctx = Depends(payroll_context("payroll:write"))):
    return c.repo.payroll_idempotent(idem, "add_payroll_adjustment", {"period": period, **body.model_dump()},
                                     lambda: c.repo.add_payroll_adjustment(body.employee_id, period, body.code, body.amount, body.note, body.ref,
                                                                           body.taxable, c.role, c.signature))


@router.get("/payroll/{period}/preview")
def preview(period: str = Path(pattern=PERIOD), c: Ctx = Depends(payroll_context("payroll:read"))):
    return c.repo.preview_payroll(period)


@router.post("/payroll/{period}/approve")
def approve(body: ApproveIn, period: str = Path(pattern=PERIOD), c: Ctx = Depends(payroll_context("payroll:approve"))):
    return c.repo.approve_payroll(period, body.fingerprint, c.role, c.signature)


@router.get("/payroll/{period}/register")
def register(period: str = Path(pattern=PERIOD), c: Ctx = Depends(payroll_context("payroll:read"))):
    return c.repo.payroll_register(period=period)


@router.get("/payroll/{period}/statutory")
def statutory(period: str = Path(pattern=PERIOD), kind: str = Query(pattern="^(eobi|ss|income_tax)$"), c: Ctx = Depends(payroll_context("payroll:read"))):
    return c.repo.statutory_summary(period, kind)


@router.post("/salary-payments/{payment_id}/reverse")
def reverse_salary_payment(payment_id: str, body: ReasonIn, c: Ctx = Depends(payroll_context("payroll:write"))):
    return c.repo.reverse_salary_payment(payment_id, body.reason, c.role, c.signature)


# ============================================================================ payslips
@router.get("/payslips/{slip_id}")
def payslip(slip_id: str, format: str = Query(default="json", pattern="^(json|html|pdf)$"), c: Ctx = Depends(payroll_context("payroll:self"))):
    """The owner (payroll:read) sees any slip; anyone else only a slip of the employee linked to THEIR login."""
    if not c.principal.can("payroll:read"):
        try:
            owner = c.repo.payslip_owner(slip_id)
        except NotFoundError:
            owner = None
        if owner is None or owner != c.principal.user_id:
            raise HTTPException(404, "no such payslip")
    d = c.repo.payslip(slip_id=slip_id)
    if format == "json":
        from munshi.documents.payslip import whatsapp_text
        return d | {"whatsapp_text": whatsapp_text(d)}
    from munshi.documents.payslip import render_html, render_pdf
    if format == "html":
        return Response(render_html(d), media_type="text/html; charset=utf-8", headers=NO_STORE)
    return Response(render_pdf(d), media_type="application/pdf", headers=NO_STORE | {"Content-Disposition": f'inline; filename="{d["slip_id"]}.pdf"'})


@router.get("/me/payslips")
def my_payslips(limit: int = Query(default=12, ge=1, le=36), c: Ctx = Depends(payroll_context("payroll:self"))):
    return c.repo.my_payslips(c.principal.user_id, limit)


# ============================================================================ staff advances
@router.get("/staff-advances")
def staff_advances(employee_id: str | None = None, status: str = Query(default="open", pattern="^(open|all)$"), c: Ctx = Depends(payroll_context("payroll:read"))):
    return c.repo.staff_advances_report(employee_id, status)


@router.post("/staff-advances", status_code=201)
def give_advance(body: AdvanceIn, idem: str | None = Header(default=None, alias="Idempotency-Key"), c: Ctx = Depends(payroll_context("payroll:write"))):
    return c.repo.payroll_idempotent(idem, "give_staff_advance", body.model_dump(),
                                     lambda: c.repo.give_staff_advance(body.employee_id, body.amount, body.method, body.account_id, body.kind, body.installment,
                                                                       body.start_period, body.note, c.role, c.signature, given_on=body.given_on))


@router.post("/staff-advances/repay", status_code=201)
def repay_advance(body: RepayIn, idem: str | None = Header(default=None, alias="Idempotency-Key"), c: Ctx = Depends(payroll_context("payroll:write"))):
    return c.repo.payroll_idempotent(idem, "repay_staff_advance", body.model_dump(),
                                     lambda: c.repo.repay_staff_advance(body.employee_id, body.amount, body.method, body.account_id, c.role, c.signature,
                                                                        paid_on=body.paid_on))


@router.post("/staff-advances/{advance_id}/reverse")
def reverse_advance(advance_id: str, body: ReasonIn, c: Ctx = Depends(payroll_context("payroll:write"))):
    return c.repo.reverse_staff_advance(advance_id, body.reason, c.role, c.signature)


# ============================================================================ statutory
@router.post("/statutory-payments", status_code=201)
def statutory_payment(body: StatutoryPaymentIn, idem: str | None = Header(default=None, alias="Idempotency-Key"), c: Ctx = Depends(payroll_context("payroll:write"))):
    return c.repo.payroll_idempotent(idem, "record_statutory_payment", body.model_dump(),
                                     lambda: c.repo.record_statutory_payment(body.kind, body.period, body.amount, body.method, body.account_id,
                                                                             body.challan_ref, body.paid_on, c.role, c.signature))


@router.get("/statutory-rates")
def statutory_rates(on: str | None = Query(default=None, pattern=DATE), c: Ctx = Depends(payroll_context("payroll:read"))):
    return c.repo.statutory_rates_list(on)


@router.post("/statutory-rates", status_code=201)
def add_statutory_rate(body: RateIn, c: Ctx = Depends(payroll_context("payroll:write"))):
    import json
    value = json.dumps(body.value) if isinstance(body.value, dict) else body.value
    return c.repo.add_statutory_rate(body.key, body.jurisdiction, value, body.effective_from, body.source, body.source_url,
                                     body.verified_on or "", body.grade, body.note, c.role, c.signature)
