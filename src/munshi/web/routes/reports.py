"""Reports, documents (invoices, receipts, statements), audit, export, backup."""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse

from munshi.documents.excel import export_xlsx
from munshi.documents.invoice import invoice_data, loading_sheet_html, render_html, render_pdf, sign, statement_html, verify, whatsapp_text
from munshi.domain.models import today_iso
from munshi.web.deps import Ctx, context, hub_of

router = APIRouter(tags=["reports"])
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _range(start: str, end: str, days: int = 30) -> tuple[str, str]:
    end = end or today_iso(); start = start or (date.fromisoformat(end) - timedelta(days=days - 1)).isoformat()
    return start, end


@router.get("/api/digest")
def digest(c: Ctx = Depends(context("chat"))):
    d = c.repo.digest()
    d["pending_approvals"] = len(c.platform.list_pending())
    d["business"] = c.repo.business_name
    if c.role in ("driver", "salesman"):      # no money totals for the field
        d = {k: d[k] for k in ("date", "orders", "dispatch", "low_stock", "business", "pending_approvals")}
    return d


@router.get("/api/reports/sales")
def sales(start: str = "", end: str = "", c: Ctx = Depends(context("reports:read"))):
    return c.repo.sales_report(*_range(start, end))


@router.get("/api/reports/profit")
def profit(start: str = "", end: str = "", c: Ctx = Depends(context("reports:read"))):
    return c.repo.profit_summary(*_range(start, end))


@router.get("/api/reports/collections")
def collections(start: str = "", end: str = "", c: Ctx = Depends(context("reports:read"))):
    return c.repo.collection_report(*_range(start, end))


@router.get("/api/reports/cashbook")
def cashbook(date: str = "", c: Ctx = Depends(context("reports:read"))):
    return c.repo.cashbook(date or None)


@router.get("/api/reports/stock-valuation")
def valuation(c: Ctx = Depends(context("reports:read"))):
    return c.repo.stock_valuation()


@router.get("/api/reports/slow-stock")
def slow(days: int = 30, c: Ctx = Depends(context("reports:read"))):
    return c.repo.slow_stock(max(7, min(days, 365)))


@router.get("/api/reports/stock-ledger/{sku}")
def ledger(sku: str, warehouse_id: str = "", c: Ctx = Depends(context("stock:read"))):
    return c.repo.stock_ledger(sku, warehouse_id or None)


@router.get("/api/reports/top-customers")
def top(days: int = 30, c: Ctx = Depends(context("reports:read"))):
    return c.repo.top_customers(max(7, min(days, 365)))


# ---------------------------------------------------------------- documents
def _secret(request: Request) -> bytes:
    return request.app.state.secret


def _public_url(request: Request, entry_id: str, business_id: str) -> str:
    base = os.environ.get("MUNSHI_PUBLIC_URL", str(request.base_url).rstrip("/"))
    return f"{base}/i/{business_id}.{entry_id}.{sign(_secret(request), business_id + ':' + entry_id)}"


@router.get("/api/documents/{entry_id}")
def document(entry_id: str, request: Request, c: Ctx = Depends(context("khata:read"))):
    d = invoice_data(c.repo, entry_id)
    url = _public_url(request, entry_id, c.principal.business_id)
    digits = "".join(ch for ch in d["customer"]["phone"] if ch.isdigit())
    if digits.startswith("0") and len(digits) == 11: digits = "92" + digits[1:]
    return d | {"public_url": url, "whatsapp_text": whatsapp_text(d, url), "wa_link": f"https://wa.me/{digits}?text={quote(whatsapp_text(d, url))}" if digits else None}


@router.get("/api/documents/{entry_id}/pdf")
def document_pdf(entry_id: str, c: Ctx = Depends(context("khata:read"))):
    d = invoice_data(c.repo, entry_id)
    return Response(render_pdf(d), media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{entry_id}.pdf"'})


@router.get("/api/documents/{entry_id}/html", response_class=HTMLResponse)
def document_html(entry_id: str, request: Request, c: Ctx = Depends(context("khata:read"))):
    return render_html(invoice_data(c.repo, entry_id), _public_url(request, entry_id, c.principal.business_id))


@router.get("/api/statements/{customer_id}/html", response_class=HTMLResponse)
def statement(customer_id: str, c: Ctx = Depends(context("khata:read"))):
    return statement_html(c.repo, customer_id)


@router.get("/api/plans/{plan_id}/loading-sheet", response_class=HTMLResponse)
def loading_sheet(plan_id: str, c: Ctx = Depends(context("dispatch:read"))):
    return loading_sheet_html(c.repo, plan_id)


@router.get("/api/statements/{customer_id}/link")
def statement_link(customer_id: str, request: Request, c: Ctx = Depends(context("khata:read"))):
    """A signed link the customer can open without an account."""
    c.repo.get_customer(customer_id)
    base = os.environ.get("MUNSHI_PUBLIC_URL", str(request.base_url).rstrip("/"))
    return {"url": f"{base}/s/{c.principal.business_id}.{customer_id}.{sign(_secret(request), 'stmt:' + c.principal.business_id + ':' + customer_id)}"}


@router.get("/s/{token}", response_class=HTMLResponse)
def public_statement(token: str, request: Request):
    parts = token.split(".")
    if len(parts) != 3: raise HTTPException(404, "no such document")
    business_id, customer_id, sig = parts
    if not verify(_secret(request), "stmt:" + business_id + ":" + customer_id, sig): raise HTTPException(404, "no such document")
    hub = hub_of(request)
    try:
        hub.registry.get_business(business_id)
        return statement_html(hub.platform(business_id).repo, customer_id)
    except Exception:
        raise HTTPException(404, "no such document")


@router.get("/i/{token}", response_class=HTMLResponse)
def public_invoice(token: str, request: Request):
    """The link a customer gets on WhatsApp: <business>.<entry>.<signature>. The signature covers both
    ids, so a link can't be guessed or pointed at another business's document."""
    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(404, "no such document")
    business_id, entry_id, sig = parts
    if not verify(_secret(request), business_id + ":" + entry_id, sig):
        raise HTTPException(404, "no such document")
    hub = hub_of(request)
    try:
        hub.registry.get_business(business_id)
        d = invoice_data(hub.platform(business_id).repo, entry_id)
    except Exception:
        raise HTTPException(404, "no such document")
    return render_html(d, f"{os.environ.get('MUNSHI_PUBLIC_URL', str(request.base_url).rstrip('/'))}/i/{token}")


# ---------------------------------------------------------------- audit, export, backup
@router.get("/api/audit")
def audit(entity_id: str = "", limit: int = 200, c: Ctx = Depends(context("audit:read"))):
    return c.repo.audit_log(min(limit, 1000), entity_id or None)


@router.get("/api/export.xlsx")
def export(c: Ctx = Depends(context("export"))):
    c.repo.audit(c.role, "export", "business", "workbook", {}, approved_by=c.signature)
    return Response(export_xlsx(c.repo), media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="munshi-{today_iso()}.xlsx"'})


@router.get("/api/backup")
def backup(request: Request, c: Ctx = Depends(context("backup"))):
    """The business's whole SQLite file, consistent (VACUUM INTO), for the owner to keep."""
    hub = hub_of(request)
    if hub.in_memory:
        raise HTTPException(501, "backup needs an on-disk installation")
    out = Path(hub.data_dir) / "backups"; out.mkdir(exist_ok=True)
    target = out / f"{c.principal.business_id}-{today_iso()}.db"
    if target.exists(): target.unlink()
    with c.repo._lock:
        c.repo._conn.execute("VACUUM INTO ?", (str(target),))
    c.repo.audit(c.role, "backup", "business", target.name, {}, approved_by=c.signature)
    return FileResponse(str(target), media_type="application/x-sqlite3", filename=f"munshi-backup-{today_iso()}.db")
