"""Munshi's HTTP layer: the JSON API the mobile app talks to, the app itself
served as an installable PWA, public invoice links, and a small in-process
scheduler (owner digest, outbox delivery). One process, one data directory,
no other services.

Security posture (see docs/SECURITY.md):
- every /api route except config/signup/session requires a session token
  that maps to one user in one business; permissions are role-based and
  declared per route;
- strict security headers and a CSP that allows only this origin plus fonts;
- sign-in and sign-up are rate-limited per client address;
- production mode (MUNSHI_ENV=production) refuses to start with the default
  secret and closes public sign-up unless explicitly opened.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from munshi.auth import AuthError
from munshi.auth.ratelimit import RateLimiter
from munshi.domain.models import business_now, business_today
from munshi.domain.repository import CapacityError, CreditHoldError, InsufficientStockError, NotFoundError, OtpError, StateError
from munshi.llm.factory import build_chat_model
from munshi.tenancy.hub import TenantHub
from munshi.web.routes import auth, money, ops, reports, setup

STATIC = Path(__file__).parent / "static"
VERSION = "1.0.0"
log = logging.getLogger("munshi.web")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; media-src 'self' blob:; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json
        d = {"t": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "lvl": record.levelname, "src": record.name, "msg": record.getMessage()}
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)[-2000:]
        return json.dumps(d, ensure_ascii=False)


def _configure_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_munshi_configured", False):
        return
    h = logging.StreamHandler(); h.setFormatter(_JsonFormatter())
    root.handlers = [h]; root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._munshi_configured = True   # type: ignore[attr-defined]


def _secret() -> bytes:
    s = os.environ.get("MUNSHI_SECRET", "")
    prod = os.environ.get("MUNSHI_ENV", "development") == "production"
    if not s or s == "change-me":
        if prod:
            raise RuntimeError("MUNSHI_ENV=production needs a real MUNSHI_SECRET (python3 -c 'import secrets;print(secrets.token_hex(32))')")
        s = "dev-" + secrets.token_hex(8)     # dev: invoice links stop working across restarts, and that's fine
    return s.encode()


def nightly_backup(hub: TenantHub, keep_days: int = 14) -> list[str]:
    """A consistent copy of every business file under data/backups, pruning copies older than keep_days."""
    if hub.in_memory: return []
    out = Path(hub.data_dir) / "backups"; out.mkdir(parents=True, exist_ok=True)
    today = business_today().isoformat(); written = []
    for b in hub.registry.list_businesses():
        if not b["active"]: continue
        target = out / f"{b['business_id']}-{today}.db"
        if target.exists(): target.unlink()
        repo = hub.platform(b["business_id"]).repo
        with repo._lock:
            repo._conn.execute("VACUUM INTO ?", (str(target),))
        written.append(target.name)
    cutoff = time.time() - keep_days * 86400
    for f in out.glob("*.db"):
        if f.stat().st_mtime < cutoff: f.unlink()
    return written


def scheduler_tick(hub: TenantHub, sent_today: dict[str, str], now: datetime | None = None) -> list[str]:
    """One pass over every business: deliver the outbox; at the business's digest_time, write the
    owner's digest as a notification and queue it to the owner's WhatsApp; at MUNSHI_BACKUP_TIME,
    back every business up. digest_time and MUNSHI_BACKUP_TIME are business-local (Asia/Karachi)
    wall-clock times, so `now` is evaluated in that zone regardless of the server's own timezone.
    Returns the businesses digested."""
    now = now or business_now()
    hhmm, today = now.strftime("%H:%M"), now.date().isoformat()
    digested = []
    if hhmm == os.environ.get("MUNSHI_BACKUP_TIME", "02:30") and sent_today.get("__backup__") != today:
        sent_today["__backup__"] = today
        log.info("nightly backup: %s", nightly_backup(hub))
    for b in hub.registry.list_businesses():
        if not b["active"]: continue
        pf = hub.platform(b["business_id"]); repo = pf.repo
        pf.deliver_messages()
        if repo.setting("digest_time") == hhmm and sent_today.get(b["business_id"]) != today:
            d = repo.digest()
            text = (f"{repo.business_name} — {d['date']}: orders {d['orders']['count']} (Rs {d['orders']['value']:,.0f}), delivered {d['dispatch']['delivered']}/{d['dispatch']['stops']}, "
                    f"cash collected Rs {d['cash']['collected']:,.0f} / deposited Rs {d['cash']['deposited']:,.0f}, receivables Rs {d['receivables']['total']:,.0f} "
                    f"(60+ days Rs {d['receivables']['overdue_60']:,.0f}), payables Rs {d['payables']:,.0f}, low stock {len(d['low_stock'])}.")
            repo.notify("owner", "digest", text)
            broken = repo.broken_promises()
            if broken:
                repo.notify("clerk", "promise", "Broken promises: " + ", ".join(f"{b['name']} Rs {b['amount']:,.0f} (by {b['date']})" for b in broken[:6]))
            if d["low_stock"]:
                repo.notify("clerk", "low_stock", "Low stock: " + ", ".join(f"{x['sku']} {x['available']} @ {x['warehouse_id']}" for x in d["low_stock"][:6]))
            if repo.setting("owner_phone"):
                repo.queue_message("whatsapp", repo.setting("owner_phone"), text, "digest")
            sent_today[b["business_id"]] = today
            digested.append(b["business_id"])
    hub.registry.purge_expired()
    return digested


def build_app(data_dir: str | None = None, model=None, enable_tracing: bool | None = None, demo: bool | None = None,
              in_memory: bool = False, scheduler: bool | None = None) -> FastAPI:
    _configure_logging()
    prod = os.environ.get("MUNSHI_ENV", "development") == "production"
    if model is None:
        model = build_chat_model()
    tracing = enable_tracing if enable_tracing is not None else os.environ.get("MUNSHI_TRACING", "0") == "1"
    hub = TenantHub(data_dir, model, enable_tracing=tracing, in_memory=in_memory)
    want_demo = demo if demo is not None else os.environ.get("MUNSHI_DEMO", "1") == "1"
    if want_demo:
        hub.ensure_demo()

    app = FastAPI(title="Munshi", version=VERSION, docs_url="/api/docs" if not prod else None, redoc_url=None, openapi_url="/api/openapi.json" if not prod else None)
    app.state.hub = hub
    app.state.secret = _secret()
    # public sign-up: open in development, closed in production unless the operator sets MUNSHI_SIGNUP=1
    app.state.signup_open = os.environ["MUNSHI_SIGNUP"] == "1" if os.environ.get("MUNSHI_SIGNUP") is not None else not prod
    app.state.login_limiter = RateLimiter(rate_per_minute=10, burst=10)
    app.state.signup_limiter = RateLimiter(rate_per_minute=3, burst=3)
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    for r in (auth.router, setup.router, ops.router, money.router, reports.router):
        app.include_router(r)

    # ---------------- errors -> clean messages, never stack traces
    @app.exception_handler(NotFoundError)
    async def _nf(_, e): return JSONResponse({"detail": str(e)}, 404)
    for exc, code in ((InsufficientStockError, 409), (CapacityError, 409), (StateError, 409), (OtpError, 403), (CreditHoldError, 409),
                      (ValueError, 400), (PermissionError, 403), (KeyError, 404), (AuthError, 401)):
        def _mk(code):
            async def h(_, e): return JSONResponse({"detail": str(e).strip("'")}, code)
            return h
        app.add_exception_handler(exc, _mk(code))

    @app.exception_handler(Exception)
    async def _boom(request: Request, e: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "something went wrong on our side — it has been logged"}, 500)

    # ---------------- security headers + request log
    @app.middleware("http")
    async def _headers(request: Request, call_next):
        rid = uuid.uuid4().hex[:12]; t0 = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
        response.headers["Content-Security-Policy"] = CSP
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if request.headers.get("x-forwarded-proto") == "https" or prod:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith("/api/") and request.url.path not in ("/api/badge",):
            p = getattr(request.state, "principal", None)
            log.info("%s %s %s %dms user=%s biz=%s rid=%s", request.method, request.url.path, response.status_code, int((time.monotonic() - t0) * 1000),
                     p.user_id if p else "-", p.business_id if p else "-", rid)
        return response

    # ---------------- app shell
    @app.get("/", include_in_schema=False)
    def index(): return FileResponse(STATIC / "index.html")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest(): return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def sw(): return FileResponse(STATIC / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        # CORS-open on purpose: the Android shell's connect screen (origin https://localhost) probes this before navigating here
        return JSONResponse({"ok": True, "version": VERSION, "businesses": len(hub.registry.list_businesses())}, headers={"Access-Control-Allow-Origin": "*"})

    # ---------------- scheduler: owner digest + outbox delivery, once a minute
    want_sched = scheduler if scheduler is not None else (os.environ.get("MUNSHI_SCHEDULER", "1") == "1" and not in_memory)
    if want_sched:
        stop = threading.Event()
        sent_today: dict[str, str] = {}

        def loop():
            while not stop.wait(60):
                try:
                    scheduler_tick(hub, sent_today)
                except Exception:
                    log.exception("scheduler tick failed")

        t = threading.Thread(target=loop, name="munshi-scheduler", daemon=True); t.start()
        app.state.scheduler_stop = stop

    log.info("Munshi %s ready: data=%s demo=%s llm=%s env=%s", VERSION, "memory" if in_memory else hub.data_dir, want_demo, os.environ.get("LLM_PROVIDER", "stub"), "production" if prod else "development")
    return app


# uvicorn entry point: `uvicorn munshi.web.app:app`. Tests build their own app and set MUNSHI_NO_AUTOAPP=1.
app = build_app() if os.environ.get("MUNSHI_NO_AUTOAPP") != "1" else None
