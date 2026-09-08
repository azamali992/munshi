"""Munshi's HTTP layer: the JSON API the mobile app talks to, plus the app
itself served as an installable PWA. One process, one SQLite file, no other
services."""
from __future__ import annotations

import hashlib
import hmac
import io
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from munshi.domain.repository import (CapacityError, CreditHoldError, InsufficientStockError, MunshiRepository,
                                      NotFoundError, OtpError, StateError)
from munshi.domain.seed import seed
from munshi.llm.factory import build_chat_model
from munshi.platform import MunshiPlatform
from munshi.safety.risk import role_may_approve

STATIC = Path(__file__).parent / "static"
SECRET = os.environ.get("MUNSHI_SECRET", "change-me-in-production").encode()
PINS = {
    "owner": os.environ.get("OWNER_PIN", "1111"),
    "clerk": os.environ.get("CLERK_PIN", "2222"),
    "driver": os.environ.get("DRIVER_PIN", "3333"),
}


class PinIn(BaseModel):
    pin: str


class ChatIn(BaseModel):
    thread_id: str = "main"
    text: str


class Decision(BaseModel):
    approve: bool
    note: str = ""


class CloseIn(BaseModel):
    delivered_items: list[dict]
    returned_items: list[dict] = []
    cash_collected: float = 0
    otp: str


def _token(role: str) -> str:
    return role + "." + hmac.new(SECRET, role.encode(), hashlib.sha256).hexdigest()[:24]


def _role_from(token: str | None) -> str:
    if not token or "." not in token:
        raise HTTPException(401, "sign in with your PIN")
    role, sig = token.split(".", 1)
    if role not in PINS or not hmac.compare_digest(_token(role), token):
        raise HTTPException(401, "invalid session")
    return role


def current_role(x_session: Optional[str] = Header(default=None)) -> str:
    return _role_from(x_session)


def build_app(db_path: str | None = None, model=None, enable_tracing: bool | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("MUNSHI_DB", "munshi.db")
    fresh = db_path == ":memory:" or not Path(db_path).exists()
    repo = MunshiRepository(db_path)
    if fresh:
        seed(repo)
    if model is None:
        model = build_chat_model()
    tracing = enable_tracing if enable_tracing is not None else os.environ.get("MUNSHI_TRACING", "0") == "1"
    platform = MunshiPlatform(repo, model, enable_tracing=tracing)

    app = FastAPI(title="Munshi", version="0.1.0")
    app.state.platform = platform
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    # ---------------- errors -> clean messages
    @app.exception_handler(NotFoundError)
    async def _nf(_, e): raise HTTPException(404, str(e))
    for exc, code in ((InsufficientStockError, 409), (CapacityError, 409), (StateError, 409), (OtpError, 403), (CreditHoldError, 409), (ValueError, 400), (PermissionError, 403), (KeyError, 404)):
        def _mk(code):
            async def h(_, e): raise HTTPException(code, str(e))
            return h
        app.add_exception_handler(exc, _mk(code))

    # ---------------- app shell
    @app.get("/")
    def index(): return FileResponse(STATIC / "index.html")

    @app.get("/manifest.webmanifest")
    def manifest(): return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    def sw(): return FileResponse(STATIC / "sw.js", media_type="application/javascript")

    # ---------------- session
    @app.post("/api/session")
    def session(body: PinIn):
        for role, pin in PINS.items():
            if hmac.compare_digest(body.pin, pin):
                return {"role": role, "token": _token(role)}
        raise HTTPException(401, "wrong PIN")

    @app.get("/api/me")
    def me(role: str = Depends(current_role)):
        return {"role": role, "llm": os.environ.get("LLM_PROVIDER", "stub"), "voice": bool(os.environ.get("GROQ_API_KEY")),
                "business": "Sultan Traders"}

    # ---------------- chat + approvals
    @app.post("/api/chat")
    def chat(body: ChatIn, role: str = Depends(current_role)):
        r = platform.handle_message(body.thread_id, role, body.text.strip())
        return {"text": r.text, "specialist": r.specialist, "pending": (asdict(r.pending) | {"summary": r.pending.describe()}) if r.pending else None}

    @app.get("/api/chat/{thread_id}")
    def history(thread_id: str, role: str = Depends(current_role)):
        return platform.repo.chat_history(thread_id)

    @app.get("/api/approvals")
    def approvals(role: str = Depends(current_role)):
        return [p | {"can_approve": role_may_approve(role, p["tool"])} for p in platform.list_pending()]

    @app.post("/api/approvals/{approval_id}")
    def decide(approval_id: str, body: Decision, role: str = Depends(current_role)):
        r = platform.resolve(approval_id, body.approve, role, body.note)
        return {"text": r.text, "specialist": r.specialist}

    # ---------------- reads
    @app.get("/api/digest")
    def digest(role: str = Depends(current_role)):
        d = platform.repo.digest(); d["pending_approvals"] = len(platform.pending); return d

    @app.get("/api/orders")
    def orders(status: str = "", role: str = Depends(current_role)):
        return platform.ops.list_orders(status)

    @app.get("/api/orders/{order_id}")
    def order(order_id: str, role: str = Depends(current_role)):
        return platform.ops.get_order(order_id)

    @app.get("/api/plans")
    def plans(date: str = "", role: str = Depends(current_role)):
        out = []
        for p in platform.repo.list_plans(date or None):
            d = asdict(p); d["stops"] = [asdict(s) for s in platform.repo.list_stops(p.plan_id)]
            d["route_name"] = platform.repo.get_route(p.route_id).name; d["plate"] = platform.repo.get_vehicle(p.vehicle_id).plate
            for s in d["stops"]:
                s["customer_name"] = platform.repo.get_customer(s["customer_id"]).name
                o = platform.repo.get_order(s["order_id"]); s["items"] = [i.__dict__ for i in o.items]; s["order_total"] = o.total
                if role != "driver": pass
                else: s["otp"] = None  # the driver never sees the OTP; the customer holds it
            out.append(d)
        return out

    @app.post("/api/stops/{stop_id}/close")
    def close_stop(stop_id: str, body: CloseIn, role: str = Depends(current_role)):
        return platform.ops.close_stop(stop_id, body.delivered_items, body.returned_items, body.cash_collected, body.otp)

    @app.get("/api/khata")
    def khata(role: str = Depends(current_role)):
        return platform.repo.aging()

    @app.get("/api/khata/{customer_id}")
    def khata_one(customer_id: str, role: str = Depends(current_role)):
        return platform.ops.get_customer_khata(customer_id)

    @app.get("/api/reminders")
    def reminders(status: str = "", role: str = Depends(current_role)):
        return [asdict(r) | {"customer_name": platform.repo.get_customer(r.customer_id).name} for r in platform.repo.list_reminders(status or None)]

    @app.post("/api/reminders/{reminder_id}/send")
    def send_reminder(reminder_id: str, role: str = Depends(current_role)):
        if not role_may_approve(role, "send_reminder"): raise HTTPException(403, "clerk or owner only")
        return platform.ops.send_reminder(reminder_id, approved_by=role)

    @app.get("/api/stock")
    def stock(role: str = Depends(current_role)):
        names = {p.sku: p.name for p in platform.repo.list_products()}
        whs = {w.warehouse_id: w.name for w in platform.repo.list_warehouses()}
        return [asdict(s) | {"available": s.available, "name": names.get(s.sku, s.sku), "warehouse": whs.get(s.warehouse_id)} for s in platform.repo.list_stock()]

    @app.get("/api/customers")
    def customers(role: str = Depends(current_role)):
        return [asdict(c) | {"outstanding": platform.repo.outstanding(c.customer_id)} for c in platform.repo.list_customers()]

    @app.get("/api/products")
    def products(role: str = Depends(current_role)):
        return [asdict(p) for p in platform.repo.list_products()]

    @app.get("/api/audit")
    def audit(role: str = Depends(current_role)):
        if role == "driver": raise HTTPException(403, "owner or clerk only")
        return platform.repo.audit_log(100)

    # ---------------- export: the business's own data, as a workbook
    @app.get("/api/export.xlsx")
    def export(role: str = Depends(current_role)):
        if role == "driver": raise HTTPException(403, "owner or clerk only")
        from openpyxl import Workbook
        wb = Workbook(); wb.remove(wb.active)
        def sheet(name, rows, header):
            ws = wb.create_sheet(name); ws.append(header)
            for r in rows: ws.append([r.get(h, "") if isinstance(r, dict) else r for h in header])
        sheet("Orders", platform.ops.list_orders(""), ["order_id", "customer_name", "status", "total", "created_at", "channel"])
        led = []
        for c in platform.repo.list_customers():
            for e in platform.repo.ledger_for(c.customer_id): led.append(asdict(e) | {"customer": c.name})
        sheet("Khata", led, ["entry_id", "customer", "kind", "amount", "ref", "due_date", "created_at"])
        sheet("Aging", platform.repo.aging(), ["customer_id", "name", "balance", "days_overdue", "bucket"])
        sheet("Stock", stock(role), ["warehouse", "sku", "name", "on_hand", "reserved", "available"])
        sheet("Audit", platform.repo.audit_log(1000), ["created_at", "actor", "action", "entity", "entity_id", "approved_by"])
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": "attachment; filename=munshi-export.xlsx"})

    # ---------------- voice orders (optional, needs GROQ_API_KEY)
    @app.post("/api/voice")
    async def voice(thread_id: str = "main", audio: UploadFile = File(...), role: str = Depends(current_role)):
        key = os.environ.get("GROQ_API_KEY")
        if not key: raise HTTPException(503, "Voice needs GROQ_API_KEY (free at console.groq.com)")
        from groq import Groq
        data = await audio.read()
        tr = Groq(api_key=key).audio.transcriptions.create(file=(audio.filename or "note.webm", data), model="whisper-large-v3", language="ur" if os.environ.get("VOICE_LANG", "auto") == "ur" else None)
        text = tr.text.strip()
        r = platform.handle_message(thread_id, role, text)
        return {"transcript": text, "text": r.text, "specialist": r.specialist,
                "pending": (asdict(r.pending) | {"summary": r.pending.describe()}) if r.pending else None}

    return app


app = build_app()
