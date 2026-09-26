"""The derived double-entry ledger (plan §2, §4.4): one pure mapping per source table turns every existing append-only
row into balanced postings. OWNED BY STREAM B.

Nothing here is stored. `postings(start, end)` maps row by row (books, exports, tests); `_balances(as_of)` runs the SAME
mapping over GROUPED rows (SUM(amount) per business date and every attribute the mapping reads), which is what the
statements use. The mapping is linear in the amount once the attributes are fixed, so the two paths agree exactly --
a test proves it account by account.

Posting rules (Dr / Cr; a negative source amount -- a reversal -- swaps the sides automatically, see accounts.signed):
  ledger invoice                      1100 customer / 4000        (method 'adjustment' = opening balance: / 3010)
  ledger credit_note (negative)       4010 / 1100 customer
  ledger payment, received_by driver  1050 / 1100                 (cash collected at a stop, still with the driver)
  ledger payment, other               money / 1100                (money = the row's account_id, else the method route
                                                                   at the row's business date; method 'adjustment' or
                                                                   an unrouted method = 1900 unassigned, flagged)
  deposits                            money (account_id or CASH) / 1050
  expenses cash_shortage              6000:cash_shortage / 1050   (recoveries are negative: the same rule reverses)
  expenses method 'payroll'           skipped: payroll posts from its own tables (_payroll_postings, Stream A);
                                      verify_books() proves the two agree
  expenses other                      6000:<category> / money
  supplier_ledger bill of a purchase  2050 / 2000 supplier        (its PRN reversal too: the PRN is a purchases row)
  supplier_ledger bill, other         3010 / 2000 supplier        (opening balance brought in)
  supplier_ledger payment (negative)  2000 supplier / money
  stock_moves purchase(_reversal)     1200 / 2050
  stock_moves sale (value negative)   1210 / 1200
  stock_moves return                  1200 / 1210
  stock_moves opening, and the 'adjust' moves of set_stock / the Excel import (setting a count outright when the
  business starts)                    1200 / 3010
  stock_moves adjust (any other)      5100 / 1200                 (damage, count difference: a gain reverses)
  sale_lines cost                     5000 / 1210                 (legacy_cost_price lines: 5000 / 3010 -- their goods
                                                                   left before the V5 value pool existed)
  inventory pool minus sum of move values (the V5 legacy difference; constant after V5)   1200 / 3010
  account_transfers                   money (to) / money (from)
  journal_lines                       as posted
  payroll (A)                         self._payroll_postings(start, end): every row of A's tables (payroll runs,
                                      salary payments, staff advances, statutory payments), sources
                                      'payroll_run' | 'salary_payment' | 'staff_advance' | 'statutory_payment'.
Account resolution of a reversal with no account of its own follows the ORIGINAL row (its account, else the route at
the original's date), so a remap between an entry and its reversal can never leave a residue."""
from __future__ import annotations

from munshi.domain import accounts as A
from munshi.domain.accounts import Posting, signed
from munshi.domain.models import sql_business_date
from munshi.domain.repository.base import RepositoryBase

MAPPING_VERSION = 1
PAYROLL_SOURCES = ("payroll_run", "salary_payment", "staff_advance", "statutory_payment")
MONEY_SOURCES = ("ledger", "deposit", "expense", "supplier_ledger", "transfer", "journal")   # the only row sources with a money leg
OPENING_STOCK_REFS = ("set_stock", "import")
_BD = sql_business_date


def _bd(col: str) -> str:
    return _BD(col)


# (source name, row SQL with an `on_` date column, `sid` id column and `amount`; the attribute columns the mapping reads)
_SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "ledger": (f"""SELECT {_bd('l.created_at')} on_, l.entry_id sid, l.kind kind, COALESCE(l.method, '') method,
                          COALESCE(l.received_by, '') received_by, COALESCE(l.account_id, o.account_id) account_id,
                          COALESCE({_bd('o.created_at')}, {_bd('l.created_at')}) resolve_on, l.customer_id party, l.amount amount
                   FROM ledger l LEFT JOIN ledger o ON o.entry_id = l.reversal_of""",
               ("kind", "method", "received_by", "account_id", "resolve_on", "party")),
    "deposit": (f"SELECT {_bd('deposited_at')} on_, deposit_id sid, COALESCE(account_id, '{A.CASH_ACCOUNT_ID}') account_id, amount_counted amount FROM deposits",
                ("account_id",)),
    "expense": ("""SELECT e.expense_date on_, e.expense_id sid, COALESCE(NULLIF(e.category, ''), 'misc') category, COALESCE(e.method, '') method,
                          COALESCE(e.account_id, o.account_id) account_id, COALESCE(o.expense_date, e.expense_date) resolve_on, e.amount amount
                   FROM expenses e LEFT JOIN expenses o ON o.expense_id = e.reversal_of""",
                ("category", "method", "account_id", "resolve_on")),
    "supplier_ledger": (f"""SELECT {_bd('s.created_at')} on_, s.entry_id sid, s.kind kind, COALESCE(s.method, '') method,
                                   COALESCE(s.account_id, o.account_id) account_id, COALESCE({_bd('o.created_at')}, {_bd('s.created_at')}) resolve_on,
                                   s.supplier_id party, EXISTS (SELECT 1 FROM purchases p WHERE p.purchase_id = s.ref) is_purchase, s.amount amount
                            FROM supplier_ledger s LEFT JOIN supplier_ledger o ON o.entry_id = s.reversal_of""",
                        ("kind", "method", "account_id", "resolve_on", "party", "is_purchase")),
    "stock_move": (f"""SELECT {_bd('created_at')} on_, move_id sid, kind kind,
                              (kind = 'opening' OR (kind = 'adjust' AND ref IN ('set_stock', 'import'))) opening_like, value_paisa amount
                       FROM stock_moves WHERE value_paisa <> 0""",
                   ("kind", "opening_like")),
    "sale_line": (f"SELECT {_bd('created_at')} on_, stop_id sid, cost_basis cost_basis, cost_paisa amount FROM sale_lines WHERE cost_paisa <> 0",
                  ("cost_basis",)),
    "transfer": ("SELECT transfer_date on_, transfer_id sid, from_account from_account, to_account to_account, amount_paisa amount FROM account_transfers",
                 ("from_account", "to_account")),
    "journal": ("""SELECT e.entry_date on_, e.je_id sid, l.account_code code, l.money_account_id money_account_id, l.party_kind party_kind,
                          l.party_id party_id, l.debit_paisa - l.credit_paisa amount, l.memo memo
                   FROM journal_lines l JOIN journal_entries e ON e.je_id = l.je_id""",
                ("code", "money_account_id", "party_kind", "party_id")),
}


class LedgerProjectionMixin(RepositoryBase):
    # ------------------------------------------------------------------ account resolution
    def _route_table(self) -> dict[str, list[tuple[str, int, str]]]:
        out: dict[str, list[tuple[str, int, str]]] = {}
        for r in self._all("SELECT method, account_id, effective_from, route_id FROM method_routes ORDER BY effective_from, route_id"):
            out.setdefault(r["method"], []).append((r["effective_from"], int(r["route_id"]), r["account_id"]))
        return out

    @staticmethod
    def _routed(routes: dict, method: str, on: str) -> str:
        acct = A.UNASSIGNED_ACCOUNT_ID
        for eff, _, account in routes.get(method, ()):
            if eff <= on:
                acct = account
            else:
                break
        return acct

    @staticmethod
    def _money_kw(prefix: str, account: str) -> tuple[str, dict]:
        """The chart code and Posting kwargs of a money leg: a real account posts to 1000, UNASSIGNED to 1900."""
        if not account or account == A.UNASSIGNED_ACCOUNT_ID:
            return A.UNASSIGNED_MONEY, {}
        return A.MONEY, {f"{prefix}money_account_id": account}

    # ------------------------------------------------------------------ the mapping (one function per source)
    def _map(self, source: str, r: dict, amount: int, routes: dict) -> list[Posting]:
        on, sid = r["on_"], r.get("sid") or ""
        kw = {"source": source, "source_id": sid}
        if amount == 0:
            return []

        def money(prefix: str, account_id, method: str, resolve_on: str) -> tuple[str, dict]:
            acct = account_id or self._routed(routes, method or "cash", resolve_on)     # '' on legacy rows = cash
            return self._money_kw(prefix, acct)

        if source == "ledger":
            cust = {"party_kind": "customer", "party_id": r["party"]}
            kind, method = r["kind"], r["method"]
            if kind == "invoice":
                credit = A.OPENING_BALANCE_EQUITY if method == "adjustment" else A.SALES
                return signed(on, A.TRADE_RECEIVABLES, credit, amount, **kw, **{f"debit_{k}": v for k, v in cust.items()})
            if kind == "credit_note":
                return signed(on, A.SALES_RETURNS, A.TRADE_RECEIVABLES, -amount, **kw, **{f"credit_{k}": v for k, v in cust.items()})
            if kind == "payment":
                if r["received_by"] == "driver":
                    debit, dkw = A.DRIVER_CASH_IN_TRANSIT, {}
                elif method == "adjustment":
                    debit, dkw = A.UNASSIGNED_MONEY, {}
                else:
                    debit, dkw = money("debit_", r["account_id"], method, r["resolve_on"])
                return signed(on, debit, A.TRADE_RECEIVABLES, -amount, **kw, **dkw, **{f"credit_{k}": v for k, v in cust.items()})
            return signed(on, A.TRADE_RECEIVABLES, A.OTHER_INCOME, amount, **kw, **{f"debit_{k}": v for k, v in cust.items()})
        if source == "deposit":
            debit, dkw = self._money_kw("debit_", r["account_id"])
            return signed(on, debit, A.DRIVER_CASH_IN_TRANSIT, amount, **kw, **dkw)
        if source == "expense":
            if r["method"] == A.PAYROLL_METHOD:
                return []
            if r["category"] == "cash_shortage":
                return signed(on, A.expense_code("cash_shortage"), A.DRIVER_CASH_IN_TRANSIT, amount, **kw)
            credit, ckw = money("credit_", r["account_id"], r["method"], r["resolve_on"])
            return signed(on, A.expense_code(r["category"]), credit, amount, **kw, **ckw)
        if source == "supplier_ledger":
            sup = {"party_kind": "supplier", "party_id": r["party"]}
            if r["kind"] == "payment":
                credit, ckw = money("credit_", r["account_id"], r["method"], r["resolve_on"])
                return signed(on, A.TRADE_PAYABLES, credit, -amount, **kw, **ckw, **{f"debit_{k}": v for k, v in sup.items()})
            debit = A.GOODS_RECEIVED_NOT_BILLED if r["is_purchase"] else A.OPENING_BALANCE_EQUITY
            return signed(on, debit, A.TRADE_PAYABLES, amount, **kw, **{f"credit_{k}": v for k, v in sup.items()})
        if source == "stock_move":
            kind = r["kind"]
            if kind in ("purchase", "purchase_reversal"):
                return signed(on, A.STOCK_GODOWNS, A.GOODS_RECEIVED_NOT_BILLED, amount, **kw)
            if kind == "sale":
                return signed(on, A.STOCK_ON_VEHICLES, A.STOCK_GODOWNS, -amount, **kw)
            if kind == "return":
                return signed(on, A.STOCK_GODOWNS, A.STOCK_ON_VEHICLES, amount, **kw)
            if r["opening_like"]:
                return signed(on, A.STOCK_GODOWNS, A.OPENING_BALANCE_EQUITY, amount, **kw)
            return signed(on, A.STOCK_GODOWNS, A.STOCK_ADJUSTMENTS, amount, **kw)
        if source == "sale_line":
            credit = A.OPENING_BALANCE_EQUITY if r["cost_basis"] == "legacy_cost_price" else A.STOCK_ON_VEHICLES
            return signed(on, A.COST_OF_GOODS_SOLD, credit, amount, **kw)
        if source == "transfer":
            return signed(on, A.MONEY, A.MONEY, amount, **kw, debit_money_account_id=r["to_account"], credit_money_account_id=r["from_account"])
        if source == "journal":
            extra = {k: r[k] for k in ("money_account_id", "party_kind", "party_id") if r.get(k) is not None}
            side = {"debit_paisa": amount} if amount > 0 else {"credit_paisa": -amount}
            return [Posting(on, r["code"], **side, **extra, **kw, memo=r.get("memo") or "")]
        raise ValueError(f"no posting rule for source {source!r}")

    # ------------------------------------------------------------------ the two paths
    def _pool_difference(self) -> tuple[str, int] | None:
        """The V5 legacy difference between the value pool and the sum of move values (constant after V5; 0 on a
        file born at V5 or later), dated at the first stock move."""
        r = self._one(f"SELECT (SELECT COALESCE(SUM(value_paisa), 0) FROM inventory_value) - (SELECT COALESCE(SUM(value_paisa), 0) FROM stock_moves) d, "
                      f"(SELECT MIN({_bd('created_at')}) FROM stock_moves) first_on")
        d = int(r["d"] or 0)
        return (r["first_on"] or "0001-01-01", d) if d else None

    def _source_rows(self, source: str, start: str | None, end: str | None, grouped: bool) -> list[dict]:
        sql, attrs = _SOURCES[source]
        cond, args = [], []
        if start: cond.append("on_ >= ?"); args.append(start)
        if end: cond.append("on_ <= ?"); args.append(end)
        where = (" WHERE " + " AND ".join(cond)) if cond else ""
        if grouped:
            cols = ", ".join(("on_",) + attrs)
            q = f"SELECT {cols}, SUM(amount) amount FROM ({sql}){where} GROUP BY {cols}"
        else:
            q = f"SELECT * FROM ({sql}){where} ORDER BY on_, sid"
        return [dict(r) for r in self._all(q, tuple(args))]

    def _project(self, start: str | None, end: str | None, grouped: bool, money_only: bool = False) -> list[Posting]:
        """money_only: just the sources that can move money (the account books' path; stock and cost never do)."""
        routes = self._route_table()
        out: list[Posting] = []
        for source in _SOURCES:
            if money_only and source not in MONEY_SOURCES: continue
            for r in self._source_rows(source, start, end, grouped):
                out.extend(self._map(source, r, int(r["amount"] or 0), routes))
        pool = None if money_only else self._pool_difference()
        if pool and (not start or pool[0] >= start) and (not end or pool[0] <= end):
            out.extend(signed(pool[0], A.STOCK_GODOWNS, A.OPENING_BALANCE_EQUITY, pool[1], source="stock_pool", source_id="V5",
                              memo="stock value brought in before the move ledger carried values"))
        out.extend(self._payroll_postings(start, end))
        return out

    def _ledger_postings(self, start: str | None = None, end: str | None = None, money_only: bool = False) -> list[Posting]:
        """Every posting dated in [start, end] (inclusive; None = open), row by row, in date order."""
        return sorted(self._project(start, end, grouped=False, money_only=money_only), key=lambda p: p.on)

    def _balances(self, as_of: str | None = None, start: str | None = None) -> dict[tuple, int]:
        """Debit-positive net per (code, money_account_id, party_kind, party_id) for postings in [start, as_of],
        computed over GROUPED source rows (the statements' path)."""
        out: dict[tuple, int] = {}
        for p in self._project(start, as_of, grouped=True):
            k = (p.code, p.money_account_id, p.party_kind, p.party_id)
            out[k] = out.get(k, 0) + p.net_paisa
        return {k: v for k, v in out.items() if v}

    @staticmethod
    def _aggregate(postings: list[Posting]) -> dict[tuple, int]:
        out: dict[tuple, int] = {}
        for p in postings:
            k = (p.code, p.money_account_id, p.party_kind, p.party_id)
            out[k] = out.get(k, 0) + p.net_paisa
        return {k: v for k, v in out.items() if v}

    @staticmethod
    def _by_code(bal: dict[tuple, int]) -> dict[str, int]:
        out: dict[str, int] = {}
        for (code, *_), v in bal.items():
            out[code] = out.get(code, 0) + v
        return out
