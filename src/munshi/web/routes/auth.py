"""Sign-up, sign-in, sessions, the caller's own profile, and staff management."""
from __future__ import annotations

import ipaddress
import logging
import os
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from munshi.auth import ROLES, AuthError, LockedError, Principal
from munshi.auth.principal import PERMISSIONS, ROLE_TITLES
from munshi.tenancy.hub import DEMO_BUSINESS_ID, DEMO_USERS
from munshi.web.deps import Ctx, context, current, hub_of, require, token_of

router = APIRouter(prefix="/api", tags=["auth"])
log = logging.getLogger("munshi.web.auth")


class SignupIn(BaseModel):
    business_name: str = Field(min_length=2, max_length=80)
    city: str = Field(default="", max_length=60)
    owner_name: str = Field(min_length=2, max_length=60)
    phone: str = Field(min_length=7, max_length=20)
    pin: str = Field(min_length=4, max_length=6)
    sample_data: bool = False
    language: str = Field(default="en", pattern="^(en|ur)$")


class SessionIn(BaseModel):
    phone: str = Field(min_length=7, max_length=20)
    pin: str = Field(min_length=4, max_length=6)
    device: str = Field(default="", max_length=80)


class SwitchIn(BaseModel):
    business_id: str = Field(min_length=1, max_length=40)
    pin: str = Field(min_length=4, max_length=6)       # the PIN for the TARGET business


class PinChangeIn(BaseModel):
    old_pin: str = Field(min_length=4, max_length=6)
    new_pin: str = Field(min_length=4, max_length=6)


class StaffIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    phone: str = Field(min_length=7, max_length=20)
    role: str = Field(pattern="^(owner|clerk|salesman|driver)$")
    pin: str = Field(min_length=4, max_length=6)


class StaffPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=60)
    role: str | None = Field(default=None, pattern="^(owner|clerk|salesman|driver)$")
    active: bool | None = None


class StaffPin(BaseModel):
    pin: str = Field(min_length=4, max_length=6)


@lru_cache(maxsize=8)
def _trusted_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))    # a typo fails loudly, not open
    return tuple(nets)


def _is_trusted(host: str, nets) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip())
    except ValueError:
        return False
    return any(ip in n for n in nets)


def _client(request: Request) -> str:
    """The address the rate limiters key on.

    X-Forwarded-For is only believed when the connecting peer is one of
    MUNSHI_TRUSTED_PROXY_IPS (comma-separated IPs/CIDRs; empty = trust nobody).
    Then the RIGHTMOST hop that is not itself a trusted proxy is the client: every
    hop to the left of it was written by the client and proves nothing."""
    peer = request.client.host if request.client else "?"
    nets = _trusted_networks(os.environ.get("MUNSHI_TRUSTED_PROXY_IPS", ""))
    if not nets or not _is_trusted(peer, nets):
        return peer
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for hop in reversed(hops):
        if not _is_trusted(hop, nets):
            return hop
    return hops[0] if hops else peer


def _me(request: Request, p: Principal) -> dict:
    hub = hub_of(request)
    biz = hub.registry.get_business(p.business_id)
    repo = hub.platform(p.business_id).repo
    s = repo.settings()
    setup = {"godowns": len(repo.list_warehouses()), "products": len(repo.list_products()), "customers": len(repo.list_customers()),
             "vehicles": len(repo.list_vehicles()), "routes": len(repo.list_routes()), "staff": len(hub.registry.list_users(p.business_id, include_inactive=False))}
    return {"user_id": p.user_id, "name": p.name, "phone": p.phone, "role": p.role, "role_title": ROLE_TITLES[p.role],
            "must_change_pin": p.must_change_pin,
            "business": {"id": biz["business_id"], "name": s["business_name"], "city": s["city"], "plan": biz["plan"], "demo": biz["business_id"] == DEMO_BUSINESS_ID},
            "permissions": sorted(k for k, roles in PERMISSIONS.items() if p.role in roles),
            "llm": os.environ.get("LLM_PROVIDER", "stub"), "voice": bool(os.environ.get("GROQ_API_KEY")),
            "channel": hub.platform(p.business_id).channel.name, "language": s["language"], "currency": s["currency"], "setup": setup}


@router.get("/config")
def public_config(request: Request):
    """What the sign-in screen needs before anyone is signed in."""
    hub = hub_of(request)
    demo = hub.registry.list_businesses() and any(b["business_id"] == DEMO_BUSINESS_ID for b in hub.registry.list_businesses())
    return {"demo": bool(demo), "demo_users": [{"name": n, "phone": ph, "role": r, "pin": pin} for n, ph, r, pin in DEMO_USERS] if demo else [],
            "signup_open": request.app.state.signup_open, "roles": ROLE_TITLES, "version": request.app.version}


@router.post("/signup", status_code=201)
def signup(body: SignupIn, request: Request):
    if not request.app.state.signup_open:
        raise HTTPException(403, "sign-up is closed on this server")
    if not request.app.state.signup_limiter.allow(_client(request)):
        raise HTTPException(429, "too many sign-ups from this address — try later")
    hub = hub_of(request)
    try:
        r = hub.signup(body.business_name, body.city, body.owner_name, body.phone, body.pin, body.sample_data, body.language)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"token": r["token"], "me": _me(request, r["principal"])}


@router.post("/session")
def sign_in(body: SessionIn, request: Request):
    if not request.app.state.login_limiter.allow(_client(request)):
        raise HTTPException(429, "too many attempts — wait a minute")
    try:
        token, p, others = hub_of(request).registry.authenticate(body.phone, body.pin, body.device or request.headers.get("user-agent", "")[:80])
    except LockedError as e:
        raise HTTPException(423, str(e))
    except AuthError as e:
        raise HTTPException(401, str(e))
    return {"token": token, "me": _me(request, p), "other_businesses": [{"id": b["business_id"], "name": b["name"]} for b in others]}


@router.post("/session/switch")
def switch(body: SwitchIn, request: Request, p: Principal = Depends(current), token: str | None = Depends(token_of)):
    hub = hub_of(request)
    if not request.app.state.login_limiter.allow(f"switch:{p.user_id}"):
        raise HTTPException(429, "too many attempts — wait a minute")
    try:
        new_token, np = hub.registry.switch_business(p, body.business_id, pin=body.pin,
                                                     device=request.headers.get("user-agent", "")[:80])
    except LockedError as e:
        raise HTTPException(423, str(e))
    except AuthError as e:
        raise HTTPException(403, str(e))
    if token: hub.registry.revoke(token)
    return {"token": new_token, "me": _me(request, np)}


@router.post("/session/logout")
def logout(request: Request, p: Principal = Depends(current), token: str | None = Depends(token_of)):
    if token: hub_of(request).registry.revoke(token)
    return {"ok": True}


@router.get("/me")
def me(request: Request, p: Principal = Depends(current)):
    return _me(request, p)


@router.post("/me/pin")
def change_pin(body: PinChangeIn, request: Request, p: Principal = Depends(current)):
    reg = hub_of(request).registry
    try:
        ok = reg.verify_pin(p.user_id, body.old_pin)     # this user's own PIN, counted toward lockout
    except LockedError as e:
        raise HTTPException(423, str(e))
    if not ok:
        raise HTTPException(401, "current PIN is wrong")
    if body.new_pin.strip() == body.old_pin.strip():
        raise HTTPException(400, "choose a PIN different from the one you were given")
    try:
        reg.set_pin(p.user_id, body.new_pin, must_change=False)   # revokes every session, including this one; clears must_change_pin
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "signed_out": True}


@router.get("/me/sessions")
def my_sessions(request: Request, p: Principal = Depends(current)):
    return hub_of(request).registry.sessions_for(p.user_id)


# ---------------------------------------------------------------- staff (owner)
# The legacy /api/staff routes keep working unchanged for the phone app, as thin wrappers over the one employee master
# (plan §4.3): when payroll is enabled on the business, a new login also gets an employee row, and deactivation / role
# changes / PIN resets are written to that employee's life history. Best effort: the registry write is the action;
# a failure to mirror it never undoes it (the Employees screen re-syncs on open).
_ROLE_HINT = {"owner": "other", "clerk": "clerk", "salesman": "salesman", "driver": "driver"}


def _mirror(c: Ctx, what: str, fn) -> None:
    try:
        if c.repo.payroll_ready():
            fn()
    except Exception:       # noqa: BLE001 -- see above; logged without any request body (a PIN may be in it)
        log.warning("staff route: could not mirror %s onto the employee master", what)


@router.get("/staff")
def staff(request: Request, p: Principal = Depends(require("staff:manage"))):
    return [u | {"role_title": ROLE_TITLES[u["role"]]} for u in hub_of(request).registry.list_users(p.business_id)]


@router.post("/staff", status_code=201)
def add_staff(body: StaffIn, request: Request, c: Ctx = Depends(context("staff:manage"))):
    try:
        u = hub_of(request).registry.create_user(c.principal.business_id, body.name, body.phone, body.role, body.pin)
    except ValueError as e:
        raise HTTPException(400, str(e))
    c.repo.audit(c.role, "staff_added", "user", u["user_id"], {"name": u["name"], "role": u["role"]}, approved_by=c.signature)

    def employee():
        e = c.repo.add_employee({"name": u["name"], "phone": u["phone"], "role_hint": _ROLE_HINT[u["role"]], "designation": ROLE_TITLES[u["role"]]},
                                c.role, c.signature)
        c.repo.link_login(e["employee_id"], u["user_id"], u["role"], c.role, generated=False)
    _mirror(c, "a new login", employee)
    return u


@router.patch("/staff/{user_id}")
def edit_staff(user_id: str, body: StaffPatch, request: Request, c: Ctx = Depends(context("staff:manage"))):
    reg = hub_of(request).registry
    try:
        u = reg.get_user(user_id)
    except AuthError:
        raise HTTPException(404, "no such user")
    if u["business_id"] != c.principal.business_id:
        raise HTTPException(404, "no such user")
    if (body.active is False or (body.role and body.role != "owner")) and u["role"] == "owner" and reg.owners_count(c.principal.business_id) <= 1:
        raise HTTPException(409, "a business needs at least one active owner")
    try:
        u = reg.update_user(user_id, name=body.name, role=body.role, active=body.active)
    except ValueError as e:
        raise HTTPException(400, str(e))
    c.repo.audit(c.role, "staff_updated", "user", user_id, body.model_dump(exclude_none=True), approved_by=c.signature)
    if body.role:
        _mirror(c, "a role change", lambda: c.repo.login_event(user_id, "role_changed", c.role, {"role": body.role}))
    if body.active is not None:
        _mirror(c, "a login switch", lambda: c.repo.login_event(user_id, "login_enabled" if body.active else "login_disabled", c.role, {}))
    return u


@router.post("/staff/{user_id}/pin")
def reset_staff_pin(user_id: str, body: StaffPin, request: Request, c: Ctx = Depends(context("staff:manage"))):
    reg = hub_of(request).registry
    u = reg.get_user(user_id)
    if u["business_id"] != c.principal.business_id: raise HTTPException(404, "no such user")
    try:
        reg.set_pin(user_id, body.pin)
    except ValueError as e:
        raise HTTPException(400, str(e))
    c.repo.audit(c.role, "staff_pin_reset", "user", user_id, {}, approved_by=c.signature)
    _mirror(c, "a PIN reset", lambda: c.repo.login_event(user_id, "pin_reset", c.role, {"generated": False}))
    return {"ok": True}


@router.post("/staff/{user_id}/signout")
def signout_staff(user_id: str, request: Request, c: Ctx = Depends(context("staff:manage"))):
    reg = hub_of(request).registry
    u = reg.get_user(user_id)
    if u["business_id"] != c.principal.business_id: raise HTTPException(404, "no such user")
    n = reg.revoke_all(user_id)
    c.repo.audit(c.role, "staff_signed_out", "user", user_id, {"sessions": n}, approved_by=c.signature)
    return {"sessions_revoked": n}


@router.get("/roles")
def roles(p: Principal = Depends(current)):
    return [{"role": r, "title": ROLE_TITLES[r], "permissions": sorted(k for k, rs in PERMISSIONS.items() if r in rs)} for r in ROLES]
