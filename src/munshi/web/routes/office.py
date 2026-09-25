"""The desktop office console (/office): the page itself, and the few API routes the console needs that the phone
app's routes don't already provide. Everything else the console does -- add/edit products, customers, suppliers,
godowns, stock adjustments and transfers, purchases, khata, statements, import/export -- goes through the existing
routes in setup.py / money.py / reports.py, with their permissions unchanged.

Permissions (auth/principal.py), declared on every route:
  reads   reports:read   -- exactly owner + clerk, the console's audience; a salesman or driver gets 403.
                            Cost prices, stock value and value moved are returned to the owner only.
  prices  setup:write    -- owner only: the same permission as the existing product form (PATCH /api/products).
  counts  preview: reports:read (a clerk may prepare a count); post: settings:write (owner only) -- the same as
          the existing POST /api/stock/adjust, since posting a count IS a batch of adjustments.
Every write is a direct, permissioned action like the setup forms: audited with the signed-in user (web/deps.py
sets it) and approved_by = the caller's signature. The chat's approval cards are not involved."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from munshi.web.deps import Ctx, context

router = APIRouter(tags=["office"])
OFFICE = Path(__file__).resolve().parent.parent / "static" / "office"


# ---------------------------------------------------------------- the page
@router.get("/office", include_in_schema=False)
@router.get("/office/", include_in_schema=False)
def office_page():
    # sign-in and the role check happen in the page itself, against /api/me: the page is static and holds no data
    return FileResponse(OFFICE / "index.html", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- bodies
class PriceChangeIn(BaseModel):
    skus: list[str] = Field(min_length=1, max_length=500)
    mode: str = Field(pattern="^(set|pct|add)$")
    value: float = Field(allow_inf_nan=False, ge=-10_000_000, le=10_000_000)
    round_to: int = Field(default=1, ge=0, le=100)          # pct only: round the result to this many rupees (0 = paisa)
    reason: str = Field(default="", max_length=120)

    @field_validator("value")
    @classmethod
    def _pct_range(cls, v, info):
        if info.data.get("mode") == "pct" and not -90 <= v <= 500:
            raise ValueError("a percentage change must be between -90% and +500%")
        return v


class PriceApplyIn(PriceChangeIn):
    expected: dict[str, float] = Field(min_length=1)     # sku -> the old price the person saw at preview


class CountLine(BaseModel):
    sku: str = Field(min_length=1, max_length=40)
    counted: int = Field(ge=0, le=10_000_000, strict=True)
    system: int | None = Field(default=None, ge=0)          # post only: what the system showed at preview


class CountIn(BaseModel):
    warehouse_id: str = Field(min_length=1, max_length=20)
    lines: list[CountLine] = Field(min_length=1, max_length=2000)
    count_date: str = Field(default="", pattern="^(|\\d{4}-\\d{2}-\\d{2})$")


def _owner(c: Ctx) -> bool:
    return c.role == "owner"


# ---------------------------------------------------------------- products + prices
@router.get("/api/office/products")
def products(c: Ctx = Depends(context("reports:read"))):
    return c.repo.office_products(include_cost=_owner(c))


@router.get("/api/office/products/{sku}/price-history")
def price_history(sku: str, c: Ctx = Depends(context("reports:read"))):
    return c.repo.price_history(sku, include_cost=_owner(c))


@router.post("/api/office/prices/preview")
def price_preview(body: PriceChangeIn, c: Ctx = Depends(context("setup:write"))):
    rows = c.repo.preview_price_change(body.skus, body.mode, body.value, body.round_to)
    return {"rows": rows, "changes": sum(1 for r in rows if not r["unchanged"] and not r["error"]), "errors": sum(1 for r in rows if r["error"])}


@router.post("/api/office/prices/apply")
def price_apply(body: PriceApplyIn, c: Ctx = Depends(context("setup:write"))):
    rows = c.repo.apply_price_change(body.skus, body.mode, body.value, body.expected, body.round_to, body.reason,
                                     c.role, c.signature, who=c.principal.label, source="office" if len(body.skus) == 1 else "office: bulk")
    return {"rows": rows, "saved": sum(1 for r in rows if not r["unchanged"])}


# ---------------------------------------------------------------- stock
@router.get("/api/office/stock")
def stock(c: Ctx = Depends(context("reports:read"))):
    return c.repo.stock_matrix(include_value=_owner(c))


@router.get("/api/office/stock/moves")
def stock_moves(sku: str, warehouse_id: str = "", limit: int = 200, c: Ctx = Depends(context("reports:read"))):
    return c.repo.stock_moves_detail(sku, warehouse_id or None, max(1, min(limit, 1000)), include_value=_owner(c))


@router.post("/api/office/stock-count/preview")
def count_preview(body: CountIn, c: Ctx = Depends(context("reports:read"))):
    return c.repo.preview_stock_count(body.warehouse_id, [ln.model_dump() for ln in body.lines], include_value=_owner(c))


@router.post("/api/office/stock-count/post", status_code=201)
def count_post(body: CountIn, c: Ctx = Depends(context("settings:write"))):     # owner only, like /api/stock/adjust
    return c.repo.post_stock_count(body.warehouse_id, [ln.model_dump() for ln in body.lines], body.count_date or None, c.role, c.signature)


# ---------------------------------------------------------------- clients
@router.get("/api/office/clients")
def clients(q: str = "", include_inactive: bool = True, c: Ctx = Depends(context("reports:read"))):
    return c.repo.client_rows(q[:80], include_inactive)
