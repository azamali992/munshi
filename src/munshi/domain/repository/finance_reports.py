"""Statements read from the derived ledger (plan §7): trial balance, general journal, income statement (with a prior
period and the reconciliation to profit_summary), balance sheet, cash flow (direct, from the money accounts), owner KPIs,
margins, the fixed-asset register, loans, and verify_books() -- the integrity alarms. OWNED BY STREAM B.

Every report returns {"table": accounts.table(...), ...raw numbers in rupees}; multi-part reports add "tables".
Signs in statements: income positive, costs negative, so every column adds up down the page.
Owner decision 2: income_statement(..., redact_payroll=True) -- and profit_summary for non-owners -- combine the
staff cost lines (accounts.SYSTEM_EXPENSE_CATEGORIES / 6100-6200) into ONE "Staff costs" line."""
from __future__ import annotations

import json
from datetime import date, timedelta

from munshi.domain import accounts as A
from munshi.domain.accounts import col, table
from munshi.domain.models import sql_business_date, to_paisa, to_rupees, today_iso
from munshi.domain.repository.ledger_projection import LedgerProjectionMixin

STAFF_CODES = (A.SALARIES, A.WAGES, A.ALLOWANCES, A.COMMISSION, A.BONUS, A.EMPLOYER_CONTRIBUTIONS)
STAFF_COSTS_LABEL = "Staff costs"
STAFF_COSTS_KEY = "staff_costs"
_PL_TYPES = ("revenue", "expense")


def _prev_day(d: str) -> str:
    return (date.fromisoformat(d) - timedelta(days=1)).isoformat()


def fiscal_year_start(d: str) -> str:
    """Pakistan's fiscal (tax) year runs July to June."""
    y, m = int(d[:4]), int(d[5:7])
    return f"{y if m >= 7 else y - 1}-07-01"


def _pct(n: int, d: int) -> float | None:
    return round(n / d * 100, 1) if d else None


def _r(p: int | None) -> float | None:
    return None if p is None else to_rupees(int(p))


def mask_staff_categories(by_cat: dict[str, int]) -> dict[str, int]:
    """Owner decision 2: the payroll expense categories become ONE staff-costs entry for a non-owner (paisa in, out)."""
    out, staff = {}, 0
    for k, v in by_cat.items():
        if k in A.SYSTEM_EXPENSE_CATEGORIES:
            staff += v
        else:
            out[k] = out.get(k, 0) + v
    if staff:
        out[STAFF_COSTS_KEY] = out.get(STAFF_COSTS_KEY, 0) + staff
    return out


class FinanceReportsMixin(LedgerProjectionMixin):
    # ------------------------------------------------------------------ helpers
    def postings(self, start: str | None = None, end: str | None = None, money_only: bool = False) -> list:
        """The derived ledger, row by row: list[accounts.Posting] dated in [start, end] (inclusive). money_only skips
        the sources that never move money (stock moves, cost of sales) -- the account books' fast path."""
        return self._ledger_postings(start, end, money_only)

    def _code_name(self, code: str, acct: str | None = None, names: dict | None = None) -> str:
        if code == A.MONEY:
            names = names if names is not None else self._account_names()
            return names.get(acct, {}).get("name", acct or "money")
        if code.startswith(A.OPERATING_EXPENSES + ":"):
            return "Expenses: " + code.split(":", 1)[1].replace("_", " ")
        return A.CHART.get(A.base_code(code), (code, ""))[0]

    @staticmethod
    def _type(code: str) -> str:
        return A.CHART.get(A.base_code(code), ("", "expense"))[1]

    def _pl(self, start: str | None, end: str) -> dict[str, int]:
        """P&L code -> debit-positive net for postings dated in [start, end]."""
        return {k: v for k, v in self._by_code(self._balances(end, start)).items() if self._type(k) in _PL_TYPES}

    @staticmethod
    def _profit(pl: dict[str, int]) -> int:
        return -sum(pl.values())

    # ------------------------------------------------------------------ trial balance and general journal
    def trial_balance(self, as_of: str | None = None) -> dict:
        as_of = as_of or today_iso()
        names = self._account_names()
        agg: dict[tuple, int] = {}
        for (code, acct, _, _), v in self._balances(as_of).items():
            agg[(code, acct)] = agg.get((code, acct), 0) + v
        rows, dr, cr = [], 0, 0
        for (code, acct), v in sorted(agg.items(), key=lambda kv: (A.base_code(kv[0][0]), kv[0][0], kv[0][1] or "")):
            if not v: continue
            dr += max(v, 0); cr += max(-v, 0)
            rows.append({"code": code if not acct else f"{code} {acct}", "account": self._code_name(code, acct, names),
                         "debit": to_rupees(v) if v > 0 else None, "credit": to_rupees(-v) if v < 0 else None})
        t = table(f"Trial balance at {as_of}", [col("code", "Code"), col("account", "Account"), col("debit", "Debit", "money"), col("credit", "Credit", "money")],
                  rows, totals={"debit": to_rupees(dr), "credit": to_rupees(cr)},
                  note=None if dr == cr else f"Debits and credits differ by Rs {to_rupees(abs(dr - cr)):,.2f}: report this, it is a bug.")
        return {"as_of": as_of, "debits": to_rupees(dr), "credits": to_rupees(cr), "balanced": dr == cr,
                "balances": {r["code"]: (r["debit"] or 0) - (r["credit"] or 0) for r in rows}, "table": t}

    def general_journal(self, start: str, end: str) -> dict:
        """Every posting in [start, end]: date, document, source, account, party, debit, credit (the accountant's export)."""
        names = self._account_names()
        rows, dr, cr = [], 0, 0
        for p in self.postings(start, end):
            dr += p.debit_paisa; cr += p.credit_paisa
            rows.append({"date": p.on, "doc": p.source_id, "source": p.source, "code": p.code, "account": self._code_name(p.code, p.money_account_id, names),
                         "party": f"{p.party_kind}:{p.party_id}" if p.party_kind else "", "debit": _r(p.debit_paisa) if p.debit_paisa else None,
                         "credit": _r(p.credit_paisa) if p.credit_paisa else None, "memo": p.memo})
        cols = [col("date", "Date", "date"), col("doc", "Doc"), col("source", "Source"), col("code", "Code"), col("account", "Account"), col("party", "Party"),
                col("debit", "Debit", "money"), col("credit", "Credit", "money"), col("memo", "Memo")]
        return {"start": start, "end": end, "rows": rows, "debits": to_rupees(dr), "credits": to_rupees(cr),
                "table": table(f"General journal, {start} to {end}", cols, rows, totals={"debit": to_rupees(dr), "credit": to_rupees(cr)})}

    # ------------------------------------------------------------------ income statement
    def _is_lines(self, pl: dict[str, int], redact: bool) -> list[tuple[str, str, int, bool]]:
        """(key, label, signed paisa, is_subtotal) in statement order. Income positive, costs negative."""
        g = lambda code: pl.get(code, 0)  # noqa: E731
        sales, returns, cogs = -g(A.SALES), -g(A.SALES_RETURNS), -g(A.COST_OF_GOODS_SOLD)
        out = [("sales", "Sales", sales, False), ("credit_notes", "Credit notes", returns, False), ("net_revenue", "Net revenue", sales + returns, True),
               ("cogs", "Cost of goods sold", cogs, False), ("gross_profit", "Gross profit", sales + returns + cogs, True)]
        seen = {A.SALES, A.SALES_RETURNS, A.COST_OF_GOODS_SOLD}
        for code in sorted((c for c in pl if c.startswith(A.OPERATING_EXPENSES + ":")), key=lambda c: -pl[c]):
            out.append((f"expense:{code.split(':', 1)[1]}", "Expenses: " + code.split(":", 1)[1].replace("_", " "), -pl[code], False)); seen.add(code)
        staff = [c for c in STAFF_CODES if pl.get(c)]
        if redact:
            if staff: out.append((STAFF_COSTS_KEY, STAFF_COSTS_LABEL, -sum(pl[c] for c in staff), False))
        else:
            for c in staff: out.append((f"staff:{c}", f"{STAFF_COSTS_LABEL}: {A.CHART[c][0].lower()}", -pl[c], False))
        seen |= set(STAFF_CODES)
        for key, code, label in (("stock_adjustments", A.STOCK_ADJUSTMENTS, "Stock write-offs and adjustments"), ("depreciation", A.DEPRECIATION, "Depreciation"),
                                 ("interest", A.INTEREST_AND_BANK_CHARGES, "Interest and bank charges"), ("other_income", A.OTHER_INCOME, "Other income"),
                                 ("cash_over_short", A.CASH_OVER_SHORT, "Cash over / short")):
            out.append((key, label, -g(code), False)); seen.add(code)
        for code in sorted(set(pl) - seen):
            out.append((f"other:{code}", self._code_name(code), -pl[code], False))
        out.append(("net_profit", "Net profit", self._profit(pl), True))
        return out

    def income_statement(self, start: str, end: str, compare: bool = True, redact_payroll: bool = False) -> dict:
        """The full P&L for [start, end] from the derived ledger -- stock write-offs, depreciation and interest included --
        with the same-length prior period, and a reconciliation to profit_summary's operating profit.
        redact_payroll=True (a caller without payroll:read) shows staff costs as ONE line (owner decision 2)."""
        n_days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        p_end = _prev_day(start)
        p_start = (date.fromisoformat(start) - timedelta(days=n_days)).isoformat()
        cur = self._is_lines(self._pl(start, end), redact_payroll)
        prior_lines = self._is_lines(self._pl(p_start, p_end), redact_payroll) if compare else []
        prior = {k: v for k, _, v, _ in prior_lines}
        prior_label = {k: label for k, label, _, _ in prior_lines}
        keys_prior = set(prior)
        net_rev = next(v for k, _, v, _ in cur if k == "net_revenue")
        always = {"sales", "net_revenue", "cogs", "gross_profit", "net_profit"}
        rows, raw = [], {}
        cur_keys = {k for k, *_ in cur}
        for k, label, v, em in cur:
            raw[k] = v
            pv = prior.get(k) if compare else None
            if not v and not pv and k not in always: continue
            row = {"line": label, "amount": to_rupees(v), "pct_of_revenue": _pct(v, net_rev)}
            if compare: row |= {"prior_period": _r(pv or 0), "change": to_rupees(v - (pv or 0))}
            if em: row["_em"] = True
            rows.append(row)
        for k in sorted(keys_prior - cur_keys):               # a line only the prior period had
            if prior[k]:
                rows.insert(-1, {"line": prior_label[k], "amount": 0.0, "pct_of_revenue": None, "prior_period": _r(prior[k]), "change": _r(-prior[k])})
        s = self._sales_paisa(start, end)
        caveat = self._cost_fields(s)["caveat"]
        cols = [col("line", "Line"), col("amount", "Amount", "money"), col("pct_of_revenue", "% of revenue", "pct")]
        if compare: cols += [col("prior_period", f"Prior ({p_start} to {p_end})", "money"), col("change", "Change", "money")]
        main = table(f"Income statement, {start} to {end}", cols, rows, note=caveat)
        # reconciliation to the operating profit the rest of the app reports (profit_summary)
        ps = self.profit_summary(start, end, redact_payroll=redact_payroll)
        ps_net = to_paisa(ps["net"])
        adj = [("Stock write-offs and adjustments", raw.get("stock_adjustments", 0)), ("Depreciation", raw.get("depreciation", 0)),
               ("Interest and bank charges", raw.get("interest", 0)), ("Other income", raw.get("other_income", 0)),
               ("Cash over / short", raw.get("cash_over_short", 0))]
        other = raw["net_profit"] - ps_net - sum(v for _, v in adj)
        rec_rows = [{"line": "Operating profit (profit summary)", "amount": to_rupees(ps_net), "_em": True}]
        rec_rows += [{"line": label, "amount": to_rupees(v)} for label, v in adj if v]
        if other: rec_rows.append({"line": "Other journal entries and payroll differences", "amount": to_rupees(other)})
        rec_rows.append({"line": "Net profit (income statement)", "amount": to_rupees(raw["net_profit"]), "_em": True})
        rec = table("Reconciliation to operating profit", [col("line", "Line"), col("amount", "Amount", "money")], rec_rows)
        exp = {k.split(":", 1)[1]: -v for k, v in raw.items() if k.startswith("expense:")}
        staff_total = -sum(v for k, v in raw.items() if k == STAFF_COSTS_KEY or k.startswith("staff:"))
        out = {"start": start, "end": end, "redacted": redact_payroll,
               "revenue": _r(raw["sales"]), "credit_notes": _r(-raw["credit_notes"]), "net_revenue": _r(raw["net_revenue"]),
               "cost_of_goods": _r(-raw["cogs"]), "gross_profit": _r(raw["gross_profit"]),
               "expenses_by_category": {k: _r(v) for k, v in exp.items() if v},
               "staff_costs": _r(staff_total), "stock_adjustments": _r(-raw.get("stock_adjustments", 0)), "depreciation": _r(-raw.get("depreciation", 0)),
               "interest": _r(-raw.get("interest", 0)), "other_income": _r(raw.get("other_income", 0)), "cash_over_short": _r(-raw.get("cash_over_short", 0)),
               "net_profit": _r(raw["net_profit"]),
               "reconciliation": {"operating_profit": _r(ps_net), "other": _r(other), "net_profit": _r(raw["net_profit"])},
               "table": main, "tables": [main, rec]} | {k: v for k, v in self._cost_fields(s).items() if k in ("cost_missing", "margin_reliable", "caveat")}
        if not redact_payroll:
            out["staff_costs_by_code"] = {A.CHART[k.split(":")[1]][0]: _r(-v) for k, v in raw.items() if k.startswith("staff:")}
        if compare:
            out["prior"] = {"start": p_start, "end": p_end, "net_revenue": _r(prior.get("net_revenue", 0)), "net_profit": _r(prior.get("net_profit", 0))}
        return out

    # ------------------------------------------------------------------ balance sheet
    def balance_sheet(self, as_of: str | None = None) -> dict:
        """Assets = liabilities + equity at as_of, customer and supplier advances reclassified, clearing accounts and
        unassigned money flagged. Equity splits retained earnings brought forward from this fiscal year's profit
        (July to June)."""
        as_of = as_of or today_iso()
        bal = self._balances(as_of)
        names = self._account_names()
        fy = fiscal_year_start(as_of)
        period_profit = self._profit(self._pl(fy, as_of))
        by_code = self._by_code(bal)
        total_profit = -sum(v for c, v in by_code.items() if self._type(c) in _PL_TYPES)
        party: dict[tuple[str, str], dict[str, int]] = {}
        money: dict[str, int] = {}
        for (code, acct, pk, pid), v in bal.items():
            if code == A.MONEY: money[acct] = money.get(acct, 0) + v
            elif pk: party.setdefault((code, pk), {}); party[(code, pk)][pid] = party[(code, pk)].get(pid, 0) + v
        g = lambda code: by_code.get(code, 0)  # noqa: E731
        ar = party.get((A.TRADE_RECEIVABLES, "customer"), {})
        ap = party.get((A.TRADE_PAYABLES, "supplier"), {})
        ar_dr, ar_cr = sum(v for v in ar.values() if v > 0), -sum(v for v in ar.values() if v < 0)
        ap_cr, ap_dr = -sum(v for v in ap.values() if v < 0), sum(v for v in ap.values() if v > 0)
        ar_other, ap_other = g(A.TRADE_RECEIVABLES) - (ar_dr - ar_cr), g(A.TRADE_PAYABLES) - (ap_dr - ap_cr)
        alarms = []
        assets: list[tuple[str, str, int]] = []
        order = {"cash": 0, "bank": 1, "wallet": 2}
        for aid in sorted(money, key=lambda a: (order.get(names.get(a, {}).get("kind"), 3), names.get(a, {}).get("name", a))):
            v = money[aid]
            if not v: continue
            assets.append((f"money:{aid}", names.get(aid, {}).get("name", aid), v))
            if v < 0: alarms.append({"code": "negative_money", "message": f"{names.get(aid, {}).get('name', aid)} is negative (Rs {to_rupees(v):,.2f}): an opening balance or an entry is missing", "amount": to_rupees(v)})
        if g(A.UNASSIGNED_MONEY):
            assets.append(("unassigned", "Unassigned money (no account for its method)", g(A.UNASSIGNED_MONEY)))
            alarms.append({"code": "unassigned_money", "message": f"Rs {to_rupees(g(A.UNASSIGNED_MONEY)):,.2f} of money has no account: route its method (bank, cheque, wallet) to an account", "amount": to_rupees(g(A.UNASSIGNED_MONEY))})
        assets += [("driver_cash", "Cash with drivers", g(A.DRIVER_CASH_IN_TRANSIT)), ("receivables", "Trade receivables", ar_dr + ar_other),
                   ("supplier_advances", "Advances to suppliers", ap_dr), ("staff_advances", "Staff advances", g(A.STAFF_ADVANCES)),
                   ("stock", "Stock in godowns", g(A.STOCK_GODOWNS)), ("stock_on_vehicles", "Stock on vehicles", g(A.STOCK_ON_VEHICLES)),
                   ("fixed_assets", "Fixed assets at cost", g(A.FIXED_ASSETS_COST)), ("acc_dep", "Less: accumulated depreciation", g(A.ACCUMULATED_DEPRECIATION))]
        liab: list[tuple[str, str, int]] = [("payables", "Trade payables", ap_cr - ap_other), ("customer_advances", "Customer advances", ar_cr),
                                            ("grni", "Goods received not billed", -g(A.GOODS_RECEIVED_NOT_BILLED))]
        for code in (A.SALARIES_PAYABLE, A.INCOME_TAX_WITHHELD, A.EOBI_PAYABLE, A.SOCIAL_SECURITY_PAYABLE, A.STAFF_WELFARE_FUND):
            liab.append((f"c:{code}", A.CHART[code][0], -g(code)))
        lenders = {r["loan_id"]: r["lender"] for r in self._all("SELECT loan_id, lender FROM loans")}
        loans = party.get((A.LOANS_PAYABLE, "loan"), {})
        for lid, v in sorted(loans.items()):
            liab.append((f"loan:{lid}", f"Loan: {lenders.get(lid, lid)}", -v))
        loan_other = g(A.LOANS_PAYABLE) - sum(loans.values())
        liab += [("loans_other", "Loans (unallocated)", -loan_other), ("other_payables", "Other payables", -g(A.OTHER_PAYABLES))]
        equity = [("capital", "Owner's capital", -g(A.OWNER_CAPITAL)), ("opening_equity", "Opening balance equity", -g(A.OPENING_BALANCE_EQUITY)),
                  ("retained", "Retained earnings brought forward", total_profit - period_profit), ("profit", f"Profit for the year from {fy}", period_profit),
                  ("drawings", "Drawings", -g(A.DRAWINGS))]
        handled = {A.MONEY, A.UNASSIGNED_MONEY, A.DRIVER_CASH_IN_TRANSIT, A.TRADE_RECEIVABLES, A.STAFF_ADVANCES, A.STOCK_GODOWNS, A.STOCK_ON_VEHICLES,
                   A.FIXED_ASSETS_COST, A.ACCUMULATED_DEPRECIATION, A.TRADE_PAYABLES, A.GOODS_RECEIVED_NOT_BILLED, A.SALARIES_PAYABLE,
                   A.INCOME_TAX_WITHHELD, A.EOBI_PAYABLE, A.SOCIAL_SECURITY_PAYABLE, A.STAFF_WELFARE_FUND, A.LOANS_PAYABLE, A.OTHER_PAYABLES,
                   A.OWNER_CAPITAL, A.OPENING_BALANCE_EQUITY, A.DRAWINGS}
        for code, v in by_code.items():                        # anything else on the balance sheet side (never expected)
            if code in handled or self._type(code) in _PL_TYPES or not v: continue
            (assets if self._type(code) == "asset" else liab if self._type(code) == "liability" else equity).append(
                (f"c:{code}", self._code_name(code), v if self._type(code) == "asset" else -v))
        for code, label in ((A.DRIVER_CASH_IN_TRANSIT, "cash with drivers"), (A.STOCK_ON_VEHICLES, "stock on vehicles"), (A.GOODS_RECEIVED_NOT_BILLED, "goods received not billed")):
            if g(code): alarms.append({"code": f"clearing_{code}", "message": f"{label.capitalize()} ({code}) should be zero once the day is done but is Rs {to_rupees(g(code)):,.2f}", "amount": to_rupees(g(code))})
        ta, tl, te = sum(v for *_, v in assets), sum(v for *_, v in liab), sum(v for *_, v in equity)
        rows = [{"line": "Assets", "amount": None, "_em": True}]
        rows += [{"line": label, "amount": to_rupees(v)} for _, label, v in assets if v]
        rows.append({"line": "Total assets", "amount": to_rupees(ta), "_em": True})
        rows.append({"line": "Liabilities", "amount": None, "_em": True})
        rows += [{"line": label, "amount": to_rupees(v)} for _, label, v in liab if v]
        rows.append({"line": "Total liabilities", "amount": to_rupees(tl), "_em": True})
        rows.append({"line": "Equity", "amount": None, "_em": True})
        rows += [{"line": label, "amount": to_rupees(v)} for _, label, v in equity if v]
        rows.append({"line": "Total equity", "amount": to_rupees(te), "_em": True})
        rows.append({"line": "Total liabilities and equity", "amount": to_rupees(tl + te), "_em": True})
        rows.append({"line": "Check: assets - (liabilities + equity)", "amount": to_rupees(ta - tl - te)})
        note = " ".join(a["message"] + "." for a in alarms) or None
        t = table(f"Balance sheet at {as_of}", [col("line", "Line"), col("amount", "Amount", "money")], rows, note=note)
        pick = lambda xs: {k: to_rupees(v) for k, _, v in xs if v}  # noqa: E731
        return {"as_of": as_of, "fiscal_year_start": fy, "assets": pick(assets), "liabilities": pick(liab), "equity": pick(equity),
                "total_assets": to_rupees(ta), "total_liabilities": to_rupees(tl), "total_equity": to_rupees(te),
                "difference": to_rupees(ta - tl - te), "balanced": ta == tl + te, "profit_for_period": to_rupees(period_profit),
                "money_total": to_rupees(sum(money.values()) + g(A.UNASSIGNED_MONEY)), "alarms": alarms, "table": t}

    # ------------------------------------------------------------------ cash flow (direct)
    _CF_LINES = {  # code -> (section, line)
        A.TRADE_RECEIVABLES: ("operating", "Received from customers"), A.DRIVER_CASH_IN_TRANSIT: ("operating", "Received from customers"),
        A.TRADE_PAYABLES: ("operating", "Paid to suppliers"), A.SALARIES_PAYABLE: ("operating", "Salaries paid"),
        A.STAFF_ADVANCES: ("operating", "Staff advances (net)"), A.INCOME_TAX_WITHHELD: ("operating", "Statutory payments"),
        A.EOBI_PAYABLE: ("operating", "Statutory payments"), A.SOCIAL_SECURITY_PAYABLE: ("operating", "Statutory payments"),
        A.STAFF_WELFARE_FUND: ("operating", "Statutory payments"), A.INTEREST_AND_BANK_CHARGES: ("operating", "Interest and bank charges paid"),
        A.FIXED_ASSETS_COST: ("investing", "Fixed assets bought and sold"), A.ACCUMULATED_DEPRECIATION: ("investing", "Fixed assets bought and sold"),
        A.OWNER_CAPITAL: ("financing", "Capital introduced"), A.DRAWINGS: ("financing", "Drawings"),
    }

    def cash_flow(self, start: str, end: str) -> dict:
        """Direct method, from the money accounts: every money movement in [start, end] attributed to what it paid
        for or came from. Opening-balance entries dated in the period count as opening money, not as a flow.
        Transfers between the business's own accounts are a memo line."""
        prev = _prev_day(start)
        bal0 = self._balances(prev)
        is_money = lambda p: p.code in (A.MONEY, A.UNASSIGNED_MONEY)  # noqa: E731
        opening = sum(v for (code, *_), v in bal0.items() if code in (A.MONEY, A.UNASSIGNED_MONEY))
        jkind = {r["je_id"]: r["kind"] for r in self._all("SELECT je_id, kind FROM journal_entries WHERE entry_date BETWEEN ? AND ?", (start, end))}
        groups: dict[tuple, list] = {}
        for p in self.postings(start, end):
            groups.setdefault((p.source, p.source_id, p.on), []).append(p)
        flows: dict[tuple[str, str], int] = {}
        opening_in = 0
        for (source, sid, _), ps in groups.items():
            m = sum(p.net_paisa for p in ps if is_money(p))
            if not m: continue
            kind = jkind.get(sid) if source == "journal" else None
            if kind == "opening":
                opening_in += m; continue
            for p in ps:
                if is_money(p): continue
                if kind in ("asset_purchase", "asset_disposal"):
                    key = ("investing", "Fixed assets bought and sold")
                elif p.code == A.LOANS_PAYABLE:
                    key = ("financing", "Loans received" if p.net_paisa < 0 else "Loans repaid")
                elif p.code.startswith(A.OPERATING_EXPENSES + ":"):
                    key = ("operating", "Expenses paid")
                elif p.code == A.OPENING_BALANCE_EQUITY:
                    key = ("opening", "")
                else:
                    key = self._CF_LINES.get(p.code, ("operating", "Other operating"))
                if key[0] == "opening":
                    opening_in -= p.net_paisa; continue
                flows[key] = flows.get(key, 0) - p.net_paisa
        opening_total = opening + opening_in
        rows, sections = [], {}
        for sec, title in (("operating", "Operating activities"), ("investing", "Investing activities"), ("financing", "Financing activities")):
            items = [(line, v) for (s, line), v in flows.items() if s == sec]
            tot = sum(v for _, v in items)
            sections[sec] = {"total": to_rupees(tot), "lines": {line: to_rupees(v) for line, v in items}}
            rows.append({"line": title, "amount": None, "_em": True})
            order = ["Received from customers", "Paid to suppliers", "Expenses paid", "Salaries paid", "Staff advances (net)", "Statutory payments",
                     "Interest and bank charges paid", "Other operating", "Fixed assets bought and sold", "Loans received", "Loans repaid",
                     "Capital introduced", "Drawings"]
            for line, v in sorted(items, key=lambda x: order.index(x[0]) if x[0] in order else 99):
                rows.append({"line": line, "amount": to_rupees(v)})
            rows.append({"line": f"Net cash from {sec} activities", "amount": to_rupees(tot), "_em": True})
        net = sum(flows.values())
        closing = opening_total + net
        bal1 = self._balances(end)
        actual = sum(v for (code, *_), v in bal1.items() if code in (A.MONEY, A.UNASSIGNED_MONEY))
        rows += [{"line": "Net change in money", "amount": to_rupees(net), "_em": True},
                 {"line": "Opening money" + (" (incl. opening balances brought in)" if opening_in else ""), "amount": to_rupees(opening_total)},
                 {"line": "Closing money", "amount": to_rupees(closing), "_em": True},
                 {"line": "Check: sum of account balances", "amount": to_rupees(actual)}]
        transfers = int(self._one("SELECT COALESCE(SUM(amount_paisa), 0) s FROM account_transfers WHERE transfer_date BETWEEN ? AND ?", (start, end))["s"])
        if transfers: rows.append({"line": "Memo: transfers between own accounts", "amount": to_rupees(transfers)})
        t = table(f"Cash flow, {start} to {end}", [col("line", "Line"), col("amount", "Amount", "money")], rows,
                  note=None if closing == actual else f"Closing money differs from the accounts by Rs {to_rupees(actual - closing):,.2f}.")
        return {"start": start, "end": end, "opening": to_rupees(opening_total), "opening_balances_brought_in": to_rupees(opening_in),
                "operating": sections["operating"], "investing": sections["investing"], "financing": sections["financing"],
                "net_change": to_rupees(net), "closing": to_rupees(closing), "accounts_total": to_rupees(actual), "ties": closing == actual,
                "transfers_memo": to_rupees(transfers), "table": t}

    # ------------------------------------------------------------------ KPIs and margins
    def margins(self, by: str, start: str, end: str) -> dict:
        """Gross margin by product, customer or route (from the delivered sales and their cost snapshots)."""
        if by not in ("product", "customer", "route"): raise ValueError("by must be product, customer or route")
        key = {"product": "s.sku", "customer": "s.customer_id", "route": "COALESCE(c.route_id, '')"}[by]
        q = (f"SELECT {key} k, SUM(s.qty) q, SUM(s.revenue_paisa) rev, SUM(s.cost_paisa) cost FROM sale_lines s LEFT JOIN customers c ON c.customer_id = s.customer_id "
             f"WHERE {sql_business_date('s.created_at')} BETWEEN ? AND ? GROUP BY k")
        if by == "product": names = {p.sku: p.name for p in self.list_products(include_inactive=True)}
        elif by == "customer": names = {c.customer_id: c.name for c in self.list_customers(include_inactive=True)}
        else: names = {r.route_id: r.name for r in self.list_routes()} | {"": "No route"}
        rows, tq, trev, tcost = [], 0, 0, 0
        for r in self._all(q, (start, end)):
            rev, cost = int(r["rev"] or 0), int(r["cost"] or 0)
            tq += int(r["q"] or 0); trev += rev; tcost += cost
            rows.append({"key": r["k"], "name": names.get(r["k"], r["k"]), "qty": int(r["q"] or 0), "revenue": to_rupees(rev), "cost": to_rupees(cost),
                         "margin": to_rupees(rev - cost), "margin_pct": _pct(rev - cost, rev)})
        rows.sort(key=lambda x: -x["margin"])
        cols = [col("name", by.capitalize()), col("qty", "Qty", "qty"), col("revenue", "Revenue", "money"), col("cost", "Cost", "money"),
                col("margin", "Margin", "money"), col("margin_pct", "Margin %", "pct")]
        t = table(f"Margin by {by}, {start} to {end}", cols, rows, totals={"qty": tq, "revenue": to_rupees(trev), "cost": to_rupees(tcost),
                                                                              "margin": to_rupees(trev - tcost), "margin_pct": _pct(trev - tcost, trev)},
                  note="Delivered sales at the cost snapshotted when the goods left the godown; credit notes are not allocated to lines.")
        return {"by": by, "start": start, "end": end, "rows": rows, "revenue": to_rupees(trev), "cost": to_rupees(tcost), "margin": to_rupees(trev - tcost), "table": t}

    def owner_kpis(self, as_of: str | None = None) -> dict:
        as_of = as_of or today_iso()
        m_start = as_of[:8] + "01"
        pm_end = _prev_day(m_start); pm_start = pm_end[:8] + "01"
        d90 = (date.fromisoformat(as_of) - timedelta(days=89)).isoformat()
        cur, prev = self._pl(m_start, as_of), self._pl(pm_start, pm_end)
        by = self._by_code(self._balances(as_of))
        p90 = self._by_code(self._balances(as_of, d90))

        def figures(pl):
            rev = -pl.get(A.SALES, 0) - pl.get(A.SALES_RETURNS, 0)
            gp = rev - pl.get(A.COST_OF_GOODS_SOLD, 0)
            staff = sum(pl.get(c, 0) for c in STAFF_CODES)
            return rev, gp, self._profit(pl), staff
        rev, gp, net, staff = figures(cur)
        prev_rev, prev_gp, prev_net, prev_staff = figures(prev)
        money = by.get(A.MONEY, 0) + by.get(A.UNASSIGNED_MONEY, 0)
        ar, ap, stock = by.get(A.TRADE_RECEIVABLES, 0), -by.get(A.TRADE_PAYABLES, 0), by.get(A.STOCK_GODOWNS, 0)
        credit_sales_90 = -p90.get(A.SALES, 0)
        bills_90 = sum(-p.net_paisa for p in self.postings(d90, as_of) if p.code == A.TRADE_PAYABLES and p.source == "supplier_ledger" and p.net_paisa < 0)
        cogs_90 = p90.get(A.COST_OF_GOODS_SOLD, 0)
        days = lambda bal, flow: round(bal * 90 / flow, 1) if flow > 0 else None  # noqa: E731
        aging = self.aging_summary()
        coll = self.collection_report(m_start, as_of)
        routes = self.margins("route", m_start, as_of)["rows"]
        best = max(routes, key=lambda r: r["margin_pct"] or -1e9) if routes else None
        worst = min(routes, key=lambda r: r["margin_pct"] if r["margin_pct"] is not None else 1e9) if routes else None
        rows = [
            {"kpi": "Revenue this month", "value": _r(rev), "unit": "Rs", "prior": _r(prev_rev), "note": f"{m_start} to {as_of}"},
            {"kpi": "Gross margin", "value": _pct(gp, rev), "unit": "%", "prior": _pct(prev_gp, prev_rev), "note": ""},
            {"kpi": "Net profit this month", "value": _r(net), "unit": "Rs", "prior": _r(prev_net), "note": "after stock write-offs, depreciation and interest"},
            {"kpi": "Cash, bank and wallets", "value": _r(money), "unit": "Rs", "prior": None, "note": "includes unassigned money" if by.get(A.UNASSIGNED_MONEY) else ""},
            {"kpi": "Receivables", "value": _r(ar), "unit": "Rs", "prior": None, "note": ""},
            {"kpi": "Days sales outstanding (DSO)", "value": days(ar, credit_sales_90), "unit": "days", "prior": None, "note": "receivables / credit sales per day, last 90 days"},
            {"kpi": "Overdue 60+ days", "value": aging["buckets"]["60+"], "unit": "Rs", "prior": None, "note": "as of today"},
            {"kpi": "Payables", "value": _r(ap), "unit": "Rs", "prior": None, "note": ""},
            {"kpi": "Days payables outstanding (DPO)", "value": days(ap, bills_90), "unit": "days", "prior": None, "note": "payables / supplier bills per day, last 90 days"},
            {"kpi": "Stock at cost", "value": _r(stock), "unit": "Rs", "prior": None, "note": "moving average"},
            {"kpi": "Stock days", "value": days(stock, cogs_90), "unit": "days", "prior": None, "note": "stock / cost of goods sold per day, last 90 days"},
            {"kpi": "Payroll % of revenue", "value": _pct(staff, rev), "unit": "%", "prior": _pct(prev_staff, prev_rev), "note": "owner only"},
            {"kpi": "Collection rate this month", "value": coll["collection_rate_pct"], "unit": "%", "prior": None, "note": "collected / invoiced"},
            {"kpi": "Best route by margin", "value": best["margin_pct"] if best else None, "unit": "%", "prior": None, "note": best["name"] if best else ""},
            {"kpi": "Worst route by margin", "value": worst["margin_pct"] if worst else None, "unit": "%", "prior": None, "note": worst["name"] if worst else ""},
        ]
        t = table(f"Owner KPIs at {as_of}", [col("kpi", "KPI"), col("value", "Value", "qty"), col("unit", "Unit"), col("prior", "Prior month", "qty"), col("note", "Note")], rows)
        return {"as_of": as_of, "kpis": {r["kpi"]: r["value"] for r in rows}, "rows": rows, "table": t}

    # ------------------------------------------------------------------ fixed assets and loans
    def _asset_view(self, asset_id: str, as_of: str | None = None) -> dict:
        a = self._asset(asset_id)
        cost = self._asset_code_net(asset_id, A.FIXED_ASSETS_COST, as_of)
        acc = -self._asset_code_net(asset_id, A.ACCUMULATED_DEPRECIATION, as_of)
        disposal = self._disposal(asset_id)
        sched = self._asset_schedule(a)
        done = self._depreciated_periods(asset_id)
        monthly = next(iter(sched.values()), 0)
        if disposal: status = "disposed"
        elif cost <= 0: status = "cancelled"
        elif int(a["life_months"]) == 0: status = "not depreciated"
        elif sched and set(sched) <= done: status = "fully depreciated"
        else: status = "in use"
        return {"asset_id": asset_id, "name": a["name"], "category": a["category"], "vehicle_id": a["vehicle_id"], "acquired_on": a["acquired_on"],
                "cost": to_rupees(int(a["cost_paisa"])), "life_months": int(a["life_months"]), "salvage": to_rupees(int(a["salvage_paisa"])),
                "monthly_dep": to_rupees(monthly), "acc_dep": to_rupees(acc), "book_value": to_rupees(cost - acc),
                "depreciated_through": max(done) if done else None, "status": status}

    def fixed_assets_register(self, as_of: str | None = None) -> dict:
        as_of = as_of or today_iso()
        rows = [self._asset_view(r["asset_id"], as_of) for r in self._all("SELECT asset_id FROM fixed_assets ORDER BY acquired_on, asset_id")]
        cols = [col("name", "Asset"), col("category", "Category"), col("acquired_on", "Acquired", "date"), col("cost", "Cost", "money"),
                col("life_months", "Life (months)", "qty"), col("monthly_dep", "Monthly dep.", "money"), col("acc_dep", "Acc. dep.", "money"),
                col("book_value", "Book value", "money"), col("status", "Status", badge=True)]
        live = [r for r in rows if r["status"] not in ("disposed", "cancelled")]
        tot = {"cost": sum(r["cost"] for r in live), "acc_dep": round(sum(r["acc_dep"] for r in live), 2), "book_value": round(sum(r["book_value"] for r in live), 2)}
        return {"as_of": as_of, "assets": rows, **tot, "table": table(f"Fixed assets at {as_of}", cols, rows, totals=tot)}

    def loans_report(self, as_of: str | None = None) -> dict:
        as_of = as_of or today_iso()
        rows = []
        for ln in self._all("SELECT * FROM loans ORDER BY received_on, loan_id"):
            r = self._one("SELECT COALESCE(SUM(CASE WHEN l.account_code=? THEN l.credit_paisa ELSE 0 END), 0) received, "
                          "COALESCE(SUM(CASE WHEN l.account_code=? THEN l.debit_paisa ELSE 0 END), 0) repaid, "
                          "COALESCE(SUM(CASE WHEN l.account_code=? THEN l.debit_paisa - l.credit_paisa ELSE 0 END), 0) interest "
                          "FROM journal_lines l JOIN journal_entries e ON e.je_id = l.je_id WHERE l.party_kind='loan' AND l.party_id=? AND e.entry_date <= ?",
                          (A.LOANS_PAYABLE, A.LOANS_PAYABLE, A.INTEREST_AND_BANK_CHARGES, ln["loan_id"], as_of))
            rec, rep, intr = int(r["received"]), int(r["repaid"]), int(r["interest"])
            rows.append({"loan_id": ln["loan_id"], "lender": ln["lender"], "kind": ln["kind"], "received_on": ln["received_on"], "received": to_rupees(rec),
                         "repaid": to_rupees(rep), "interest_paid": to_rupees(intr), "outstanding": to_rupees(rec - rep), "terms": ln["terms"]})
        cols = [col("loan_id", "Loan"), col("lender", "Lender"), col("kind", "Kind"), col("received", "Received", "money"), col("repaid", "Repaid", "money"),
                col("interest_paid", "Interest paid", "money"), col("outstanding", "Outstanding", "money"), col("terms", "Terms")]
        tot = {k: round(sum(r[k] for r in rows), 2) for k in ("received", "repaid", "interest_paid", "outstanding")}
        return {"as_of": as_of, "loans": rows, **tot, "table": table(f"Loans at {as_of}", cols, rows, totals=tot)}

    # ------------------------------------------------------------------ integrity
    def verify_books(self, as_of: str | None = None) -> dict:
        """Alarms: trial balance not zero; clearing accounts (1050 cash with drivers, 1210 stock on vehicles, 2050 goods
        received not billed) not zero; unassigned money; a negative money account; the ledger disagreeing with the
        khata, the supplier khata or the stock pool; payroll expense rows disagreeing with the payroll postings; a
        closed period whose numbers changed since it was closed ("books drift")."""
        today = today_iso()
        as_of = as_of or today
        bal = self._balances(as_of)
        by = self._by_code(bal)
        names = self._account_names()
        alarms: list[dict] = []

        def alarm(code: str, message: str, amount_p: int | None = None):
            alarms.append({"code": code, "message": message, "amount": _r(amount_p)})
        tb = sum(bal.values())
        if tb: alarm("trial_balance", f"the trial balance is off by Rs {to_rupees(tb):,.2f}", tb)
        for code, label in ((A.DRIVER_CASH_IN_TRANSIT, "Cash with drivers"), (A.STOCK_ON_VEHICLES, "Stock on vehicles"), (A.GOODS_RECEIVED_NOT_BILLED, "Goods received not billed")):
            if by.get(code): alarm(f"clearing_{code}", f"{label} ({code}) is Rs {to_rupees(by[code]):,.2f}, not zero", by[code])
        if by.get(A.UNASSIGNED_MONEY): alarm("unassigned_money", f"Rs {to_rupees(by[A.UNASSIGNED_MONEY]):,.2f} of money has no account (route bank / cheque / wallet payments)", by[A.UNASSIGNED_MONEY])
        money: dict[str, int] = {}
        for (code, acct, *_), v in bal.items():
            if code == A.MONEY: money[acct] = money.get(acct, 0) + v
        for acct, v in money.items():
            if v < 0: alarm("negative_money", f"{names.get(acct, {}).get('name', acct)} is negative (Rs {to_rupees(v):,.2f})", v)
        khata = int(self._one(f"SELECT COALESCE(SUM(amount), 0) s FROM ledger WHERE {sql_business_date('created_at')} <= ?", (as_of,))["s"])
        if by.get(A.TRADE_RECEIVABLES, 0) != khata: alarm("receivables", f"receivables in the ledger (Rs {to_rupees(by.get(A.TRADE_RECEIVABLES, 0)):,.2f}) differ from the khata (Rs {to_rupees(khata):,.2f})", by.get(A.TRADE_RECEIVABLES, 0) - khata)
        sup = int(self._one(f"SELECT COALESCE(SUM(amount), 0) s FROM supplier_ledger WHERE {sql_business_date('created_at')} <= ?", (as_of,))["s"])
        if -by.get(A.TRADE_PAYABLES, 0) != sup: alarm("payables", f"payables in the ledger (Rs {to_rupees(-by.get(A.TRADE_PAYABLES, 0)):,.2f}) differ from the supplier khata (Rs {to_rupees(sup):,.2f})", -by.get(A.TRADE_PAYABLES, 0) - sup)
        if as_of >= today:
            pool = int(self._one("SELECT COALESCE(SUM(value_paisa), 0) s FROM inventory_value")["s"])
            if by.get(A.STOCK_GODOWNS, 0) != pool: alarm("stock", f"stock in the ledger (Rs {to_rupees(by.get(A.STOCK_GODOWNS, 0)):,.2f}) differs from the stock pool (Rs {to_rupees(pool):,.2f})", by.get(A.STOCK_GODOWNS, 0) - pool)
        # payroll: the PAYROLL_METHOD expense rows (what profit_summary sees) must equal the payroll run postings
        exp: dict[str, int] = {}
        for r in self._all("SELECT category, SUM(amount) s FROM expenses WHERE method=? AND expense_date <= ? GROUP BY category", (A.PAYROLL_METHOD, as_of)):
            code = A.PAYROLL_CATEGORY_CODE.get(r["category"], A.expense_code(r["category"]))
            exp[code] = exp.get(code, 0) + int(r["s"] or 0)
        runs: dict[str, int] = {}
        for p in self._payroll_postings(None, as_of):
            if p.source == "payroll_run" and (p.code in STAFF_CODES or p.code.startswith(A.OPERATING_EXPENSES + ":")):
                runs[p.code] = runs.get(p.code, 0) + p.net_paisa
        for code in set(exp) | set(runs):
            if exp.get(code, 0) != runs.get(code, 0):
                alarm("payroll_expense", f"payroll expense rows for {self._code_name(code)} (Rs {to_rupees(exp.get(code, 0)):,.2f}) differ from the payroll postings (Rs {to_rupees(runs.get(code, 0)):,.2f})",
                      exp.get(code, 0) - runs.get(code, 0))
        # closed periods: recompute each standing snapshot
        for r in self._all("SELECT close_id, through_date, snapshot FROM period_closes WHERE reopened_at IS NULL AND through_date <= ?", (as_of,)):
            snap = json.loads(r["snapshot"])
            now = self._tb_snapshot(r["through_date"])
            if now["sha256"] != snap.get("sha256"):
                changed = sorted(k for k in set(now["trial_balance"]) | set(snap.get("trial_balance", {})) if now["trial_balance"].get(k) != snap.get("trial_balance", {}).get(k))
                alarm("books_drift", f"books drift: the period closed through {r['through_date']} (close {r['close_id']}) no longer matches its snapshot ({', '.join(changed[:5])})")
        t = table(f"Books check at {as_of}", [col("code", "Check"), col("message", "What is wrong"), col("amount", "Amount", "money")], alarms,
                  lead="The books are clean." if not alarms else f"{len(alarms)} alarm{'s' if len(alarms) != 1 else ''}.")
        return {"as_of": as_of, "ok": not alarms, "alarms": alarms, "table": t}

    # ------------------------------------------------------------------ staff-cost masking for existing reports
    @staticmethod
    def _mask_staff(by_cat_paisa: dict[str, int]) -> dict[str, int]:
        return mask_staff_categories(by_cat_paisa)
