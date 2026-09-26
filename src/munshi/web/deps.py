"""Request-scoped dependencies: who is calling, what they may do, and which
business's platform they get. Every route in web/routes/ goes through
`require(<permission>)`; nothing reads a tenant without a Principal."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from munshi.auth import AuthError, Principal
from munshi.domain.repository import MunshiRepository
from munshi.platform import MunshiPlatform
from munshi.tenancy.hub import TenantHub


def hub_of(request: Request) -> TenantHub:
    return request.app.state.hub


def token_of(x_session: str | None = Header(default=None), authorization: str | None = Header(default=None)) -> str | None:
    if x_session: return x_session.strip()
    if authorization and authorization.lower().startswith("bearer "): return authorization[7:].strip()
    return None


# While a user still carries an owner-issued PIN (Principal.must_change_pin) these are the ONLY routes they can reach:
# who am I, change my PIN, sign out. Everything else answers 403 {"detail": {"error": "pin_change_required", ...}}.
# Enforced here, in the one dependency every authenticated route goes through -- not in middleware.
PIN_CHANGE_ALLOWED = frozenset({("GET", "/api/me"), ("POST", "/api/me/pin"), ("POST", "/api/session/logout")})


def current(request: Request, token: str | None = Depends(token_of)) -> Principal:
    try:
        p = hub_of(request).registry.resolve(token)
    except AuthError as e:
        raise HTTPException(401, str(e))
    request.state.principal = p
    # scope["path"], not request.url.path: request.url is rebuilt from the Host header, and a malformed Host made that
    # path lie to path-based auth checks before Starlette 1.0.1 (CVE-2026-48710 "BadHost"; this venv has 1.7.0).
    if p.must_change_pin and (request.method, str(request.scope.get("path", "")).rstrip("/")) not in PIN_CHANGE_ALLOWED:
        raise HTTPException(403, {"error": "pin_change_required", "message": "choose your own PIN before using Munshi"})
    return p


def require(permission: str):
    def dep(p: Principal = Depends(current)) -> Principal:
        if not p.can(permission):
            raise HTTPException(403, f"your role ({p.role}) can't do that")
        return p
    return dep


@dataclass
class Ctx:
    principal: Principal
    platform: MunshiPlatform

    @property
    def repo(self) -> MunshiRepository:
        return self.platform.repo

    @property
    def role(self) -> str:
        return self.principal.role

    @property
    def who(self) -> str:
        return self.principal.name

    @property
    def signature(self) -> str:
        """Who approved a direct (form-based) action: the human, named."""
        return f"{self.principal.role}:{self.principal.name}"


def context(permission: str):
    """Dependency: a Ctx for the caller's business, after the permission check.

    Also names the caller as the acting user for every write this request makes. That identity
    is a ContextVar, so it must be set in the request's own asyncio task: this dependency is
    deliberately `async`. (A sync dependency runs on a worker thread in a COPY of the request's
    context, so a value set there would silently never reach the endpoint -- whose own worker
    thread gets a fresh copy of the task's context, with this caller's name in it.)"""
    async def dep(request: Request, p: Principal = Depends(require(permission))) -> Ctx:
        platform = await run_in_threadpool(hub_of(request).platform, p.business_id)   # may open/seed a database
        platform.repo.set_current_user(p.name)          # this request's task only; never another request's
        return Ctx(p, platform)
    return dep
