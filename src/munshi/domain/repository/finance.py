"""Company finance: money accounts, method routes, transfers, cash counts, bank reconciliation, the journal (capital,
drawings, loans, fixed assets, depreciation, openings, general entries), period close and lock. OWNED BY STREAM B
(plan §3.4, §4.1, §4.4, §9 Stream B). The derived ledger lives in ledger_projection.py, the statements in
finance_reports.py; this module holds the writes and the per-account reads (books, reconciliation).

Contracts other streams consume (signatures frozen by SEAMS.md):
  * assert_period_open(on_date) -> None: StateError when on_date falls on or before the books lock. Every dated write
    here calls it; so do cash.record_expense and Stream A's payroll writers. V8 triggers back it up in the database.
  * resolve_account(method, account_id=None, on_date=None) -> str: the explicit account, else the method route in
    force on on_date (today by default), else accounts.UNASSIGNED_ACCOUNT_ID -- never None.
The owner's decisions (full text in domain/accounts.py):
  * decision 2: account_book(..., redact_payroll=True) -- for callers without payroll:read -- aggregates salary,
    advance and statutory lines per day under accounts.PAYROLL_REDACTED_LABEL: no employee name, no single salary.
  * decision 3 (accrual): postings() maps payroll via A's _payroll_postings(); salary payments clear 2100.
Every gated write: actor + approved_by, EXACTLY ONE audit row named accounts.AUDIT_ACTION[tool], immediate_tx,
integer paisa inside and rupees at the edge. History is append-only: a mistake is a reversal (dated today), never an
edit. Reversals, close and reopen require a named approver (like the existing reversals in cash.py)."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta

from munshi.domain import accounts as A
from munshi.domain.accounts import Posting, col, signed, table
from munshi.domain.models import now_iso, to_paisa, to_rupees, today_iso
from munshi.domain.repository.base import NotFoundError, StateError, new_id
from munshi.domain.repository.finance_reports import FinanceReportsMixin
from munshi.domain.repository.guarded import immediate_tx
from munshi.domain.repository.ledger_projection import MAPPING_VERSION, PAYROLL_SOURCES
from munshi.domain.repository.numbering import next_doc_no

# which account kinds a payment method may land in: cash is the drawer; everything else is a bank or a wallet
METHOD_ACCOUNT_KINDS = {"cash": ("cash",), "bank": ("bank", "wallet"), "cheque": ("bank", "wallet"),
                        "jazzcash": ("wallet", "bank"), "easypaisa": ("wallet", "bank")}
# codes a hand-posted journal entry may NOT touch: each is the summary of an operational subledger (the khata, the
# supplier khata, the stock pool, the payroll tables) or a clearing / derived account. Posting there would make the
# books disagree with the subledger; the fix belongs where the thing lives.
SUBLEDGER_CODES = frozenset({A.DRIVER_CASH_IN_TRANSIT, A.TRADE_RECEIVABLES, A.STAFF_ADVANCES, A.STOCK_GODOWNS, A.STOCK_ON_VEHICLES,
                             A.TRADE_PAYABLES, A.GOODS_RECEIVED_NOT_BILLED, A.SALARIES_PAYABLE, A.INCOME_TAX_WITHHELD, A.EOBI_PAYABLE,
                             A.SOCIAL_SECURITY_PAYABLE, A.STAFF_WELFARE_FUND, A.RETAINED_EARNINGS, A.SALES, A.SALES_RETURNS,
                             A.COST_OF_GOODS_SOLD, A.SALARIES, A.WAGES, A.ALLOWANCES, A.COMMISSION, A.BONUS, A.EMPLOYER_CONTRIBUTIONS})
GENERAL_JOURNAL_KINDS = ("adjustment", "bank_charge")
ASSET_CATEGORIES = ("vehicle", "building", "land", "furniture", "equipment", "computer", "other")
LOAN_KINDS = ("bank", "informal", "family", "other")
CLEARING_SOURCES = ("ledger", "supplier_ledger", "expense", "deposit", "transfer", "journal", "salary_payment", "staff_advance", "statutory_payment")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PERIOD = re.compile(r"^\d{4}-\d{2}$")


def _date(d: str | None, what: str = "date") -> str:
    d = (d or "").strip() or today_iso()
    if not _DATE.match(d):
        raise ValueError(f"{what} must be YYYY-MM-DD, got {d!r}")
    date.fromisoformat(d)
    return d


def _positive(amount, what: str = "amount") -> int:
    p = to_paisa(amount)
    if p <= 0:
        raise ValueError(f"{what} must be positive")
    return p


def _reason(reason: str) -> str:
    r = (reason or "").strip()
    if len(r) < 3:
        raise ValueError("a reversal or reopen needs a reason (at least 3 characters)")
    return r[:120]


def _approver(approved_by: str | None, what: str = "this") -> str:
    if not (approved_by or "").strip():
        raise StateError(f"{what} changes the books: it must be approved by the owner")
    return approved_by


def _month_end(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return (nxt - timedelta(days=1)).isoformat()


def _months(first: str, last: str) -> list[str]:
    out, y, m = [], int(first[:4]), int(first[5:7])
    while f"{y:04d}-{m:02d}" <= last:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _depreciation_schedule(cost_p: int, salvage_p: int, opening_acc_p: int, life: int, acquired_on: str, start_period: str) -> dict[str, int]:
    """Straight line, full-month convention: the depreciable amount left at start_period spread evenly over the months
    of life left; the last month takes the rounding remainder, so the schedule sums to it exactly."""
    if life <= 0:
        return {}
    elapsed = len(_months(acquired_on[:7], start_period)) - 1
    n = max(1, life - elapsed)
    left = cost_p - salvage_p - opening_acc_p
    if left <= 0:
        return {}
    base, rem = divmod(left, n)
    y, m = int(start_period[:4]), int(start_period[5:7])
    out = {}
    for i in range(n):
        out[f"{y:04d}-{m:02d}"] = base + (rem if i == n - 1 else 0)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


class FinanceMixin(FinanceReportsMixin):
    # ================================================================== period lock and account resolution (consumed by A, cash.py)
    def _books_lock(self) -> str | None:
        r = self._one("SELECT through_date FROM books_lock")
        d = r["through_date"] if r else None
        return None if not d or d == "0000-00-00" else d

    def assert_period_open(self, on_date: str) -> None:
        """Raise StateError if on_date falls on or before the books lock (a closed period)."""
        lock = self._books_lock()
        if lock and (on_date or "") <= lock:
            raise StateError(f"books are closed through {lock}: post it in an open period or ask the owner to reopen")

    def resolve_account(self, method: str, account_id: str | None = None, on_date: str | None = None) -> str:
        """Which money account a payment by `method` lands in: the explicit account, else the method route in force on
        on_date (today by default), else UNASSIGNED (chart 1900; every report flags it). Never None."""
        if account_id:
            return account_id
        return self._routed(self._route_table(), method or "cash", on_date or today_iso())

    def _money_account(self, account_id: str) -> dict:
        r = self._one("SELECT * FROM money_accounts WHERE account_id=?", (account_id,))
        if not r:
            raise NotFoundError(f"no such money account: {account_id}")
        return dict(r)

    def _check_account_for(self, method: str, account_id: str) -> str:
        """An explicit account on a payment: it exists, is active, and suits the method (cash lands in a cash account)."""
        acct = self._money_account(account_id)
        if not acct["active"]:
            raise StateError(f"money account {account_id} is closed; use an active account")
        kinds = METHOD_ACCOUNT_KINDS.get(method or "cash")
        if kinds is None:
            raise ValueError(f"method {method!r} cannot name a money account")
        if acct["kind"] not in kinds:
            raise ValueError(f"a {method} payment cannot land in {acct['name']} ({acct['kind']}); pick a {' or '.join(kinds)} account")
        return account_id

    def _real_account(self, method: str, account_id: str | None, on: str) -> str:
        """The account a finance write moves money through: explicit (checked) or routed -- never UNASSIGNED."""
        if account_id:
            return self._check_account_for(method, account_id)
        if method not in A.MONEY_METHODS:
            raise ValueError(f"method must be one of {', '.join(A.MONEY_METHODS)}")
        acct = self.resolve_account(method, None, on)
        if acct == A.UNASSIGNED_ACCOUNT_ID:
            raise StateError(f"no money account takes {method} payments yet: add the account or route {method} to one first")
        return acct

    # ================================================================== money accounts and routes
    def _account_names(self) -> dict[str, dict]:
        return {r["account_id"]: dict(r) for r in self._all("SELECT * FROM money_accounts ORDER BY created_at, account_id")}

    def _new_account_id(self, basis: str) -> str:
        slug = re.sub(r"[^A-Z0-9]+", "-", basis.strip().upper()).strip("-")[:12] or "ACCOUNT"
        aid, n = f"ACC-{slug}", 2
        while self._one("SELECT 1 FROM money_accounts WHERE account_id=?", (aid,)):
            aid, n = f"ACC-{slug}-{n}", n + 1
        return aid

    def add_money_account(self, kind: str, name: str, provider: str = "", number_last4: str = "", opening_balance: float = 0.0,
                          opening_date: str | None = None, actor: str = "", approved_by: str | None = None) -> dict:
        """A cash drawer, bank account or wallet. An opening balance is a journal entry (Dr the account / Cr 3010)."""
        if kind not in A.MONEY_ACCOUNT_KINDS:
            raise ValueError(f"kind must be one of {', '.join(A.MONEY_ACCOUNT_KINDS)}")
        name = (name or "").strip()
        if not 1 <= len(name) <= 60:
            raise ValueError("an account needs a name (up to 60 characters)")
        last4 = re.sub(r"\D", "", number_last4 or "")[-4:]
        opening_p = to_paisa(opening_balance or 0)
        on = _date(opening_date, "opening date") if (opening_date or opening_p) else None
        with immediate_tx(self) as c:
            if on and opening_p:
                self.assert_period_open(on)
            aid = self._new_account_id(provider or name)
            default = 0 if self._one("SELECT 1 FROM money_accounts WHERE kind=? AND is_default=1", (kind,)) else 1
            c.execute("INSERT INTO money_accounts (account_id, kind, name, provider, number_last4, opening_date, is_default, active, created_at) "
                      "VALUES (?,?,?,?,?,?,?,1,?)", (aid, kind, name, (provider or "").strip()[:40], last4, on, default, now_iso()))
            je = None
            if opening_p:
                je = self._post_je(c, on, "opening", f"Opening balance of {name}"[:200],
                                   signed(on, A.MONEY, A.OPENING_BALANCE_EQUITY, opening_p, debit_money_account_id=aid),
                                   "money_account", aid, actor, approved_by)
            self.audit(actor, A.AUDIT_ACTION["add_money_account"], "money_account", aid,
                       {"kind": kind, "name": name, "provider": provider, "opening_balance": to_rupees(opening_p), "journal": je}, approved_by)
        return self._account_row(aid) | {"opening_journal": je}

    def update_money_account(self, account_id: str, changes: dict, actor: str, approved_by: str | None = None) -> dict:
        """Rename, change provider / last 4, make default for its kind, or close (active=False). Id and kind never change."""
        allowed = {"name", "provider", "number_last4", "active", "is_default"}
        bad = set(changes) - allowed
        if bad:
            raise ValueError(f"cannot change {', '.join(sorted(bad))} on a money account")
        with immediate_tx(self) as c:
            acct = self._money_account(account_id)
            sets: dict = {}
            if "name" in changes:
                n = str(changes["name"] or "").strip()
                if not 1 <= len(n) <= 60: raise ValueError("an account needs a name (up to 60 characters)")
                sets["name"] = n
            if "provider" in changes: sets["provider"] = str(changes["provider"] or "").strip()[:40]
            if "number_last4" in changes: sets["number_last4"] = re.sub(r"\D", "", str(changes["number_last4"] or ""))[-4:]
            if "active" in changes and not bool(changes["active"]) and acct["active"]:
                if account_id == A.CASH_ACCOUNT_ID:
                    raise StateError("the cash drawer (CASH) is where every cash entry lands; it cannot be closed")
                routed = [m for m in A.MONEY_METHODS if self.resolve_account(m) == account_id]
                if routed:
                    raise StateError(f"{', '.join(routed)} payments still land in {acct['name']}; route them elsewhere first")
                sets["active"] = 0
                sets["is_default"] = 0
            elif "active" in changes and bool(changes["active"]):
                sets["active"] = 1
            if changes.get("is_default"):
                if not (sets.get("active", acct["active"])):
                    raise StateError("a closed account cannot be the default")
                c.execute("UPDATE money_accounts SET is_default=0 WHERE kind=? AND account_id<>?", (acct["kind"], account_id))
                sets["is_default"] = 1
            if sets:
                c.execute(f"UPDATE money_accounts SET {', '.join(k + '=?' for k in sets)} WHERE account_id=?", (*sets.values(), account_id))
            self.audit(actor, "update_money_account", "money_account", account_id, {"changes": sets}, approved_by)
        return self._account_row(account_id)

    def _account_row(self, account_id: str) -> dict:
        a = self._money_account(account_id)
        return {"account_id": a["account_id"], "kind": a["kind"], "name": a["name"], "provider": a["provider"],
                "number_last4": a["number_last4"], "opening_date": a["opening_date"], "is_default": bool(a["is_default"]),
                "active": bool(a["active"]), "balance": to_rupees(self.account_balance_paisa(account_id))}

    def method_routes(self, on_date: str | None = None) -> dict:
        """The route of each method in force on on_date (today), and the full dated history."""
        on = _date(on_date)
        names = self._account_names()
        history = [dict(r) for r in self._all("SELECT route_id, method, account_id, effective_from, set_by, set_at FROM method_routes ORDER BY method, effective_from, route_id")]
        current = []
        for m in A.MONEY_METHODS:
            acct = self.resolve_account(m, None, on)
            current.append({"method": m, "account_id": acct, "account": names.get(acct, {}).get("name", "Unassigned (no account yet)")})
        t = table("Where each payment method lands", [col("method", "Method"), col("account", "Account"), col("account_id", "Id")], current,
                  note="A method with no account lands in Unassigned money; add the account and route the method to it.")
        return {"as_of": on, "routes": current, "history": history, "table": t}

    def set_method_route(self, method: str, account_id: str, effective_from: str, actor: str, approved_by: str | None) -> dict:
        """From effective_from on, `method` payments with no explicit account land in account_id. Dated and
        append-only, so history before effective_from keeps its account; refused inside a closed period."""
        if method not in A.MONEY_METHODS:
            raise ValueError(f"method must be one of {', '.join(A.MONEY_METHODS)}")
        eff = _date(effective_from, "effective_from")
        with immediate_tx(self) as c:
            self._check_account_for(method, account_id)
            self.assert_period_open(eff)
            c.execute("INSERT INTO method_routes (method, account_id, effective_from, set_by, set_at) VALUES (?,?,?,?,?)",
                      (method, account_id, eff, actor or self._current_user(), now_iso()))
            self.audit(actor, A.AUDIT_ACTION["set_method_route"], "method_route", method, {"account": account_id, "effective_from": eff}, approved_by)
        return self.method_routes(max(eff, today_iso()))

    def list_money_accounts(self, include_inactive: bool = False) -> dict:
        today = today_iso()
        bal = self._balances(today)
        money = {}
        for (code, acct, _, _), v in bal.items():
            if code == A.MONEY: money[acct] = money.get(acct, 0) + v
        unassigned = sum(v for (code, *_), v in bal.items() if code == A.UNASSIGNED_MONEY)
        rows, raw = [], []
        accounts = self._account_names()
        movements = self._money_items(None, today) if any(a["kind"] != "cash" for a in accounts.values()) else {}   # only banks and wallets clear
        for aid, a in accounts.items():
            if not a["active"] and not include_inactive and not money.get(aid):
                continue
            last = self._one("SELECT statement_date, statement_paisa, difference_paisa FROM reconciliations WHERE account_id=? ORDER BY statement_date DESC, recon_id DESC LIMIT 1", (aid,))
            unc_in = unc_out = 0
            if a["kind"] != "cash":
                for it in movements.get(aid, {}).values():
                    if it["cleared_on"]: continue
                    if it["net"] > 0: unc_in += it["net"]
                    else: unc_out -= it["net"]
            r = {"account_id": aid, "account": a["name"], "kind": a["kind"], "provider": a["provider"], "number_last4": a["number_last4"],
                 "is_default": bool(a["is_default"]), "active": bool(a["active"]), "balance_paisa": money.get(aid, 0),
                 "last_reconciled": last["statement_date"] if last else None,
                 "last_statement_balance": to_rupees(int(last["statement_paisa"])) if last else None,
                 "difference": to_rupees(int(last["difference_paisa"])) if last else None,
                 "uncleared_in": to_rupees(unc_in) if a["kind"] != "cash" else None, "uncleared_out": to_rupees(unc_out) if a["kind"] != "cash" else None}
            raw.append(r)
            rows.append(r | {"balance": to_rupees(r["balance_paisa"]), "as_of": today})
        if unassigned:
            rows.append({"account_id": A.UNASSIGNED_ACCOUNT_ID, "account": "Unassigned money (no account for its method)", "kind": "unassigned",
                         "balance": to_rupees(unassigned), "as_of": today})
        total = sum(money.values()) + unassigned
        cols = [col("account", "Account"), col("kind", "Kind"), col("balance", "Balance", "money"), col("as_of", "As of", "date"),
                col("last_reconciled", "Last reconciled", "date"), col("uncleared_in", "Uncleared in", "money"),
                col("uncleared_out", "Uncleared out", "money"), col("last_statement_balance", "Last statement", "money"),
                col("difference", "Difference", "money")]
        note = f"Rs {to_rupees(unassigned):,.2f} is in Unassigned money: route its payment method to an account." if unassigned else None
        return {"as_of": today, "accounts": [{k: v for k, v in r.items() if k != "balance_paisa"} | {"balance": to_rupees(r["balance_paisa"])} for r in raw],
                "unassigned": to_rupees(unassigned), "total": to_rupees(total),
                "table": table("Money accounts", cols, rows, totals={"balance": to_rupees(total)}, note=note)}

    def account_balance_paisa(self, account_id: str, as_of: str | None = None) -> int:
        """The book balance of a money account through as_of (today). UNASSIGNED gives the 1900 balance."""
        bal = self._balances(as_of or today_iso())
        if account_id == A.UNASSIGNED_ACCOUNT_ID:
            return sum(v for (code, *_), v in bal.items() if code == A.UNASSIGNED_MONEY)
        return sum(v for (code, acct, *_), v in bal.items() if code == A.MONEY and acct == account_id)

    # ================================================================== account book
    def _money_items(self, start: str | None, end: str | None) -> dict[str, dict[tuple, dict]]:
        """account -> (on, source, source_id) -> {net, memo, cleared_on}: every money account's movements in [start, end]."""
        cleared = {(r["account_id"], r["source"], r["source_id"]): r["cleared_on"] for r in self._all("SELECT account_id, source, source_id, cleared_on FROM bank_clearings")}
        out: dict[str, dict[tuple, dict]] = {}
        for p in self.postings(start, end, money_only=True):
            if p.code != A.MONEY:
                continue
            it = out.setdefault(p.money_account_id, {}).setdefault((p.on, p.source, p.source_id),
                                                                   {"net": 0, "memo": p.memo, "cleared_on": cleared.get((p.money_account_id, p.source, p.source_id))})
            it["net"] += p.net_paisa
        return {a: {k: v for k, v in items.items() if v["net"]} for a, items in out.items()}

    def _book_items(self, account_id: str, start: str | None, end: str | None) -> dict[tuple, dict]:
        """(on, source, source_id) -> {net, memo, cleared_on} for one money account's postings in [start, end]."""
        return self._money_items(start, end).get(account_id, {})

    def _narrations(self, keys: list[tuple]) -> dict[tuple, tuple[str, str, str]]:
        """(source, source_id) -> (kind label, narration, by) for the book's rows."""
        out: dict[tuple, tuple[str, str, str]] = {}
        by_src: dict[str, list[str]] = {}
        for s, sid in keys: by_src.setdefault(s, []).append(sid)

        def rows(sql: str, ids: list[str]):
            return self._all(sql.format(marks=",".join("?" * len(ids))), tuple(ids)) if ids else []
        custs = {c.customer_id: c.name for c in self.list_customers(include_inactive=True)}
        sups = {s.supplier_id: s.name for s in self.list_suppliers()}
        names = self._account_names()
        for r in rows("SELECT entry_id, customer_id, kind, method, received_by, reversal_of FROM ledger WHERE entry_id IN ({marks})", by_src.get("ledger", [])):
            out[("ledger", r["entry_id"])] = (f"customer {r['kind'].replace('_', ' ')}" + (" reversal" if r["reversal_of"] else "") + (f" · {r['method']}" if r["method"] else ""),
                                              custs.get(r["customer_id"], r["customer_id"]), r["received_by"] or "")
        for r in rows("SELECT entry_id, supplier_id, kind, method, reversal_of FROM supplier_ledger WHERE entry_id IN ({marks})", by_src.get("supplier_ledger", [])):
            out[("supplier_ledger", r["entry_id"])] = (f"supplier {r['kind']}" + (" reversal" if r["reversal_of"] else "") + (f" · {r['method']}" if r["method"] else ""),
                                                       sups.get(r["supplier_id"], r["supplier_id"]), "")
        for r in rows("SELECT expense_id, category, note, paid_by, reversal_of FROM expenses WHERE expense_id IN ({marks})", by_src.get("expense", [])):
            out[("expense", r["expense_id"])] = (f"expense · {r['category']}" + (" reversal" if r["reversal_of"] else ""), r["note"] or r["category"], r["paid_by"] or "")
        for r in rows("SELECT deposit_id, plan_id, counted_by FROM deposits WHERE deposit_id IN ({marks})", by_src.get("deposit", [])):
            out[("deposit", r["deposit_id"])] = ("driver hand-in", r["plan_id"] or "", r["counted_by"] or "")
        for r in rows("SELECT transfer_id, from_account, to_account, note, created_by, reversal_of FROM account_transfers WHERE transfer_id IN ({marks})", by_src.get("transfer", [])):
            what = f"{names.get(r['from_account'], {}).get('name', r['from_account'])} → {names.get(r['to_account'], {}).get('name', r['to_account'])}"
            out[("transfer", r["transfer_id"])] = ("transfer" + (" reversal" if r["reversal_of"] else ""), what + (f" ({r['note']})" if r["note"] else ""), r["created_by"] or "")
        for r in rows("SELECT je_id, kind, memo, created_by FROM journal_entries WHERE je_id IN ({marks})", by_src.get("journal", [])):
            out[("journal", r["je_id"])] = (f"journal · {r['kind'].replace('_', ' ')}", r["memo"], r["created_by"] or "")
        return out

    def account_book(self, account_id: str, start: str, end: str, redact_payroll: bool = False) -> dict:
        """One money account's book: opening, every movement in and out with a running balance, closing. For a caller
        without payroll:read (redact_payroll=True) salary, advance and statutory lines are summed per day under
        accounts.PAYROLL_REDACTED_LABEL -- no employee, no single salary (owner decision 2)."""
        start, end = _date(start, "start"), _date(end, "end")
        if start > end: raise ValueError("start must be on or before end")
        if account_id == A.UNASSIGNED_ACCOUNT_ID:
            raise ValueError("Unassigned money has no book of its own: route its method to an account (see verify_books)")
        acct = self._money_account(account_id)
        prev = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
        opening = self.account_balance_paisa(account_id, prev)
        items = self._book_items(account_id, start, end)
        labels = self._narrations([(s, sid) for (_, s, sid) in items if s not in PAYROLL_SOURCES])
        rows, bal, tin, tout = [], opening, 0, 0
        rows.append({"date": start, "doc": "", "kind": "opening", "narration": "Opening balance", "money_in": None, "money_out": None,
                     "balance": to_rupees(opening), "cleared": "", "by": "", "_em": True})
        redacted: dict[str, int] = {}
        entries = []
        for (on, source, sid), it in sorted(items.items()):
            if redact_payroll and source in PAYROLL_SOURCES:
                redacted[on] = redacted.get(on, 0) + it["net"]
                continue
            kind, narr, by = labels.get((source, sid), (source.replace("_", " "), it["memo"], ""))
            entries.append((on, sid, kind, narr, by, it["net"], it["cleared_on"]))
        for on, net in redacted.items():
            if net: entries.append((on, "", "staff payments", A.PAYROLL_REDACTED_LABEL, "", net, None))
        for on, sid, kind, narr, by, net, cleared in sorted(entries, key=lambda e: (e[0], e[1] == "", e[1])):
            bal += net
            tin += max(net, 0); tout += max(-net, 0)
            rows.append({"date": on, "doc": sid, "kind": kind, "narration": narr, "money_in": to_rupees(net) if net > 0 else None,
                         "money_out": to_rupees(-net) if net < 0 else None, "balance": to_rupees(bal),
                         "cleared": ("cleared " + cleared) if cleared else ("" if acct["kind"] == "cash" else "uncleared"), "by": by})
        rows.append({"date": end, "doc": "", "kind": "closing", "narration": "Closing balance", "money_in": None, "money_out": None,
                     "balance": to_rupees(bal), "cleared": "", "by": "", "_em": True})
        cols = [col("date", "Date", "date"), col("doc", "Doc"), col("kind", "Kind"), col("narration", "Narration"), col("money_in", "In", "money"),
                col("money_out", "Out", "money"), col("balance", "Balance", "money"), col("cleared", "Cleared", badge=True), col("by", "By")]
        t = table(f"{acct['name']} book, {start} to {end}", cols, rows, totals={"money_in": to_rupees(tin), "money_out": to_rupees(tout)})
        return {"account_id": account_id, "account": acct["name"], "kind": acct["kind"], "start": start, "end": end,
                "opening": to_rupees(opening), "money_in": to_rupees(tin), "money_out": to_rupees(tout), "closing": to_rupees(bal),
                "redacted": redact_payroll, "table": t}

    # ================================================================== transfers
    def transfer(self, from_account: str, to_account: str, amount: float, on_date: str | None = None, ref: str = "", note: str = "",
                 actor: str = "", approved_by: str | None = None) -> dict:
        """Move money between two of the business's own accounts (XFR series). Nothing is earned or spent."""
        amount_p = _positive(amount)
        on = _date(on_date)
        if from_account == to_account: raise ValueError("a transfer needs two different accounts")
        with immediate_tx(self) as c:
            for aid in (from_account, to_account):
                if not self._money_account(aid)["active"]: raise StateError(f"money account {aid} is closed")
            self.assert_period_open(on)
            created_at = now_iso()
            tid = next_doc_no(self, c, A.SERIES_TRANSFER, created_at)
            c.execute("INSERT INTO account_transfers (transfer_id, from_account, to_account, amount_paisa, transfer_date, ref, note, created_by, approved_by, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)", (tid, from_account, to_account, amount_p, on, (ref or "")[:80], (note or "")[:120],
                                                        actor or self._current_user(), approved_by, created_at))
            self.audit(actor, A.AUDIT_ACTION["transfer_between_accounts"], "account_transfer", tid,
                       {"from": from_account, "to": to_account, "amount": to_rupees(amount_p), "date": on}, approved_by)
        return self._transfer(tid)

    def _transfer(self, tid: str) -> dict:
        r = self._one("SELECT * FROM account_transfers WHERE transfer_id=?", (tid,))
        if not r: raise NotFoundError(f"no such transfer: {tid}")
        d = dict(r); d["amount"] = to_rupees(int(d.pop("amount_paisa"))); return d

    def reverse_transfer(self, transfer_id: str, reason: str, actor: str, approved_by: str | None) -> dict:
        reason, approved_by = _reason(reason), _approver(approved_by, "a reversal")
        with immediate_tx(self) as c:
            r = self._one("SELECT * FROM account_transfers WHERE transfer_id=?", (transfer_id,))
            if not r: raise NotFoundError(f"no such transfer: {transfer_id}")
            if r["reversal_of"]: raise StateError(f"{transfer_id} is itself a reversal (of {r['reversal_of']})")
            done = self._one("SELECT transfer_id FROM account_transfers WHERE reversal_of=?", (transfer_id,))
            if done: raise StateError(f"{transfer_id} was already reversed by {done['transfer_id']}")
            on = today_iso()
            self.assert_period_open(on)
            created_at = now_iso()
            rid = next_doc_no(self, c, A.SERIES_TRANSFER, created_at)
            c.execute("INSERT INTO account_transfers (transfer_id, from_account, to_account, amount_paisa, transfer_date, ref, note, created_by, approved_by, created_at, reversal_of) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (rid, r["from_account"], r["to_account"], -int(r["amount_paisa"]), on, r["ref"],
                                                          f"reversal of {transfer_id}: {reason}"[:120], actor or self._current_user(), approved_by, created_at, transfer_id))
            self.audit(actor, A.AUDIT_ACTION["reverse_account_transfer"], "account_transfer", transfer_id,
                       {"reversal": rid, "amount": to_rupees(-int(r["amount_paisa"])), "reason": reason}, approved_by)
        return self._transfer(rid)

    # ================================================================== cash counts
    def count_cash(self, account_id: str, counted: float, note: str = "", actor: str = "") -> dict:
        """Record a count of a drawer (or any account) against the book. Nothing is posted: the owner posts the
        difference (post_cash_difference) after looking at it."""
        counted_p = to_paisa(counted)
        if counted_p < 0: raise ValueError("a count cannot be negative")
        on = today_iso()
        with immediate_tx(self) as c:
            self._money_account(account_id)
            book_p = self.account_balance_paisa(account_id, on)
            cid = new_id(A.ID_PREFIX["cash_count"])
            c.execute("INSERT INTO cash_counts (count_id, account_id, counted_on, counted_paisa, book_paisa, counted_by, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
                      (cid, account_id, on, counted_p, book_p, self._current_user() or actor or "unknown", (note or "")[:120], now_iso()))
            self.audit(actor, A.AUDIT_ACTION["count_cash"], "cash_count", cid,
                       {"account": account_id, "counted": to_rupees(counted_p), "book": to_rupees(book_p), "difference": to_rupees(counted_p - book_p)})
        return self._cash_count(cid)

    def _cash_count(self, cid: str) -> dict:
        r = self._one("SELECT * FROM cash_counts WHERE count_id=?", (cid,))
        if not r: raise NotFoundError(f"no such cash count: {cid}")
        je = self._standing_je("cash_count", cid)
        return {"count_id": cid, "account_id": r["account_id"], "counted_on": r["counted_on"], "counted": to_rupees(int(r["counted_paisa"])),
                "book": to_rupees(int(r["book_paisa"])), "difference": to_rupees(int(r["counted_paisa"]) - int(r["book_paisa"])),
                "counted_by": r["counted_by"], "note": r["note"], "posted_journal": je}

    def post_cash_difference(self, count_id: str, actor: str, approved_by: str | None) -> dict:
        """Post a count's difference: over -> Dr the account / Cr 8100; short -> Dr 8100 / Cr the account."""
        with immediate_tx(self) as c:
            cc = self._cash_count(count_id)
            if cc["posted_journal"]: raise StateError(f"{count_id} was already posted ({cc['posted_journal']})")
            diff_p = to_paisa(cc["difference"])
            if diff_p == 0: raise StateError("the count matches the book: nothing to post")
            on = cc["counted_on"]
            self.assert_period_open(on)
            what = "over" if diff_p > 0 else "short"
            je = self._post_je(c, on, "cash_count", f"Cash {what} by Rs {to_rupees(abs(diff_p)):,.2f} on count {count_id}",
                               signed(on, A.MONEY, A.CASH_OVER_SHORT, diff_p, debit_money_account_id=cc["account_id"]),
                               "cash_count", count_id, actor, approved_by)
            self.audit(actor, A.AUDIT_ACTION["post_cash_difference"], "cash_count", count_id, {"journal": je, "difference": to_rupees(diff_p)}, approved_by)
        return self._cash_count(count_id)

    # ================================================================== bank reconciliation
    def mark_cleared(self, account_id: str, items: list[dict], cleared_on: str, cleared: bool, actor: str) -> dict:
        """Tick (or untick) book items as seen on the bank statement. items: [{source, source_id}]."""
        on = _date(cleared_on, "cleared_on")
        if not items: raise ValueError("tick at least one item")
        with immediate_tx(self) as c:
            acct = self._money_account(account_id)
            book = {(s, sid) for (_, s, sid) in self._book_items(account_id, None, None)}
            done = []
            for it in items:
                key = (str(it.get("source") or ""), str(it.get("source_id") or ""))
                if key[0] not in CLEARING_SOURCES: raise ValueError(f"unknown source {key[0]!r}")
                if key not in book: raise NotFoundError(f"{key[1]} is not in the {acct['name']} book")
                if cleared:
                    c.execute("INSERT OR REPLACE INTO bank_clearings (account_id, source, source_id, cleared_on, cleared_by, cleared_at) VALUES (?,?,?,?,?,?)",
                              (account_id, key[0], key[1], on, self._current_user() or actor or "unknown", now_iso()))
                else:
                    c.execute("DELETE FROM bank_clearings WHERE account_id=? AND source=? AND source_id=?", (account_id, *key))
                done.append({"source": key[0], "source_id": key[1]})
            self.audit(actor, A.AUDIT_ACTION["mark_cleared"], "money_account", account_id, {"cleared": bool(cleared), "on": on, "items": done})
        return {"account_id": account_id, "cleared": bool(cleared), "cleared_on": on, "items": done}

    def _reconcile(self, account_id: str, statement_date: str, statement_p: int | None, redact_payroll: bool = False) -> dict:
        acct = self._money_account(account_id)
        book_p = self.account_balance_paisa(account_id, statement_date)
        items = self._book_items(account_id, None, statement_date)
        labels = self._narrations([(s, sid) for (_, s, sid) in items if s not in PAYROLL_SOURCES])
        unc_in, unc_out = [], []
        for (on, source, sid), it in sorted(items.items()):
            if it["cleared_on"] and it["cleared_on"] <= statement_date:
                continue
            _, narr, _ = labels.get((source, sid), (source, it["memo"], ""))
            (unc_in if it["net"] > 0 else unc_out).append({"date": on, "source": source, "source_id": sid, "narration": narr, "amount_paisa": abs(it["net"])})
        if redact_payroll:          # owner decision 2: one line per side for staff payments, no employee, no single salary
            for side in (unc_in, unc_out):
                staff = [x for x in side if x["source"] in PAYROLL_SOURCES]
                if staff:
                    side[:] = [x for x in side if x["source"] not in PAYROLL_SOURCES] + [
                        {"date": max(x["date"] for x in staff), "source": "staff_payments", "source_id": "", "narration": A.PAYROLL_REDACTED_LABEL,
                         "amount_paisa": sum(x["amount_paisa"] for x in staff)}]
        in_p, out_p = sum(x["amount_paisa"] for x in unc_in), sum(x["amount_paisa"] for x in unc_out)
        adjusted = book_p + out_p - in_p
        diff = None if statement_p is None else statement_p - adjusted
        rows = [{"line": "Balance per books", "amount": to_rupees(book_p), "_em": True}]
        rows += [{"line": f"Add: not yet cleared {x['source_id']} ({x['narration']})".replace("  ", " "), "amount": to_rupees(x["amount_paisa"])} for x in unc_out]
        rows += [{"line": f"Less: not yet credited {x['source_id']} ({x['narration']})".replace("  ", " "), "amount": to_rupees(-x["amount_paisa"])} for x in unc_in]
        rows.append({"line": "Adjusted balance (should equal the statement)", "amount": to_rupees(adjusted), "_em": True})
        if statement_p is not None:
            rows.append({"line": "Balance per statement", "amount": to_rupees(statement_p)})
            rows.append({"line": "Difference", "amount": to_rupees(diff), "_em": True})
        t = table(f"{acct['name']} reconciliation at {statement_date}", [col("line", "Line"), col("amount", "Amount", "money")], rows,
                  note=None if diff in (None, 0) else "The difference is not zero: look for an entry missing from the books or the statement.")
        clean = lambda xs: [{k: v for k, v in x.items() if k != "amount_paisa"} | {"amount": to_rupees(x["amount_paisa"])} for x in xs]  # noqa: E731
        return {"account_id": account_id, "statement_date": statement_date, "book": to_rupees(book_p),
                "uncleared_in": clean(unc_in), "uncleared_out": clean(unc_out), "uncleared_in_total": to_rupees(in_p), "uncleared_out_total": to_rupees(out_p),
                "adjusted": to_rupees(adjusted), "statement": None if statement_p is None else to_rupees(statement_p),
                "difference": None if diff is None else to_rupees(diff), "table": t,
                "_p": (book_p, in_p, out_p, diff)}

    def reconciliation(self, account_id: str, statement_date: str, redact_payroll: bool = False) -> dict:
        """The reconciliation at statement_date, using the statement balance saved for that date (if any).
        redact_payroll (a caller without payroll:read): uncleared staff payments are one line per side."""
        on = _date(statement_date, "statement_date")
        saved = self._one("SELECT statement_paisa FROM reconciliations WHERE account_id=? AND statement_date=? ORDER BY recon_id DESC LIMIT 1", (account_id, on))
        out = self._reconcile(account_id, on, int(saved["statement_paisa"]) if saved else None, redact_payroll)
        out.pop("_p")
        return out

    def save_reconciliation(self, account_id: str, statement_date: str, statement_balance: float, actor: str, redact_payroll: bool = False) -> dict:
        """Record the statement balance at statement_date against the books (the difference must be 0.00)."""
        on = _date(statement_date, "statement_date")
        st_p = to_paisa(statement_balance)
        with immediate_tx(self) as c:
            out = self._reconcile(account_id, on, st_p, redact_payroll)
            book_p, in_p, out_p, diff = out.pop("_p")
            c.execute("INSERT INTO reconciliations (account_id, statement_date, statement_paisa, book_paisa, uncleared_in_paisa, uncleared_out_paisa, difference_paisa, done_by, done_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (account_id, on, st_p, book_p, in_p, out_p, diff, self._current_user() or actor or "unknown", now_iso()))
            out["recon_id"] = c.lastrowid
            self.audit(actor, A.AUDIT_ACTION["save_reconciliation"], "money_account", account_id,
                       {"statement_date": on, "statement": to_rupees(st_p), "book": to_rupees(book_p), "difference": to_rupees(diff)})
        return out

    # ================================================================== the journal
    def _post_je(self, c, entry_date: str, kind: str, memo: str, lines: list[Posting], source: str | None, source_id: str | None,
                 actor: str, approved_by: str | None, period: str | None = None, reversal_of: str | None = None) -> str:
        """Insert one balanced journal entry (lines first, the header last: the header's trigger proves the balance).
        Called inside the caller's immediate_tx; the caller has checked the period lock."""
        if len(lines) < 2 or sum(p.net_paisa for p in lines) != 0:
            raise ValueError("a journal entry needs at least two lines that balance")
        memo = (memo or "").strip()
        if not 3 <= len(memo) <= 200: raise ValueError("a journal entry needs a memo of 3 to 200 characters")
        created_at = now_iso()
        je = next_doc_no(self, c, A.SERIES_JOURNAL, created_at)
        for p in lines:
            if p.on != entry_date: raise ValueError("every line of an entry carries the entry's date")
            c.execute("INSERT INTO journal_lines (je_id, account_code, money_account_id, party_kind, party_id, debit_paisa, credit_paisa, memo) VALUES (?,?,?,?,?,?,?,?)",
                      (je, p.code, p.money_account_id, p.party_kind, p.party_id, p.debit_paisa, p.credit_paisa, (p.memo or "")[:120]))
        c.execute("INSERT INTO journal_entries (je_id, entry_date, kind, memo, source, source_id, period, created_by, approved_by, created_at, reversal_of) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (je, entry_date, kind, memo, source, source_id, period, actor or self._current_user(), approved_by, created_at, reversal_of))
        return je

    def _standing_je(self, source: str, source_id: str, kind: str | None = None, period: str | None = None) -> str | None:
        """The latest entry from (source, source_id) [of kind, for period] that has not been reversed."""
        q = ("SELECT e.je_id FROM journal_entries e WHERE e.source=? AND e.source_id=? AND e.reversal_of IS NULL "
             "AND NOT EXISTS (SELECT 1 FROM journal_entries r WHERE r.reversal_of = e.je_id)")
        a: list = [source, source_id]
        if kind: q += " AND e.kind=?"; a.append(kind)
        if period: q += " AND e.period=?"; a.append(period)
        r = self._one(q + " ORDER BY e.created_at DESC, e.rowid DESC LIMIT 1", tuple(a))
        return r["je_id"] if r else None

    def journal_entry(self, je_id: str) -> dict:
        e = self._one("SELECT * FROM journal_entries WHERE je_id=?", (je_id,))
        if not e: raise NotFoundError(f"no such journal entry: {je_id}")
        lines = [{"code": r["account_code"], "account": self._code_name(r["account_code"], r["money_account_id"]), "account_id": r["money_account_id"],
                  "party_kind": r["party_kind"], "party_id": r["party_id"], "debit": to_rupees(int(r["debit_paisa"])), "credit": to_rupees(int(r["credit_paisa"])),
                  "memo": r["memo"]} for r in self._all("SELECT * FROM journal_lines WHERE je_id=? ORDER BY line_id", (je_id,))]
        rev = self._one("SELECT je_id FROM journal_entries WHERE reversal_of=?", (je_id,))
        return dict(e) | {"lines": lines, "reversed_by": rev["je_id"] if rev else None,
                          "table": table(f"{je_id} {e['kind'].replace('_', ' ')}: {e['memo']}",
                                         [col("code", "Code"), col("account", "Account"), col("debit", "Debit", "money"), col("credit", "Credit", "money"), col("memo", "Memo")],
                                         lines, totals={"debit": sum(x["debit"] for x in lines), "credit": sum(x["credit"] for x in lines)})}

    def _simple_je(self, tool: str, kind: str, on_date: str | None, lines_for, memo: str, source: str | None, source_id: str | None,
                   actor: str, approved_by: str | None, payload: dict, entity: str = "journal_entry", entity_id: str | None = None) -> dict:
        on = _date(on_date)
        with immediate_tx(self) as c:
            self.assert_period_open(on)
            lines = lines_for(on)
            je = self._post_je(c, on, kind, memo, lines, source, source_id, actor, approved_by)
            self.audit(actor, A.AUDIT_ACTION[tool], entity, entity_id or je, payload | {"journal": je}, approved_by)
        return self.journal_entry(je)

    def record_capital(self, amount: float, method: str, account_id: str | None = None, on_date: str | None = None, note: str = "",
                       actor: str = "", approved_by: str | None = None) -> dict:
        """Owner puts money in: Dr the account / Cr 3000 owner's capital."""
        amount_p = _positive(amount)
        on = _date(on_date)
        acct = self._real_account(method, account_id, on)
        return self._simple_je("record_capital", "capital", on,
                               lambda d: signed(d, A.MONEY, A.OWNER_CAPITAL, amount_p, debit_money_account_id=acct, credit_party_kind="owner", credit_party_id="owner"),
                               (note or "Capital introduced by the owner")[:200], None, None, actor, approved_by,
                               {"amount": to_rupees(amount_p), "account": acct, "method": method})

    def record_drawing(self, amount: float, method: str, account_id: str | None = None, on_date: str | None = None, note: str = "",
                       actor: str = "", approved_by: str | None = None) -> dict:
        """Owner takes money out for himself: Dr 3100 drawings / Cr the account. Not an expense."""
        amount_p = _positive(amount)
        on = _date(on_date)
        acct = self._real_account(method, account_id, on)
        return self._simple_je("record_drawing", "drawing", on,
                               lambda d: signed(d, A.DRAWINGS, A.MONEY, amount_p, credit_money_account_id=acct, debit_party_kind="owner", debit_party_id="owner"),
                               (note or "Drawings by the owner")[:200], None, None, actor, approved_by,
                               {"amount": to_rupees(amount_p), "account": acct, "method": method})

    # ------------------------------------------------------------------ loans
    def _loan(self, loan_id: str) -> dict:
        r = self._one("SELECT * FROM loans WHERE loan_id=?", (loan_id,))
        if not r: raise NotFoundError(f"no such loan: {loan_id}")
        return dict(r)

    def loan_outstanding_paisa(self, loan_id: str, as_of: str | None = None) -> int:
        q = ("SELECT COALESCE(SUM(l.credit_paisa - l.debit_paisa), 0) s FROM journal_lines l JOIN journal_entries e ON e.je_id = l.je_id "
             "WHERE l.account_code=? AND l.party_kind='loan' AND l.party_id=?")
        a: list = [A.LOANS_PAYABLE, loan_id]
        if as_of: q += " AND e.entry_date <= ?"; a.append(as_of)
        return int(self._one(q, tuple(a))["s"])

    def add_loan(self, lender: str, kind: str, amount: float, method: str, account_id: str | None = None, on_date: str | None = None,
                 terms: str = "", actor: str = "", approved_by: str | None = None) -> dict:
        """Money borrowed (bank or informal): Dr the account / Cr 2200 for this loan."""
        lender = (lender or "").strip()
        if not 2 <= len(lender) <= 80: raise ValueError("name the lender (2 to 80 characters)")
        if kind not in LOAN_KINDS: raise ValueError(f"kind must be one of {', '.join(LOAN_KINDS)}")
        amount_p = _positive(amount)
        on = _date(on_date)
        acct = self._real_account(method, account_id, on)
        with immediate_tx(self) as c:
            self.assert_period_open(on)
            lid = new_id(A.ID_PREFIX["loan"])
            c.execute("INSERT INTO loans (loan_id, lender, kind, principal_paisa, received_on, terms, created_at) VALUES (?,?,?,?,?,?,?)",
                      (lid, lender, kind, amount_p, on, (terms or "")[:200], now_iso()))
            je = self._post_je(c, on, "loan", f"Loan received from {lender}",
                               signed(on, A.MONEY, A.LOANS_PAYABLE, amount_p, debit_money_account_id=acct, credit_party_kind="loan", credit_party_id=lid),
                               "loan", lid, actor, approved_by)
            self.audit(actor, A.AUDIT_ACTION["record_loan"], "loan", lid, {"lender": lender, "kind": kind, "amount": to_rupees(amount_p), "account": acct, "journal": je}, approved_by)
        return self._loan(lid) | {"principal": to_rupees(amount_p), "journal": je, "outstanding": to_rupees(self.loan_outstanding_paisa(lid))}

    def repay_loan(self, loan_id: str, principal: float, interest: float = 0.0, method: str = "bank", account_id: str | None = None,
                   on_date: str | None = None, actor: str = "", approved_by: str | None = None) -> dict:
        """Dr 2200 principal (+ Dr 7000 interest) / Cr the account."""
        principal_p, interest_p = to_paisa(principal or 0), to_paisa(interest or 0)
        if principal_p < 0 or interest_p < 0 or principal_p + interest_p == 0: raise ValueError("repay a positive principal and/or interest")
        on = _date(on_date)
        acct = self._real_account(method, account_id, on)
        with immediate_tx(self) as c:
            loan = self._loan(loan_id)
            self.assert_period_open(on)
            left = self.loan_outstanding_paisa(loan_id)
            if principal_p > left: raise StateError(f"only Rs {to_rupees(left):,.2f} of the {loan['lender']} loan is outstanding")
            lines: list[Posting] = []
            if principal_p: lines.append(Posting(on, A.LOANS_PAYABLE, debit_paisa=principal_p, party_kind="loan", party_id=loan_id, memo="principal"))
            if interest_p: lines.append(Posting(on, A.INTEREST_AND_BANK_CHARGES, debit_paisa=interest_p, party_kind="loan", party_id=loan_id, memo="interest / markup"))
            lines.append(Posting(on, A.MONEY, credit_paisa=principal_p + interest_p, money_account_id=acct))
            je = self._post_je(c, on, "loan_repayment", f"Repayment to {loan['lender']}", lines, "loan", loan_id, actor, approved_by)
            self.audit(actor, A.AUDIT_ACTION["repay_loan"], "loan", loan_id,
                       {"principal": to_rupees(principal_p), "interest": to_rupees(interest_p), "account": acct, "journal": je}, approved_by)
        return self._loan(loan_id) | {"journal": je, "outstanding": to_rupees(self.loan_outstanding_paisa(loan_id))}

    # ------------------------------------------------------------------ fixed assets
    def _asset(self, asset_id: str) -> dict:
        r = self._one("SELECT * FROM fixed_assets WHERE asset_id=?", (asset_id,))
        if not r: raise NotFoundError(f"no such fixed asset: {asset_id}")
        return dict(r)

    def _asset_code_net(self, asset_id: str, code: str, as_of: str | None = None) -> int:
        q = ("SELECT COALESCE(SUM(l.debit_paisa - l.credit_paisa), 0) s FROM journal_lines l JOIN journal_entries e ON e.je_id = l.je_id "
             "WHERE l.account_code=? AND l.party_kind='asset' AND l.party_id=?")
        a: list = [code, asset_id]
        if as_of: q += " AND e.entry_date <= ?"; a.append(as_of)
        return int(self._one(q, tuple(a))["s"])

    def _validate_asset(self, name: str, category: str, cost_p: int, life_months, salvage_p: int) -> tuple[str, int]:
        name = (name or "").strip()
        if not 2 <= len(name) <= 80: raise ValueError("name the asset (2 to 80 characters)")
        if category not in ASSET_CATEGORIES: raise ValueError(f"category must be one of {', '.join(ASSET_CATEGORIES)}")
        if cost_p <= 0: raise ValueError("an asset's cost must be positive")
        if isinstance(life_months, bool) or int(life_months) != life_months or int(life_months) < 0: raise ValueError("life_months is a whole number of months (0 = not depreciated)")
        if not 0 <= salvage_p <= cost_p: raise ValueError("salvage value must be between 0 and the cost")
        return name, int(life_months)

    def _insert_asset(self, c, name, category, cost_p, salvage_p, acquired_on, life, vehicle_id, funded_by, dep_start, opening_acc_p) -> str:
        aid = new_id(A.ID_PREFIX["fixed_asset"])
        if vehicle_id: self.get_vehicle(vehicle_id)
        c.execute("INSERT INTO fixed_assets (asset_id, name, category, vehicle_id, cost_paisa, salvage_paisa, acquired_on, life_months, method, dep_start_period, "
                  "opening_acc_dep_paisa, funded_by, created_at) VALUES (?,?,?,?,?,?,?,?,'straight_line',?,?,?,?)",
                  (aid, name, category, vehicle_id, cost_p, salvage_p, acquired_on, life, dep_start, opening_acc_p, funded_by, now_iso()))
        return aid

    def add_fixed_asset(self, name: str, category: str, cost: float, acquired_on: str, life_months: int, salvage: float = 0.0,
                        funded_by: str = "paid", method: str | None = None, account_id: str | None = None, vehicle_id: str | None = None,
                        actor: str = "", approved_by: str | None = None) -> dict:
        """A vehicle, godown, equipment... Dr 1500 / Cr the account (paid), 2300 other payables (payable) or 3010 (opening)."""
        cost_p, salvage_p = to_paisa(cost), to_paisa(salvage or 0)
        name, life = self._validate_asset(name, category, cost_p, life_months, salvage_p)
        on = _date(acquired_on, "acquired_on")
        if funded_by not in ("paid", "payable", "opening"): raise ValueError("funded_by must be paid, payable or opening")
        acct = self._real_account(method or "cash", account_id, on) if funded_by == "paid" else None
        with immediate_tx(self) as c:
            self.assert_period_open(on)
            aid = self._insert_asset(c, name, category, cost_p, salvage_p, on, life, vehicle_id, funded_by, on[:7], 0)
            credit, ckw = {"paid": (A.MONEY, {"credit_money_account_id": acct}), "payable": (A.OTHER_PAYABLES, {}),
                           "opening": (A.OPENING_BALANCE_EQUITY, {})}[funded_by]
            je = self._post_je(c, on, "opening" if funded_by == "opening" else "asset_purchase", f"{name} acquired",
                               signed(on, A.FIXED_ASSETS_COST, credit, cost_p, debit_party_kind="asset", debit_party_id=aid, **ckw),
                               "fixed_asset", aid, actor, approved_by)
            self.audit(actor, A.AUDIT_ACTION["add_fixed_asset"], "fixed_asset", aid,
                       {"name": name, "category": category, "cost": to_rupees(cost_p), "life_months": life, "funded_by": funded_by, "account": acct, "journal": je}, approved_by)
        return self._asset_view(aid) | {"journal": je}

    def _disposal(self, asset_id: str) -> str | None:
        return self._standing_je("fixed_asset", asset_id, kind="asset_disposal")

    def dispose_fixed_asset(self, asset_id: str, on_date: str, proceeds: float, method: str, account_id: str | None = None,
                            actor: str = "", approved_by: str | None = None) -> dict:
        """Sold or scrapped: Dr 1510 depreciation to date + Dr the account (proceeds) / Cr 1500 cost; the gain (Cr) or
        loss (Dr) goes to 8000 other income. Run depreciation for the months before this first if it is due."""
        proceeds_p = to_paisa(proceeds or 0)
        if proceeds_p < 0: raise ValueError("proceeds cannot be negative")
        on = _date(on_date, "on_date")
        acct = self._real_account(method, account_id, on) if proceeds_p else None
        with immediate_tx(self) as c:
            asset = self._asset(asset_id)
            if self._disposal(asset_id): raise StateError(f"{asset['name']} was already disposed of")
            self.assert_period_open(on)
            cost_p = self._asset_code_net(asset_id, A.FIXED_ASSETS_COST)
            if cost_p <= 0: raise StateError(f"{asset['name']} carries no cost on the books (its purchase was reversed)")
            acc_p = -self._asset_code_net(asset_id, A.ACCUMULATED_DEPRECIATION)
            gain_p = proceeds_p - (cost_p - acc_p)
            lines = [Posting(on, A.FIXED_ASSETS_COST, credit_paisa=cost_p, party_kind="asset", party_id=asset_id, memo="cost out")]
            if acc_p: lines.append(Posting(on, A.ACCUMULATED_DEPRECIATION, debit_paisa=acc_p, party_kind="asset", party_id=asset_id, memo="depreciation to date"))
            if proceeds_p: lines.append(Posting(on, A.MONEY, debit_paisa=proceeds_p, money_account_id=acct, memo="proceeds"))
            if gain_p: lines.append(Posting(on, A.OTHER_INCOME, **({"credit_paisa": gain_p} if gain_p > 0 else {"debit_paisa": -gain_p}), party_kind="asset",
                                            party_id=asset_id, memo="gain on disposal" if gain_p > 0 else "loss on disposal"))
            je = self._post_je(c, on, "asset_disposal", f"{asset['name']} disposed of", lines, "fixed_asset", asset_id, actor, approved_by)
            self.audit(actor, A.AUDIT_ACTION["dispose_fixed_asset"], "fixed_asset", asset_id,
                       {"proceeds": to_rupees(proceeds_p), "gain": to_rupees(gain_p), "account": acct, "journal": je}, approved_by)
        return self._asset_view(asset_id) | {"journal": je, "gain": to_rupees(gain_p)}

    def _asset_schedule(self, a: dict) -> dict[str, int]:
        return _depreciation_schedule(int(a["cost_paisa"]), int(a["salvage_paisa"]), int(a["opening_acc_dep_paisa"]), int(a["life_months"]),
                                      a["acquired_on"], a["dep_start_period"])

    def _depreciated_periods(self, asset_id: str) -> set[str]:
        return {r["period"] for r in self._all(
            "SELECT e.period FROM journal_entries e WHERE e.kind='depreciation' AND e.source='fixed_asset' AND e.source_id=? AND e.reversal_of IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM journal_entries r WHERE r.reversal_of = e.je_id)", (asset_id,))}

    def run_depreciation(self, through_period: str, actor: str, approved_by: str | None) -> dict:
        """Straight-line depreciation for every asset, every month up to through_period (YYYY-MM) not yet posted:
        one entry per asset-month (Dr 6300 / Cr 1510), dated the month's last day. Idempotent: a second run posts
        nothing; a reversed month is posted again."""
        if not _PERIOD.match(through_period or ""): raise ValueError("through_period is YYYY-MM")
        posted, total = [], 0
        with immediate_tx(self) as c:
            for a in [dict(r) for r in self._all("SELECT * FROM fixed_assets ORDER BY acquired_on, asset_id")]:
                sched = self._asset_schedule(a)
                if not sched: continue
                disposal = self._disposal(a["asset_id"])
                last = through_period
                if disposal:
                    d_on = self._one("SELECT entry_date FROM journal_entries WHERE je_id=?", (disposal,))["entry_date"]
                    last = min(last, d_on[:7])
                if self._asset_code_net(a["asset_id"], A.FIXED_ASSETS_COST) <= 0 and not disposal:
                    continue                                                  # purchase reversed: nothing to depreciate
                done = self._depreciated_periods(a["asset_id"])
                for period, amt in sched.items():
                    if period > last or period in done: continue
                    on = _month_end(period)
                    if disposal and on > d_on: on = d_on
                    self.assert_period_open(on)
                    je = self._post_je(c, on, "depreciation", f"Depreciation {period}: {a['name']}"[:200],
                                       signed(on, A.DEPRECIATION, A.ACCUMULATED_DEPRECIATION, amt, credit_party_kind="asset", credit_party_id=a["asset_id"]),
                                       "fixed_asset", a["asset_id"], actor, approved_by, period=period)
                    posted.append({"asset_id": a["asset_id"], "name": a["name"], "period": period, "amount": to_rupees(amt), "journal": je})
                    total += amt
            self.audit(actor, A.AUDIT_ACTION["run_depreciation"], "fixed_assets", through_period,
                       {"entries": len(posted), "total": to_rupees(total), "through": through_period}, approved_by)
        t = table(f"Depreciation posted through {through_period}", [col("name", "Asset"), col("period", "Month"), col("amount", "Amount", "money"), col("journal", "Journal")],
                  posted, totals={"amount": to_rupees(total)}, note=None if posted else "Nothing was due: every month is already posted.")
        return {"through_period": through_period, "posted": posted, "total": to_rupees(total), "table": t}

    # ------------------------------------------------------------------ general entries, reversal, openings
    def post_journal(self, entry_date: str, kind: str, memo: str, lines: list[dict], source: str | None = None, source_id: str | None = None,
                     actor: str = "", approved_by: str | None = None) -> dict:
        """A balanced general entry (bank charges, an adjustment). lines: [{code, debit | credit (rupees), account_id
        (for 1000), party_kind, party_id, memo}]. Subledger accounts (khata, stock, payroll...) are refused: correct
        those where they live."""
        if kind not in GENERAL_JOURNAL_KINDS:
            raise ValueError(f"a general entry is one of {', '.join(GENERAL_JOURNAL_KINDS)}; capital, drawings, loans, assets and openings have their own actions")
        on = _date(entry_date, "entry_date")
        built: list[Posting] = []
        for ln in lines or []:
            code = str(ln.get("code") or "").strip()
            if A.base_code(code) not in A.CHART: raise ValueError(f"unknown account code {code!r}")
            if code in SUBLEDGER_CODES or A.base_code(code) in SUBLEDGER_CODES:
                raise ValueError(f"{code} {A.CHART[A.base_code(code)][0]} is kept by its own records; post the correction there")
            if code == A.LOANS_PAYABLE and not (ln.get("party_kind") == "loan" and ln.get("party_id")):
                raise ValueError("a loan line names its loan (party_kind 'loan', party_id LN-...)")
            if code == A.LOANS_PAYABLE: self._loan(str(ln["party_id"]))
            d, cr = to_paisa(ln.get("debit") or 0), to_paisa(ln.get("credit") or 0)
            if d < 0 or cr < 0 or (d == 0) == (cr == 0): raise ValueError("each line has exactly one of debit or credit, positive")
            acct = None
            if code == A.MONEY:
                acct = str(ln.get("account_id") or "")
                if not acct: raise ValueError("a money line names its account (account_id)")
                if not self._money_account(acct)["active"]: raise StateError(f"money account {acct} is closed")
            built.append(Posting(on, code, debit_paisa=d, credit_paisa=cr, money_account_id=acct, party_kind=ln.get("party_kind") or None,
                                 party_id=ln.get("party_id") or None, memo=str(ln.get("memo") or "")[:120]))
        if len(built) < 2: raise ValueError("a journal entry needs at least two lines")
        if sum(p.net_paisa for p in built) != 0:
            raise ValueError(f"debits and credits differ by Rs {to_rupees(abs(sum(p.net_paisa for p in built))):,.2f}: the entry must balance")
        with immediate_tx(self) as c:
            self.assert_period_open(on)
            je = self._post_je(c, on, kind, memo, built, source, source_id, actor, approved_by)
            self.audit(actor, A.AUDIT_ACTION["post_journal_entry"], "journal_entry", je,
                       {"kind": kind, "memo": memo, "total": to_rupees(sum(p.debit_paisa for p in built))}, approved_by)
        return self.journal_entry(je)

    def reverse_journal(self, je_id: str, reason: str, actor: str, approved_by: str | None) -> dict:
        """Cancel an entry with its exact mirror, dated today (the original stays). A reversed depreciation month is
        picked up again by the next run_depreciation."""
        reason, approved_by = _reason(reason), _approver(approved_by, "a reversal")
        with immediate_tx(self) as c:
            e = self._one("SELECT * FROM journal_entries WHERE je_id=?", (je_id,))
            if not e: raise NotFoundError(f"no such journal entry: {je_id}")
            if e["reversal_of"]: raise StateError(f"{je_id} is itself a reversal (of {e['reversal_of']}); post a fresh entry instead")
            done = self._one("SELECT je_id FROM journal_entries WHERE reversal_of=?", (je_id,))
            if done: raise StateError(f"{je_id} was already reversed by {done['je_id']}")
            if e["kind"] == "asset_purchase" or (e["kind"] == "opening" and e["source"] == "fixed_asset"):
                later = self._one("SELECT je_id FROM journal_entries x WHERE x.source='fixed_asset' AND x.source_id=? AND x.kind IN ('depreciation','asset_disposal') "
                                  "AND x.reversal_of IS NULL AND NOT EXISTS (SELECT 1 FROM journal_entries r WHERE r.reversal_of = x.je_id)", (e["source_id"],))
                if later: raise StateError(f"reverse {later['je_id']} (depreciation or disposal of that asset) first")
            if e["kind"] == "loan":
                repaid = self._one("SELECT je_id FROM journal_entries x WHERE x.source='loan' AND x.source_id=? AND x.kind='loan_repayment' AND x.reversal_of IS NULL "
                                   "AND NOT EXISTS (SELECT 1 FROM journal_entries r WHERE r.reversal_of = x.je_id)", (e["source_id"],))
                if repaid: raise StateError(f"reverse the repayment {repaid['je_id']} first")
            on = today_iso()
            self.assert_period_open(on)
            lines = [Posting(on, r["account_code"], debit_paisa=int(r["credit_paisa"]), credit_paisa=int(r["debit_paisa"]), money_account_id=r["money_account_id"],
                             party_kind=r["party_kind"], party_id=r["party_id"], memo=r["memo"])
                     for r in self._all("SELECT * FROM journal_lines WHERE je_id=? ORDER BY line_id", (je_id,))]
            rid = self._post_je(c, on, e["kind"], f"Reversal of {je_id}: {reason}"[:200], lines, e["source"], e["source_id"], actor, approved_by,
                                period=e["period"], reversal_of=je_id)
            self.audit(actor, A.AUDIT_ACTION["reverse_journal_entry"], "journal_entry", je_id, {"reversal": rid, "reason": reason}, approved_by)
        return self.journal_entry(rid)

    def record_opening_balances(self, as_of: str, money: list[dict], assets: list[dict], loans: list[dict],
                                capital_label: str = "Opening balance equity", actor: str = "", approved_by: str | None = None) -> dict:
        """Bring the books in on the day Munshi starts: money in each account, fixed assets (cost, depreciation to date,
        remaining life) and loans owed, balanced against 3010 opening balance equity -- ONE journal entry.
          money:  [{account_id, amount}]            (a negative amount is an overdraft)
          assets: [{name, category, cost, life_months, acquired_on?, salvage?, accumulated_depreciation?, vehicle_id?}]
          loans:  [{lender, kind, amount, terms?}]
        Customer and supplier openings stay on their khatas (opening_balance / supplier_opening_balance), stock on the
        stock ledger (set_stock / import): those already post to 3010."""
        on = _date(as_of, "as_of")
        lines: list[Posting] = []
        with immediate_tx(self) as c:
            self.assert_period_open(on)
            made_assets, made_loans = [], []
            for m in money or []:
                aid, amt = str(m.get("account_id") or ""), to_paisa(m.get("amount") or 0)
                if not amt: continue
                if not self._money_account(aid)["active"]: raise StateError(f"money account {aid} is closed")
                lines += signed(on, A.MONEY, A.OPENING_BALANCE_EQUITY, amt, debit_money_account_id=aid)
            for a in assets or []:
                cost_p, salvage_p, acc_p = to_paisa(a.get("cost") or 0), to_paisa(a.get("salvage") or 0), to_paisa(a.get("accumulated_depreciation") or 0)
                name, life = self._validate_asset(str(a.get("name") or ""), str(a.get("category") or "other"), cost_p, a.get("life_months", 0), salvage_p)
                acquired = _date(a.get("acquired_on") or on, "acquired_on")
                if acquired > on: raise ValueError(f"{name} cannot be acquired after the opening date")
                if not 0 <= acc_p <= cost_p - salvage_p: raise ValueError(f"{name}: accumulated depreciation must be between 0 and cost less salvage")
                start = on[:7] if (acc_p or acquired[:7] < on[:7]) else acquired[:7]
                aid = self._insert_asset(c, name, str(a.get("category") or "other"), cost_p, salvage_p, acquired, life, a.get("vehicle_id"), "opening", start, acc_p)
                lines.append(Posting(on, A.FIXED_ASSETS_COST, debit_paisa=cost_p, party_kind="asset", party_id=aid, memo=name))
                if acc_p: lines.append(Posting(on, A.ACCUMULATED_DEPRECIATION, credit_paisa=acc_p, party_kind="asset", party_id=aid, memo=f"{name}: depreciation to date"))
                lines.append(Posting(on, A.OPENING_BALANCE_EQUITY, credit_paisa=cost_p - acc_p, memo=name) if cost_p - acc_p else None)
                made_assets.append(aid)
            for ln in loans or []:
                lender, kind, amt = str(ln.get("lender") or "").strip(), str(ln.get("kind") or "informal"), to_paisa(ln.get("amount") or 0)
                if not 2 <= len(lender) <= 80: raise ValueError("name the lender (2 to 80 characters)")
                if kind not in LOAN_KINDS: raise ValueError(f"loan kind must be one of {', '.join(LOAN_KINDS)}")
                if amt <= 0: raise ValueError(f"the {lender} loan needs a positive amount")
                lid = new_id(A.ID_PREFIX["loan"])
                c.execute("INSERT INTO loans (loan_id, lender, kind, principal_paisa, received_on, terms, created_at) VALUES (?,?,?,?,?,?,?)",
                          (lid, lender, kind, amt, on, str(ln.get("terms") or "")[:200], now_iso()))
                lines += signed(on, A.OPENING_BALANCE_EQUITY, A.LOANS_PAYABLE, amt, credit_party_kind="loan", credit_party_id=lid)
                made_loans.append(lid)
            lines = [p for p in lines if p is not None]
            if not lines: raise ValueError("nothing to bring in: give at least one balance")
            # collapse the 3010 legs into one line per side, so the entry reads as the opening statement it is
            eq = sum(p.net_paisa for p in lines if p.code == A.OPENING_BALANCE_EQUITY)
            lines = [p for p in lines if p.code != A.OPENING_BALANCE_EQUITY]
            label = (capital_label or "Opening balance equity").strip()[:120]
            if eq: lines.append(Posting(on, A.OPENING_BALANCE_EQUITY, **({"debit_paisa": eq} if eq > 0 else {"credit_paisa": -eq}), memo=label))
            je = self._post_je(c, on, "opening", f"Opening balances at {on}", lines, "opening", on, actor, approved_by)
            self.audit(actor, A.AUDIT_ACTION["record_opening_balances"], "journal_entry", je,
                       {"as_of": on, "accounts": len(money or []), "assets": made_assets, "loans": made_loans, "equity": to_rupees(-eq)}, approved_by)
        return self.journal_entry(je) | {"assets": made_assets, "loans": made_loans}

    # ================================================================== periods
    def period_status(self) -> dict:
        lock = self._books_lock()
        closes = [dict(r) for r in self._all("SELECT close_id, through_date, closed_by, closed_at, note, reopened_by, reopened_at, reopen_reason FROM period_closes ORDER BY close_id DESC")]
        rows = [{"close_id": str(x["close_id"]), "through_date": x["through_date"], "closed_by": x["closed_by"], "status": "reopened" if x["reopened_at"] else "closed",
                 "note": x["reopen_reason"] if x["reopened_at"] else x["note"]} for x in closes]
        t = table("Closed periods", [col("close_id", "Close"), col("through_date", "Closed through", "date"), col("closed_by", "By"), col("status", "Status", badge=True),
                                     col("note", "Note")], rows,
                  lead=f"Books are closed through {lock}." if lock else "No period is closed yet.")
        return {"locked_through": lock, "closes": closes, "table": t}

    def _tb_snapshot(self, through: str) -> dict:
        bal = self._balances(through)
        tb: dict[str, int] = {}
        for (code, acct, _, _), v in bal.items():
            k = f"{code}|{acct}" if acct else code
            tb[k] = tb.get(k, 0) + v
        tb = {k: v for k, v in sorted(tb.items()) if v}
        body = json.dumps({"mapping_version": MAPPING_VERSION, "through_date": through, "trial_balance": tb}, sort_keys=True, separators=(",", ":"))
        return {"mapping_version": MAPPING_VERSION, "through_date": through, "trial_balance": tb, "sha256": hashlib.sha256(body.encode()).hexdigest()}

    def close_period(self, through_date: str, note: str = "", force: bool = False, actor: str = "", approved_by: str | None = None) -> dict:
        """Close the books through a date before today: every expense, journal entry, transfer, method route (and
        payroll row) dated on or before it is refused from then on. A trial-balance snapshot (JSON + sha256) is
        kept; verify_books() raises "books drift" if the numbers of a closed period ever change. Refused while
        verify_books() raises alarms for the period, unless force=True."""
        approved_by = _approver(approved_by, "closing the books")
        through = _date(through_date, "through_date")
        if through >= today_iso(): raise StateError("a period can only be closed through a date before today")
        with immediate_tx(self) as c:
            lock = self._books_lock()
            if lock and through <= lock: raise StateError(f"books are already closed through {lock}")
            check = self.verify_books(through)
            if check["alarms"] and not force:
                raise StateError("the books have alarms for this period: " + "; ".join(a["message"] for a in check["alarms"]) + " (close with force to accept them)")
            snap = self._tb_snapshot(through)
            c.execute("INSERT INTO period_closes (through_date, closed_by, closed_at, note, snapshot) VALUES (?,?,?,?,?)",
                      (through, actor or self._current_user() or "owner", now_iso(), (note or "")[:200], json.dumps(snap, sort_keys=True)))
            close_id = c.lastrowid
            self.audit(actor, A.AUDIT_ACTION["close_period"], "period_close", str(close_id),
                       {"through": through, "sha256": snap["sha256"], "forced_alarms": [a["code"] for a in check["alarms"]] if force else []}, approved_by)
        return self.period_status() | {"close_id": close_id, "sha256": snap["sha256"]}

    def reopen_period(self, close_id: int, reason: str, actor: str, approved_by: str | None) -> dict:
        reason, approved_by = _reason(reason), _approver(approved_by, "reopening the books")
        with immediate_tx(self) as c:
            r = self._one("SELECT * FROM period_closes WHERE close_id=?", (int(close_id),))
            if not r: raise NotFoundError(f"no such close: {close_id}")
            if r["reopened_at"]: raise StateError(f"close {close_id} was already reopened")
            c.execute("UPDATE period_closes SET reopened_by=?, reopened_at=?, reopen_reason=? WHERE close_id=?",
                      (actor or self._current_user() or "owner", now_iso(), reason, int(close_id)))
            self.audit(actor, A.AUDIT_ACTION["reopen_period"], "period_close", str(close_id), {"through": r["through_date"], "reason": reason}, approved_by)
        return self.period_status()
