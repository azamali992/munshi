"""Money accounts, account books, transfers, cash counts, bank reconciliation, method routes, the journal (capital,
drawings, loans, assets, depreciation, openings), statements, KPIs, period close. OWNED BY STREAM B (plan §9 Stream B
REST list).

Permissions (auth/principal.py):
  books:read (owner, clerk)    /api/accounts*, account books -- redact_payroll=not principal.can("payroll:read")
  books:write (owner, clerk)   transfers, cash counts, clearing ticks, reconciliations
  finance:read (owner)         /api/finance/{pnl|balance-sheet|cash-flow|trial-balance|kpis|margins|assets|loans|verify}, general journal
  finance:write (owner)        accounts, method routes, journal, capital, drawings, loans, assets, depreciation, close / reopen,
                               reversing a transfer, posting a cash difference
Form writes carry the signed-in human (Ctx.signature) as approved_by, like the existing money routes. Every body is a
pydantic model; money is rupees at this edge (integer paisa inside)."""
from __future__ import annotations

import csv
import io
from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from munshi.domain.models import today_iso
from munshi.web.deps import Ctx, context

router = APIRouter(tags=["finance"])

DATE = "^\\d{4}-\\d{2}-\\d{2}$"
Method = Literal["cash", "bank", "jazzcash", "easypaisa", "cheque"]


def _range(start: str, end: str) -> tuple[str, str]:
    end = end or today_iso()
    return (start or end[:8] + "01"), end


def _redact(c: Ctx) -> bool:
    return not c.principal.can("payroll:read")


# ---------------------------------------------------------------- bodies
class AccountIn(BaseModel):
    kind: Literal["cash", "bank", "wallet"]
    name: str = Field(min_length=1, max_length=60)
    provider: str = Field(default="", max_length=40)
    number_last4: str = Field(default="", max_length=4, pattern="^\\d{0,4}$")
    opening_balance: float = 0.0
    opening_date: str | None = Field(default=None, pattern=DATE)


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    provider: str | None = Field(default=None, max_length=40)
    number_last4: str | None = Field(default=None, max_length=4, pattern="^\\d{0,4}$")
    active: bool | None = None
    is_default: bool | None = None


class RouteIn(BaseModel):
    method: Method
    account_id: str = Field(min_length=2, max_length=40)
    effective_from: str = Field(pattern=DATE)


class TransferIn(BaseModel):
    from_account: str = Field(min_length=2, max_length=40)
    to_account: str = Field(min_length=2, max_length=40)
    amount: float = Field(gt=0)
    on_date: str | None = Field(default=None, pattern=DATE)
    ref: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=120)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=120)


class CountIn(BaseModel):
    counted: float = Field(ge=0)
    note: str = Field(default="", max_length=120)


class ClearItem(BaseModel):
    source: Literal["ledger", "supplier_ledger", "expense", "deposit", "transfer", "journal", "salary_payment", "staff_advance", "statutory_payment"]
    source_id: str = Field(min_length=1, max_length=60)


class ClearIn(BaseModel):
    items: list[ClearItem] = Field(min_length=1, max_length=500)
    cleared_on: str = Field(pattern=DATE)
    cleared: bool = True


class ReconIn(BaseModel):
    statement_date: str = Field(pattern=DATE)
    statement_balance: float


class MoneyIn(BaseModel):
    amount: float = Field(gt=0)
    method: Method = "cash"
    account_id: str | None = Field(default=None, max_length=40)
    on_date: str | None = Field(default=None, pattern=DATE)
    note: str = Field(default="", max_length=200)


class LoanIn(BaseModel):
    lender: str = Field(min_length=2, max_length=80)
    kind: Literal["bank", "informal", "family", "other"] = "informal"
    amount: float = Field(gt=0)
    method: Method = "bank"
    account_id: str | None = Field(default=None, max_length=40)
    on_date: str | None = Field(default=None, pattern=DATE)
    terms: str = Field(default="", max_length=200)


class RepayIn(BaseModel):
    principal: float = Field(default=0, ge=0)
    interest: float = Field(default=0, ge=0)
    method: Method = "bank"
    account_id: str | None = Field(default=None, max_length=40)
    on_date: str | None = Field(default=None, pattern=DATE)


class AssetIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    category: Literal["vehicle", "building", "land", "furniture", "equipment", "computer", "other"]
    cost: float = Field(gt=0)
    acquired_on: str = Field(pattern=DATE)
    life_months: int = Field(ge=0, le=1200)
    salvage: float = Field(default=0, ge=0)
    funded_by: Literal["paid", "payable", "opening"] = "paid"
    method: Method | None = None
    account_id: str | None = Field(default=None, max_length=40)
    vehicle_id: str | None = Field(default=None, max_length=40)


class DisposeIn(BaseModel):
    on_date: str = Field(pattern=DATE)
    proceeds: float = Field(default=0, ge=0)
    method: Method = "cash"
    account_id: str | None = Field(default=None, max_length=40)


class DepreciationIn(BaseModel):
    through_period: str = Field(pattern="^\\d{4}-\\d{2}$")


class JournalLineIn(BaseModel):
    code: str = Field(min_length=4, max_length=40)
    debit: float = Field(default=0, ge=0)
    credit: float = Field(default=0, ge=0)
    account_id: str | None = Field(default=None, max_length=40)
    party_kind: Literal["customer", "supplier", "employee", "loan", "asset", "owner"] | None = None
    party_id: str | None = Field(default=None, max_length=60)
    memo: str = Field(default="", max_length=120)


class JournalIn(BaseModel):
    entry_date: str = Field(pattern=DATE)
    kind: Literal["adjustment", "bank_charge"] = "adjustment"
    memo: str = Field(min_length=3, max_length=200)
    lines: list[JournalLineIn] = Field(min_length=2, max_length=50)


class OpeningMoney(BaseModel):
    account_id: str = Field(min_length=2, max_length=40)
    amount: float


class OpeningAsset(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    category: Literal["vehicle", "building", "land", "furniture", "equipment", "computer", "other"] = "other"
    cost: float = Field(gt=0)
    life_months: int = Field(ge=0, le=1200)
    acquired_on: str | None = Field(default=None, pattern=DATE)
    salvage: float = Field(default=0, ge=0)
    accumulated_depreciation: float = Field(default=0, ge=0)
    vehicle_id: str | None = Field(default=None, max_length=40)


class OpeningLoan(BaseModel):
    lender: str = Field(min_length=2, max_length=80)
    kind: Literal["bank", "informal", "family", "other"] = "informal"
    amount: float = Field(gt=0)
    terms: str = Field(default="", max_length=200)


class OpeningIn(BaseModel):
    as_of: str = Field(pattern=DATE)
    money: list[OpeningMoney] = Field(default_factory=list, max_length=50)
    assets: list[OpeningAsset] = Field(default_factory=list, max_length=200)
    loans: list[OpeningLoan] = Field(default_factory=list, max_length=50)
    capital_label: str = Field(default="Opening balance equity", max_length=120)


class CloseIn(BaseModel):
    through_date: str = Field(pattern=DATE)
    note: str = Field(default="", max_length=200)
    force: bool = False


# ---------------------------------------------------------------- money accounts (books:read / finance:write)
@router.get("/api/accounts")
def accounts(include_inactive: bool = False, c: Ctx = Depends(context("books:read"))):
    return c.repo.list_money_accounts(include_inactive)


@router.post("/api/accounts", status_code=201)
def add_account(body: AccountIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.add_money_account(body.kind, body.name, body.provider, body.number_last4, body.opening_balance, body.opening_date, c.role, c.signature)


@router.get("/api/accounts/method-routes")
def method_routes(on_date: str = "", c: Ctx = Depends(context("books:read"))):
    return c.repo.method_routes(on_date or None)


@router.put("/api/accounts/method-routes")
def set_route(body: RouteIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.set_method_route(body.method, body.account_id, body.effective_from, c.role, c.signature)


@router.post("/api/accounts/transfer", status_code=201)
def transfer(body: TransferIn, c: Ctx = Depends(context("books:write"))):
    return c.repo.transfer(body.from_account, body.to_account, body.amount, body.on_date, body.ref, body.note, c.role, c.signature)


@router.post("/api/accounts/transfers/{transfer_id}/reverse", status_code=201)
def reverse_transfer(transfer_id: str, body: ReasonIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.reverse_transfer(transfer_id, body.reason, c.role, c.signature)


@router.patch("/api/accounts/{account_id}")
def patch_account(account_id: str, body: AccountPatch, c: Ctx = Depends(context("finance:write"))):
    return c.repo.update_money_account(account_id, body.model_dump(exclude_none=True), c.role, c.signature)


@router.get("/api/accounts/{account_id}/book")
def account_book(account_id: str, start: str = "", end: str = "", c: Ctx = Depends(context("books:read"))):
    return c.repo.account_book(account_id, *_range(start, end), redact_payroll=_redact(c))


@router.post("/api/accounts/{account_id}/cash-count", status_code=201)
def cash_count(account_id: str, body: CountIn, c: Ctx = Depends(context("books:write"))):
    return c.repo.count_cash(account_id, body.counted, body.note, c.role)


@router.post("/api/cash-counts/{count_id}/post", status_code=201)
def post_cash_difference(count_id: str, c: Ctx = Depends(context("finance:write"))):
    return c.repo.post_cash_difference(count_id, c.role, c.signature)


@router.get("/api/accounts/{account_id}/reconciliation")
def reconciliation(account_id: str, statement_date: str = "", c: Ctx = Depends(context("books:read"))):
    return c.repo.reconciliation(account_id, statement_date or today_iso(), redact_payroll=_redact(c))


@router.post("/api/accounts/{account_id}/clear")
def clear(account_id: str, body: ClearIn, c: Ctx = Depends(context("books:write"))):
    return c.repo.mark_cleared(account_id, [i.model_dump() for i in body.items], body.cleared_on, body.cleared, c.role)


@router.post("/api/accounts/{account_id}/reconciliations", status_code=201)
def save_reconciliation(account_id: str, body: ReconIn, c: Ctx = Depends(context("books:write"))):
    return c.repo.save_reconciliation(account_id, body.statement_date, body.statement_balance, c.role, redact_payroll=_redact(c))


# ---------------------------------------------------------------- journal writes (finance:write)
@router.post("/api/finance/capital", status_code=201)
def capital(body: MoneyIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.record_capital(body.amount, body.method, body.account_id, body.on_date, body.note, c.role, c.signature)


@router.post("/api/finance/drawings", status_code=201)
def drawings(body: MoneyIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.record_drawing(body.amount, body.method, body.account_id, body.on_date, body.note, c.role, c.signature)


@router.post("/api/finance/loans", status_code=201)
def add_loan(body: LoanIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.add_loan(body.lender, body.kind, body.amount, body.method, body.account_id, body.on_date, body.terms, c.role, c.signature)


@router.post("/api/finance/loans/{loan_id}/repay", status_code=201)
def repay_loan(loan_id: str, body: RepayIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.repay_loan(loan_id, body.principal, body.interest, body.method, body.account_id, body.on_date, c.role, c.signature)


@router.post("/api/finance/assets", status_code=201)
def add_asset(body: AssetIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.add_fixed_asset(body.name, body.category, body.cost, body.acquired_on, body.life_months, body.salvage, body.funded_by,
                                  body.method, body.account_id, body.vehicle_id, c.role, c.signature)


@router.post("/api/finance/assets/{asset_id}/dispose", status_code=201)
def dispose_asset(asset_id: str, body: DisposeIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.dispose_fixed_asset(asset_id, body.on_date, body.proceeds, body.method, body.account_id, c.role, c.signature)


@router.post("/api/finance/depreciation/run", status_code=201)
def run_depreciation(body: DepreciationIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.run_depreciation(body.through_period, c.role, c.signature)


@router.post("/api/finance/journal", status_code=201)
def post_journal(body: JournalIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.post_journal(body.entry_date, body.kind, body.memo, [ln.model_dump() for ln in body.lines], actor=c.role, approved_by=c.signature)


@router.get("/api/finance/journal/{je_id}")
def journal_entry(je_id: str, c: Ctx = Depends(context("finance:read"))):
    return c.repo.journal_entry(je_id)


@router.post("/api/finance/journal/{je_id}/reverse", status_code=201)
def reverse_journal(je_id: str, body: ReasonIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.reverse_journal(je_id, body.reason, c.role, c.signature)


@router.post("/api/finance/opening-balances", status_code=201)
def opening_balances(body: OpeningIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.record_opening_balances(body.as_of, [m.model_dump() for m in body.money], [a.model_dump() for a in body.assets],
                                          [ln.model_dump() for ln in body.loans], body.capital_label, c.role, c.signature)


# ---------------------------------------------------------------- statements (finance:read)
@router.get("/api/finance/pnl")
def pnl(start: str = "", end: str = "", compare: bool = True, c: Ctx = Depends(context("finance:read"))):
    return c.repo.income_statement(*_range(start, end), compare=compare, redact_payroll=_redact(c))


@router.get("/api/finance/balance-sheet")
def balance_sheet(as_of: str = "", c: Ctx = Depends(context("finance:read"))):
    return c.repo.balance_sheet(as_of or None)


@router.get("/api/finance/cash-flow")
def cash_flow(start: str = "", end: str = "", c: Ctx = Depends(context("finance:read"))):
    return c.repo.cash_flow(*_range(start, end))


@router.get("/api/finance/trial-balance")
def trial_balance(as_of: str = "", c: Ctx = Depends(context("finance:read"))):
    return c.repo.trial_balance(as_of or None)


@router.get("/api/finance/kpis")
def kpis(as_of: str = "", c: Ctx = Depends(context("finance:read"))):
    return c.repo.owner_kpis(as_of or None)


@router.get("/api/finance/margins")
def margins(by: Literal["product", "customer", "route"] = "product", start: str = "", end: str = "", c: Ctx = Depends(context("finance:read"))):
    return c.repo.margins(by, *_range(start, end))


@router.get("/api/finance/assets")
def assets(as_of: str = "", c: Ctx = Depends(context("finance:read"))):
    return c.repo.fixed_assets_register(as_of or None)


@router.get("/api/finance/loans")
def loans(as_of: str = "", c: Ctx = Depends(context("finance:read"))):
    return c.repo.loans_report(as_of or None)


@router.get("/api/finance/verify")
def verify(as_of: str = "", c: Ctx = Depends(context("finance:read"))):
    return c.repo.verify_books(as_of or None)


@router.get("/api/finance/general-journal")
def general_journal(start: str = "", end: str = "", c: Ctx = Depends(context("finance:read"))):
    return c.repo.general_journal(*_range(start, end))


@router.get("/api/finance/general-journal.csv")
def general_journal_csv(start: str = "", end: str = "", c: Ctx = Depends(context("finance:read"))):
    s, e = _range(start, end)
    gj = c.repo.general_journal(s, e)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "doc", "source", "code", "account", "party", "debit", "credit", "memo"])
    for r in gj["rows"]:
        w.writerow([r["date"], r["doc"], r["source"], r["code"], _csv_safe(r["account"]), _csv_safe(r["party"]),
                    f"{r['debit']:.2f}" if r["debit"] else "", f"{r['credit']:.2f}" if r["credit"] else "", _csv_safe(r["memo"])])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="general-journal-{s}-{e}.csv"'})


def _csv_safe(v: str) -> str:
    """No formula injection when the accountant opens the file in Excel."""
    v = v or ""
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


# ---------------------------------------------------------------- periods
@router.get("/api/finance/periods")
def periods(c: Ctx = Depends(context("books:read"))):
    return c.repo.period_status()


@router.post("/api/finance/periods/close", status_code=201)
def close_period(body: CloseIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.close_period(body.through_date, body.note, body.force, c.role, c.signature)


@router.post("/api/finance/periods/{close_id}/reopen")
def reopen_period(close_id: int, body: ReasonIn, c: Ctx = Depends(context("finance:write"))):
    return c.repo.reopen_period(close_id, body.reason, c.role, c.signature)

