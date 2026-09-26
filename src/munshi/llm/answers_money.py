"""Readable answers for the payroll and company-finance tools (the Tankhwa and Accounts munshis), in the llm/answers.py way:
code says what the books say -- the model never states a figure. Every report from Streams A / B carries its `table` in
the chat renderer's shape (domain/accounts.table); the sentence is built here from the same result (its raw numbers, else
the table's own rows and totals), so the table and the sentence can't disagree. A statutory figure always carries the
boundary line (domain/accounts.BOUNDARY_TEXT): Munshi calculates, it doesn't advise.

Registered into answers.FORMATTERS (answers.py imports this module last). Every formatter tolerates a missing key -- the
repository shapes beyond `table` are Stream A / B's -- and never raises (answers.render_full catches anyway)."""
from __future__ import annotations

from typing import Any

from munshi.domain import accounts as ACC
from munshi.llm.answers import MAX_ROWS, _Ctx, _day, rs

_TOTAL_WORDS = {"total", "kul", "کل"}


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _attach(d: dict, c: _Ctx, lead: str, min_rows: int = 2) -> None:
    """The report's own table as this answer's table (rows capped for chat, the question's language kept on it)."""
    t = d.get("table") if isinstance(d, dict) else None
    if not isinstance(t, dict) or not isinstance(t.get("rows"), list) or len(t["rows"]) < min_rows:
        return
    rows = t["rows"][:MAX_ROWS]
    note = t.get("note")
    if len(t["rows"]) > MAX_ROWS:
        more = f"Showing the first {MAX_ROWS} of {len(t['rows'])}."
        note = f"{note} {more}" if note else more
    c.table = dict(t) | {"rows": rows, "lead": lead.rstrip(":. ") + ":", "note": note, "count": len(t["rows"]), "lang": c.lang,
                         "columns": [dict(x) for x in t.get("columns") or []]}


def _rows(d: dict) -> list[dict]:
    t = d.get("table") if isinstance(d, dict) else None
    return [r for r in (t or {}).get("rows") or [] if isinstance(r, dict)]


def _line(d: dict, *labels: str) -> float | None:
    """A statement line's amount by its label ('Net profit', 'Total assets'), read from the table."""
    want = {x.lower() for x in labels}
    for r in _rows(d):
        lab = str(r.get("line") or r.get("item") or r.get("kpi") or "").strip().lower()
        if lab in want:
            return _num(r.get("amount", r.get("value")))
    return None


def _pick(d: dict, *keys: str) -> float | None:
    for k in keys:
        v = _num(d.get(k)) if isinstance(d, dict) else None
        if v is not None:
            return v
    return None


def _money_col(d: dict, *prefer: str) -> str | None:
    t = (d.get("table") or {}) if isinstance(d, dict) else {}
    cols = [x for x in t.get("columns") or [] if x.get("kind") == "money"]
    for k in prefer:
        if any(x.get("key") == k for x in cols):
            return k
    return cols[-1]["key"] if cols else None


def _label_col(d: dict) -> str | None:
    t = (d.get("table") or {}) if isinstance(d, dict) else {}
    return next((x["key"] for x in t.get("columns") or [] if x.get("kind") == "text"), None)


def _boundary(d: dict, c: _Ctx) -> str:
    txt = ACC.BOUNDARY_TEXT["ru" if c.lang == "ru" else "en"]
    return " " + txt.format(verified_on=d.get("rules_verified_on") or d.get("verified_on") or "?", source=d.get("source") or "the rate settings")


def _who(d: dict, c: _Ctx) -> str:
    return str(d.get("name") or (d.get("employee") or {}).get("name") or "")


# ------------------------------------------------------------------ payroll reads
def _employees(d, c: _Ctx) -> str:
    rows = [e for e in (d or {}).get("employees") or [] if isinstance(e, dict)]
    if not rows:
        return c.t("No staff on the books yet.", "Abhi koi staff darj nahi.", "ابھی کوئی ملازم درج نہیں۔")
    names = ", ".join(f"{e.get('name')}" + (f" ({e.get('designation')})" if e.get("designation") else "") for e in rows[:15])
    head = c.t("{n} staff: ", "{n} staff: ", "{n} ملازم: ", n=len(rows))
    _attach(d, c, head)
    return head + names + (c.more(len(rows) - 15) if len(rows) > 15 else ".")


def _preview(d, c: _Ctx) -> str:
    return _register(d, c, booked=False)


def _register(d, c: _Ctx, booked: bool = True) -> str:
    """A preview ({period, employees, totals, warnings, errors}) or an approved register ({run: {...}, payslips})."""
    d = d or {}
    run = d.get("run") if isinstance(d.get("run"), dict) else {}
    people = [x for x in d.get("employees") or d.get("payslips") or d.get("lines") or [] if isinstance(x, dict)]
    net = _pick(run, "net") if run else None
    if net is None:
        net = _pick(d.get("totals") or {}, "net")
    if net is None:
        net = _pick(d, "total_net") or sum(_num(x.get("net", x.get("net_pay"))) or 0 for x in people)
    period = run.get("period") or d.get("period") or ""
    ids = [x for x in (c.args.get("employee_ids") or []) if x]
    if ids and len(people) == 1:
        x = people[0]
        comm = x.get("commission")
        if comm is None:
            comm = sum(_num(ln.get("amount")) or 0 for ln in x.get("lines") or [] if isinstance(ln, dict) and ln.get("code") == "COMM")
        extra = f", commission {rs(comm)}" if _num(comm) else ""
        return c.t("{who} for {p}: gross {g}{extra}, net pay {n} (not booked yet).", "{who} {p}: gross {g}{extra}, net {n} (abhi book nahi hui).",
                   who=x.get("name") or "", p=period, g=rs(x.get("gross")), extra=extra, n=rs(x.get("net", x.get("net_pay"))))
    k = int(run.get("headcount") or 0) or len(people)
    head = (c.t("Salary register {p}: {k} staff, net pay {n} in all.", "Tankhwa register {p}: {k} staff, kul net {n}.", "تنخواہ رجسٹر {p}: {k} ملازم، کل {n}۔",
                p=period, k=k, n=rs(net)) if booked else
            c.t("Payroll for {p} (not booked yet): {k} staff, net pay {n} in all.", "{p} ki tankhwa (abhi book nahi hui): {k} staff, kul net {n}.",
                "{p} کی تنخواہ (ابھی درج نہیں): {k} ملازم، کل {n}۔", p=period, k=k, n=rs(net)))
    if booked and run:
        paid = sum(_num(x.get("paid")) or 0 for x in people)
        head += c.t(" Paid {a}, still due {b}.", " {a} de diye, {b} baqi.", " {a} ادا، {b} باقی۔", a=rs(paid), b=rs(max((net or 0) - paid, 0)))
    notes = [str(w) for w in (d.get("errors") or [])][:2] + [str(w) for w in (d.get("warnings") or [])][:3]
    _attach(d, c, head)
    return head + (" " + "; ".join(notes) + "." if notes else "")


def _payslip(d, c: _Ctx) -> str:
    d = d or {}
    meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
    _attach(d, c, c.t("Payslip", "Salary slip", "سیلری سلپ"))
    return c.t("{who}'s payslip for {p}: net pay {n} ({st}).", "{who} ki salary slip {p}: net {n} ({st}).", "{who} کی سیلری سلپ {p}: خالص {n} ({st})۔",
               who=meta.get("name") or _who(d, c), p=meta.get("month") or meta.get("period") or d.get("period") or "",
               n=rs(_pick(d, "net", "net_pay", "total_payment")), st=d.get("status") or meta.get("paid_status") or "")


def _my_slips(d, c: _Ctx) -> str:
    d = d or {}
    if d.get("signed_out"):
        return c.t("Sign in as yourself to see your own payslips.", "Apni salary slip dekhne ke liye apne naam se sign in karein.",
                   "اپنی سیلری سلپ دیکھنے کے لیے اپنے نام سے سائن ان کریں۔")
    slips = [s for s in d.get("payslips") or d.get("slips") or [] if isinstance(s, dict)]
    if not slips:
        return c.t("There is no payslip linked to your login yet -- ask the owner.", "Aap ke login se abhi koi salary slip judi nahi -- owner se poochein.",
                   "آپ کے لاگ ان سے ابھی کوئی سیلری سلپ منسلک نہیں -- مالک سے پوچھیں۔")
    s0 = slips[0]
    head = c.t("Your payslip for {p}: net pay {n}.", "Aap ki salary slip {p}: net {n}.", "آپ کی سیلری سلپ {p}: خالص {n}۔",
               p=s0.get("month") or s0.get("period") or "", n=rs(_pick(s0, "net", "net_pay", "total_payment")))
    _attach(d, c, head)
    return head + (c.t(" {k} earlier slips below.", " {k} pichli slips neeche.", " {k} پچھلی سلپیں نیچے۔", k=len(slips) - 1) if len(slips) > 1 else "")


def _advances(d, c: _Ctx) -> str:
    d = d or {}
    rows = [r for r in d.get("advances") or _rows(d) if isinstance(r, dict) and not r.get("_em")]
    total = _pick(d, "outstanding", "total_outstanding")
    if total is None:
        total = sum(_num(r.get("outstanding")) or 0 for r in rows)
    if not rows:
        return c.t("No open staff advance.", "Koi advance baqi nahi.", "کوئی ایڈوانس باقی نہیں۔")
    parts = "; ".join(f"{r.get('name')} {rs(r.get('outstanding'))}" for r in rows[:8])
    head = c.t("Staff advances outstanding {t}: ", "Staff advance baqi {t}: ", "ملازمین کا ایڈوانس باقی {t}: ", t=rs(total))
    _attach(d, c, head)
    return head + parts + "."


def _statutory(d, c: _Ctx) -> str:
    d = d or {}
    kind = {"eobi": "EOBI", "ss": "Social security", "income_tax": "Salary tax withheld"}.get(str(d.get("kind") or c.args.get("kind")), "Statutory")
    due = _pick(d, "due", "total", "amount")
    head = c.t("{k} for {p}: {t} due", "{k} {p}: {t} jama karwane hain", "{k} {p}: {t} جمع کروانے ہیں", k=kind, p=d.get("period") or "", t=rs(due))
    if _pick(d, "paid"):
        head += c.t(", {a} paid, {b} still to pay", ", {a} jama ho chuke, {b} baqi", "، {a} جمع، {b} باقی", a=rs(d.get("paid")), b=rs(d.get("balance")))
    head += "."
    if not d.get("run_id") and "run_id" in d:
        head += c.t(" That month's payroll isn't approved yet.", " Is mahine ki tankhwa abhi approve nahi hui.", " اس مہینے کی تنخواہ ابھی منظور نہیں ہوئی۔")
    _attach(d, c, head)
    return head + (" " + str(d["boundary"]) if d.get("boundary") else _boundary(d, c))


# ------------------------------------------------------------------ payroll writes (after approval)
def _attendance(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("Attendance recorded for {p}: {n} people.", "Hazri darj {p}: {n} log.", "حاضری درج {p}: {n} افراد۔", p=d.get("period") or c.args.get("period") or "",
               n=len(d.get("rows") or c.args.get("rows") or []))


def _run_approved(d, c: _Ctx) -> str:
    d = d or {}
    run = d.get("run") if isinstance(d.get("run"), dict) else d
    return c.t("Payroll for {p} approved ({r}): net pay {n} is now owed to staff.", "{p} ki tankhwa approve ({r}): staff ko {n} dene hain.",
               p=run.get("period") or c.args.get("period") or "", r=run.get("run_id") or "", n=rs(_pick(run, "net", "total_net")))


def _paid(d, c: _Ctx) -> str:
    d = d or {}
    pays = [p for p in d.get("payments") or [] if isinstance(p, dict)]
    total = _pick(d, "total") or sum(_num(p.get("amount")) or 0 for p in pays)
    return c.t("Salaries paid: {k} people, {t} in all.", "Tankhwa di gayi: {k} log, kul {t}.", "تنخواہ ادا: {k} افراد، کل {t}۔", k=len(pays), t=rs(total))


def _advance_given(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("Advance given: {w} {a} by {m} ({i}).", "Advance diya: {w} ko {a} {m} se ({i}).", w=_who(d, c), a=rs(d.get("amount")),
               m=c.w(d.get("method") or c.args.get("method")), i=d.get("advance_id") or "")


def _employee_added(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("{w} added as {des} ({no}).", "{w} {des} ke taur par shamil ({no}).", w=d.get("name") or c.args.get("name") or "",
               des=d.get("designation") or c.args.get("designation") or "staff", no=d.get("emp_no") or d.get("employee_id") or "")


def _employment_ended(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("{w}'s employment ended on {day}.", "{w} ki mulazmat {day} ko khatam.", w=d.get("name") or "", day=_day(d.get("left_on")))


def _pay_set(d, c: _Ctx) -> str:
    d = d or {}
    try:
        who = c.repo.get_employee(str(c.args.get("employee_id") or ""))["name"]
    except Exception:
        who = ""
    return c.t("Pay set{w}: {a} {b} from {day}.", "Tankhwa set{w}: {a} {b}, {day} se.", w=f" for {who}" if who else "",
               a=rs(d.get("basic") or d.get("daily_rate")), b="a month" if (d.get("pay_basis") or "monthly") == "monthly" else "a day",
               day=_day(d.get("effective_from")))


def _adjusted_pay(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("{code} of {a} added to {p} pay ({i}).", "{code} {a} {p} ki tankhwa mein ({i}).", code=str(d.get("code") or c.args.get("code") or "").replace("_", " "),
               a=rs(d.get("amount") or c.args.get("amount")), p=d.get("period") or "", i=d.get("adj_id") or "")


# ------------------------------------------------------------------ accounts
def _accounts(d, c: _Ctx) -> str:
    d = d or {}
    rows = [a for a in d.get("accounts") or [] if isinstance(a, dict)]
    if not rows:
        return c.t("No money account is set up yet.", "Abhi koi account nahi bana.", "ابھی کوئی اکاؤنٹ نہیں بنا۔")
    parts = "; ".join(f"{a.get('name') or a.get('account')} {rs(a.get('balance'))}" for a in rows[:10])
    total = _pick(d, "total") or sum(_num(a.get("balance")) or 0 for a in rows)
    head = c.t("Money now: ", "Paisa abhi: ", "رقم ابھی: ")
    _attach(d, c, head + rs(total))
    # bank/wallet receipts and payments with no account of their kind sit in "unassigned": say so, or the total
    # (accounts + unassigned) looks wrong next to the accounts listed
    un = _num(d.get("unassigned")) or 0
    note = c.t(" Rs {u} is bank/wallet money with no account yet: add the bank or wallet account in the office (Accounts & banks).",
               " Rs {u} bank/wallet ka paisa kisi account mein nahi: office mein (Accounts & banks) bank ya wallet account bana dein.",
               " {u} روپے بینک/والٹ کی رقم کسی اکاؤنٹ میں نہیں: آفس میں (Accounts & banks) بینک یا والٹ اکاؤنٹ بنا دیں۔",
               u=f"{un:,.0f}") if un else ""
    return head + parts + c.t(" -- {t} in all.", " -- kul {t}.", " -- کل {t}۔", t=rs(total)) + note


def _book(d, c: _Ctx) -> str:
    d = d or {}
    rows = _rows(d)
    closing = _pick(d, "closing", "balance")
    if closing is None and rows:
        closing = _num(rows[-1].get("balance"))
    head = c.t("{a}: {k} entries, balance {b}.", "{a}: {k} entries, baqaya {b}.", "{a}: {k} اندراج، بقایا {b}۔", a=d.get("name") or c.args.get("account_id") or "",
               k=len(rows), b=rs(closing))
    _attach(d, c, head)
    return head


def _statement(title_en: str, title_ru: str, keys: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]):
    """A statement's sentence: each (label, raw keys, table line labels) that the result has."""
    def fmt(d, c: _Ctx) -> str:
        d = d or {}
        parts = []
        for label, raw, lines in keys:
            v = _pick(d, *raw)
            if v is None:
                v = _line(d, *lines)
            if v is not None:
                parts.append(f"{label} {rs(v)}")
        period = f" {d.get('start')} to {d.get('end')}" if d.get("start") and d.get("end") else (f" as of {d.get('as_of')}" if d.get("as_of") else "")
        head = c.t(title_en, title_ru) + period + ": " + ("; ".join(parts) if parts else c.t("see the table", "table dekhein")) + "."
        _attach(d, c, head)
        return head
    return fmt


_income = _statement("Profit and loss", "Munafa nuqsan", (("net revenue", ("net_revenue",), ("net revenue",)),
                                                          ("gross profit", ("gross_profit",), ("gross profit",)),
                                                          ("net profit", ("net_profit",), ("net profit",))))
_balance = _statement("Balance sheet", "Balance sheet", (("assets", ("total_assets",), ("total assets",)),
                                                          ("liabilities", ("total_liabilities",), ("total liabilities",)),
                                                          ("equity", ("total_equity",), ("total equity",))))
_cashflow = _statement("Cash flow", "Cash flow", (("money at start", ("opening",), ("opening money",)), ("net change", ("net_change",), ("net change",)),
                                                   ("money at end", ("closing",), ("closing money",))))
_trial = _statement("Trial balance", "Trial balance", (("debits", ("total_debit",), ()), ("credits", ("total_credit",), ())))


def _generic_report(title_en: str, title_ru: str):
    def fmt(d, c: _Ctx) -> str:
        d = d or {}
        rows = [r for r in _rows(d) if not r.get("_em")]
        lab, mc = _label_col(d), _money_col(d, "amount", "balance", "margin", "outstanding", "book_value")
        if rows and lab:
            shown = rows[:6]
            parts = "; ".join(f"{r.get(lab)}" + (f" {rs(r.get(mc))}" if mc and r.get(mc) is not None else
                                                  (f" {r.get('value')}{(' ' + str(r.get('unit'))) if r.get('unit') else ''}" if r.get("value") is not None else ""))
                              for r in shown)
            head = c.t(title_en, title_ru) + ": " + parts + (c.more(len(rows) - 6) if len(rows) > 6 else ".")
        else:
            head = c.t(title_en, title_ru) + c.t(": nothing to show yet.", ": abhi kuch nahi.")
        _attach(d, c, head)
        return head
    return fmt


def _moved(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("Moved {a} from {f} to {t} ({i}).", "{a} {f} se {t} mein ({i}).", a=rs(d.get("amount") or c.args.get("amount")),
               f=d.get("from_account") or c.args.get("from_account") or "", t=d.get("to_account") or c.args.get("to_account") or "", i=d.get("transfer_id") or "")


def _counted(d, c: _Ctx) -> str:
    d = d or {}
    diff = _num(d.get("difference"))
    tail = (c.t(" It matches the book.", " Book se barabar.") if diff == 0 else
            c.t(" The book says {b}: {x} {ov}. The owner decides whether to book the difference.", " Book {b}: {x} {ov}.", b=rs(d.get("book")),
                x=rs(abs(diff or 0)), ov=c.t("over" if (diff or 0) > 0 else "short", "zyada" if (diff or 0) > 0 else "kam"))) if diff is not None else ""
    return c.t("Cash counted: {a} ({i}).", "Cash gina: {a} ({i}).", a=rs(d.get("counted") or c.args.get("counted")), i=d.get("count_id") or "") + tail


def _journal(what_en: str, what_ru: str):
    def fmt(d, c: _Ctx) -> str:
        d = d or {}
        amt = _pick(d, "amount", "cost", "principal")
        return c.t(what_en, what_ru) + (f" {rs(amt)}" if amt else "") + (f" ({d.get('je_id') or d.get('loan_id') or d.get('asset_id')})"
                                                                         if (d.get("je_id") or d.get("loan_id") or d.get("asset_id")) else "") + "."
    return fmt


def _closed(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("Books closed through {day}: nothing in that period can change now.", "Hisaab {day} tak band: us muddat mein ab kuch nahi badlega.",
               day=_day(d.get("through_date") or c.args.get("through_date")))


def _done(d, c: _Ctx) -> str:
    d = d or {}
    ref = next((str(v) for k, v in d.items() if k.endswith("_id") and isinstance(v, (str, int))), "")
    return c.t("Done", "Ho gaya") + (f" ({ref})." if ref else ".")


FORMATTERS = {
    "list_employees": _employees, "find_employee": lambda d, c: _employees({"employees": (d or {}).get("candidates") or []}, c),
    "payroll_preview": _preview, "payroll_register": _register, "payslip": _payslip, "my_payslips": _my_slips,
    "staff_advances_report": _advances, "statutory_summary": _statutory,
    "record_attendance": _attendance, "approve_payroll_run": _run_approved, "pay_salaries": _paid, "give_staff_advance": _advance_given,
    "add_employee": _employee_added, "end_employment": _employment_ended, "add_payroll_adjustment": _adjusted_pay, "set_pay_structure": _pay_set,
    "money_accounts": _accounts, "account_book": _book, "income_statement": _income, "balance_sheet": _balance, "cash_flow": _cashflow,
    "trial_balance": _trial, "owner_kpis": _generic_report("Your numbers", "Aap ke numbers"), "margins_report": _generic_report("Margin", "Margin"),
    "fixed_assets_register": _generic_report("Fixed assets", "Assets"), "loans_report": _generic_report("Loans", "Qarz"),
    "period_status": _generic_report("Periods", "Mahine"), "reconciliation_status": _generic_report("Reconciliation", "Reconciliation"),
    "transfer_between_accounts": _moved, "count_cash": _counted, "record_drawing": _journal("Drawing recorded", "Drawing darj"),
    "record_capital": _journal("Capital recorded", "Capital darj"), "record_loan": _journal("Loan recorded", "Qarz darj"),
    "repay_loan": _journal("Loan repayment recorded", "Qarz ki wapsi darj"), "add_fixed_asset": _journal("Asset added", "Asset darj"),
    "run_depreciation": _journal("Depreciation booked", "Depreciation darj"), "close_period": _closed,
}
for _t in ("update_employee", "rehire_employee", "set_pay_structure", "set_commission_rule", "void_payroll_adjustment", "reverse_payroll_run",
           "reverse_salary_payment", "repay_staff_advance", "reverse_staff_advance", "record_statutory_payment", "add_statutory_rate",
           "set_payroll_settings", "mark_cleared", "save_reconciliation", "add_money_account", "set_method_route", "dispose_fixed_asset",
           "post_journal_entry", "reverse_journal_entry", "reverse_account_transfer", "post_cash_difference", "record_opening_balances",
           "reopen_period", "list_attachments"):
    FORMATTERS.setdefault(_t, _done)
