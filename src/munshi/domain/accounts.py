"""The frozen vocabulary of payroll, company finance and payment proofs (Stream 0 seams).

Every stream (A payroll, B finance, C console, D chat, E payment proofs) imports its names from HERE, so the strings
that cross a stream boundary -- chart codes, audit actions, document series, settings keys, the table shape -- are
spelled once. Changing anything in this file is a change request to the tech lead, not an edit a stream makes.
Pure module: no database, no framework, no LLM.

THE OWNER'S DECISIONS (binding; 2026-09-26)
 1. Payroll follows the NEW Punjab Labour Code 2026 limits BY DEFAULT. `payroll_profile` defaults to 'plc_2026'
    (the plan had 'legacy_1969'); switching it to 'legacy_1969' restores the old-law behaviour. Under plc_2026 the
    engine ENFORCES (refuses, not just warns -- see PLC_LIMITS):
      * a staff advance above 3 x the monthly minimum wage, or a second advance while one is still open;
      * an advance instalment above 20% of that month's pay;
      * a fine above 3% of that period's pay (legacy: 3.125%, stored as 312 bp);
      * a salary or advance paid in cash -- only CASHLESS_METHODS (bank, wallet, cheque);
      * the payslip carries the prescribed PLC s.165(15) content (PAYSLIP_SECTIONS), leave balances included.
    Every refusal names the switch: "... Punjab Labour Code 2026 limit; the owner can switch payroll to the old
    law in Settings". (Open question for the owner, raised by Stream 0: daily-wage loaders paid in cash at day's end
    are refused under plc_2026 -- confirm, or allow a per-employee cash exemption.)
 2. Salaries are visible to the OWNER ONLY: the payroll register, other people's payslips, pay structures,
    commission rules, advances and statutory summaries. A clerk may RECORD ATTENDANCE (days, leave, OT minutes,
    trips) but never sees a rupee of pay; every employee sees only their OWN payslips. Encoded as permissions in
    auth/principal.py (payroll:* / attendance:write / employees:read) and, for chat, as TOOL_PERMISSION below.
    Surfaces shared with clerks must not leak pay either -- see PAYROLL_REDACTED_LABEL.
 3. Salary cost is booked when a payroll run is APPROVED (accrual): Dr staff costs (6100-6200) / Cr 2100 salaries
    payable (+ 2110/2120/2130 withholdings, 1150 advance recovery, 2140 fines). Salary payments later clear 2100, so
    unpaid salary shows on the balance sheet as owed to staff. Approval also writes the PAYROLL_METHOD expense rows
    (one per SYSTEM_EXPENSE_CATEGORIES category, dated the period end) so profit_summary includes staff cost.
 4. (Stream D) A clerk's chat request for an owner-tier (HIGH_RISK) action becomes an approval card for the owner --
    the existing approval flow already does this; nothing here blocks it.
"""
from __future__ import annotations

from dataclasses import dataclass

# ============================================================================ chart of accounts (plan §4.4)
MONEY = "1000"                    # a money account; the line names which one in Posting.money_account_id
DRIVER_CASH_IN_TRANSIT = "1050"   # clearing: collected at stops - hand-ins - booked shortages -> 0
TRADE_RECEIVABLES = "1100"
STAFF_ADVANCES = "1150"
STOCK_GODOWNS = "1200"
STOCK_ON_VEHICLES = "1210"        # clearing: loaded - delivered - returned -> 0
FIXED_ASSETS_COST = "1500"
ACCUMULATED_DEPRECIATION = "1510"
UNASSIGNED_MONEY = "1900"         # a method with no route: every report flags it
TRADE_PAYABLES = "2000"
GOODS_RECEIVED_NOT_BILLED = "2050"  # clearing: purchased stock value - bills -> 0
SALARIES_PAYABLE = "2100"
INCOME_TAX_WITHHELD = "2110"
EOBI_PAYABLE = "2120"
SOCIAL_SECURITY_PAYABLE = "2130"
STAFF_WELFARE_FUND = "2140"       # fines collected
LOANS_PAYABLE = "2200"
OTHER_PAYABLES = "2300"
OWNER_CAPITAL = "3000"
OPENING_BALANCE_EQUITY = "3010"
DRAWINGS = "3100"
RETAINED_EARNINGS = "3200"        # derived, never posted to
SALES = "4000"
SALES_RETURNS = "4010"
COST_OF_GOODS_SOLD = "5000"
STOCK_ADJUSTMENTS = "5100"
OPERATING_EXPENSES = "6000"       # per category: expense_code(category) -> '6000:fuel'
SALARIES = "6100"
WAGES = "6110"
ALLOWANCES = "6120"
COMMISSION = "6130"
BONUS = "6140"
EMPLOYER_CONTRIBUTIONS = "6200"
DEPRECIATION = "6300"
INTEREST_AND_BANK_CHARGES = "7000"
OTHER_INCOME = "8000"
CASH_OVER_SHORT = "8100"

# code -> (name, type); type in asset | liability | equity | revenue | expense. Normal side: debit for asset/expense.
CHART: dict[str, tuple[str, str]] = {
    MONEY: ("Money accounts", "asset"),
    DRIVER_CASH_IN_TRANSIT: ("Cash with drivers", "asset"),
    TRADE_RECEIVABLES: ("Trade receivables", "asset"),
    STAFF_ADVANCES: ("Staff advances and loans", "asset"),
    STOCK_GODOWNS: ("Stock in godowns", "asset"),
    STOCK_ON_VEHICLES: ("Stock on vehicles", "asset"),
    FIXED_ASSETS_COST: ("Fixed assets at cost", "asset"),
    ACCUMULATED_DEPRECIATION: ("Accumulated depreciation", "asset"),     # contra-asset (credit balance)
    UNASSIGNED_MONEY: ("Unassigned money", "asset"),
    TRADE_PAYABLES: ("Trade payables", "liability"),
    GOODS_RECEIVED_NOT_BILLED: ("Goods received not billed", "liability"),
    SALARIES_PAYABLE: ("Salaries payable", "liability"),
    INCOME_TAX_WITHHELD: ("Income tax withheld", "liability"),
    EOBI_PAYABLE: ("EOBI payable", "liability"),
    SOCIAL_SECURITY_PAYABLE: ("Social security payable", "liability"),
    STAFF_WELFARE_FUND: ("Staff welfare fund", "liability"),
    LOANS_PAYABLE: ("Loans payable", "liability"),
    OTHER_PAYABLES: ("Other payables", "liability"),
    OWNER_CAPITAL: ("Owner's capital", "equity"),
    OPENING_BALANCE_EQUITY: ("Opening balance equity", "equity"),
    DRAWINGS: ("Drawings", "equity"),
    RETAINED_EARNINGS: ("Retained earnings", "equity"),
    SALES: ("Sales", "revenue"),
    SALES_RETURNS: ("Sales returns and allowances", "revenue"),
    COST_OF_GOODS_SOLD: ("Cost of goods sold", "expense"),
    STOCK_ADJUSTMENTS: ("Stock adjustments and write-offs", "expense"),
    OPERATING_EXPENSES: ("Operating expenses", "expense"),
    SALARIES: ("Salaries", "expense"),
    WAGES: ("Wages", "expense"),
    ALLOWANCES: ("Allowances", "expense"),
    COMMISSION: ("Commission", "expense"),
    BONUS: ("Bonus", "expense"),
    EMPLOYER_CONTRIBUTIONS: ("Employer contributions", "expense"),
    DEPRECIATION: ("Depreciation", "expense"),
    INTEREST_AND_BANK_CHARGES: ("Interest and bank charges", "expense"),
    OTHER_INCOME: ("Other income", "revenue"),
    CASH_OVER_SHORT: ("Cash over / short", "expense"),
}
CLEARING_ACCOUNTS = (DRIVER_CASH_IN_TRANSIT, STOCK_ON_VEHICLES, GOODS_RECEIVED_NOT_BILLED)   # must net to zero


def expense_code(category: str) -> str:
    """The chart code of an operating-expense category: '6000:fuel'. Its parent is OPERATING_EXPENSES."""
    return f"{OPERATING_EXPENSES}:{category}"


def base_code(code: str) -> str:
    """'6000:fuel' -> '6000' (CHART lookups)."""
    return code.split(":", 1)[0]


@dataclass(frozen=True)
class Posting:
    """One side of a balanced entry, derived from a source row (plan §2, §4.4). Integer paisa; exactly one of
    debit/credit is non-zero (a negative source amount -- a reversal -- swaps sides, it never posts a negative).
    `on` is the business date (YYYY-MM-DD). `money_account_id` is set iff code == MONEY."""
    on: str
    code: str
    debit_paisa: int = 0
    credit_paisa: int = 0
    money_account_id: str | None = None
    party_kind: str | None = None      # customer | supplier | employee | loan | asset | owner
    party_id: str | None = None
    source: str = ""                   # 'ledger', 'expense', 'payroll_run', 'salary_payment', 'journal', ...
    source_id: str = ""
    memo: str = ""

    def __post_init__(self) -> None:
        for v in (self.debit_paisa, self.credit_paisa):
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"posting amounts are non-negative integer paisa, got {v!r}")
        if (self.debit_paisa == 0) == (self.credit_paisa == 0):
            raise ValueError("a posting has exactly one non-zero side")
        if (self.code == MONEY) != (self.money_account_id is not None):
            raise ValueError("a money posting (1000) names its money account, and only a money posting does")
        if base_code(self.code) not in CHART:
            raise ValueError(f"unknown chart code {self.code!r}")
        if len(self.on) != 10:
            raise ValueError(f"posting date must be YYYY-MM-DD, got {self.on!r}")

    @property
    def net_paisa(self) -> int:
        """Debit-positive signed amount."""
        return self.debit_paisa - self.credit_paisa


def signed(on: str, debit: str, credit: str, amount_paisa: int, **kw) -> list[Posting]:
    """The two postings of `amount_paisa` from `credit` to `debit`; a negative amount (a reversal) swaps the sides.
    kw: source, source_id, memo, and debit_/credit_ prefixed money_account_id / party_kind / party_id."""
    if amount_paisa == 0:
        return []
    dr, cr, amt = (debit, credit, amount_paisa) if amount_paisa > 0 else (credit, debit, -amount_paisa)
    side = {"d": "debit_" if amount_paisa > 0 else "credit_", "c": "credit_" if amount_paisa > 0 else "debit_"}
    common = {k: kw[k] for k in ("source", "source_id", "memo") if k in kw}
    def extra(prefix: str) -> dict:
        return {k: kw[prefix + k] for k in ("money_account_id", "party_kind", "party_id") if prefix + k in kw}
    return [Posting(on, dr, debit_paisa=amt, **common, **extra(side["d"])),
            Posting(on, cr, credit_paisa=amt, **common, **extra(side["c"]))]


# ============================================================================ money accounts and methods (V8)
MONEY_METHODS = ("cash", "bank", "jazzcash", "easypaisa", "cheque")
CASHLESS_METHODS = ("bank", "jazzcash", "easypaisa", "cheque")    # PLC s.18 / s.165(5): banking or digital channels
MONEY_ACCOUNT_KINDS = ("cash", "bank", "wallet")
CASH_ACCOUNT_ID = "CASH"             # seeded by V8: 'Cash in hand (galla)'
UNASSIGNED_ACCOUNT_ID = "UNASSIGNED"  # virtual; resolve_account() for a method with no route (chart 1900)

# ============================================================================ payroll <-> expenses (plan §4.4)
PAYROLL_METHOD = "payroll"           # expenses.method of the rows an approved payroll run writes
# Categories only payroll approval writes (method='payroll'). record_expense REFUSES them (Stream B); reverse_expense
# refuses any method='payroll' row ("reverse payroll run PAY-..."). DEVIATION from the plan: the plan reused 'salary',
# but 'salary' is a hand-keyable category today (cash.EXPENSE_CATEGORIES) that clerks and chat use -- refusing it
# would break existing flows. Hand-keyed 'salary' stays legal; payroll_enabled=1 warns about the double count.
SYSTEM_EXPENSE_CATEGORIES = ("staff_salaries", "staff_wages", "staff_allowances", "staff_commission", "staff_bonus",
                             "employer_contributions")
PAYROLL_CATEGORY_CODE = {"staff_salaries": SALARIES, "staff_wages": WAGES, "staff_allowances": ALLOWANCES,
                         "staff_commission": COMMISSION, "staff_bonus": BONUS, "employer_contributions": EMPLOYER_CONTRIBUTIONS}
RECOVERY_NOTE_PREFIX = "RECOVERY"    # a LOSS line's negative cash_shortage row: note 'RECOVERY <plan_id> <run_id>'

# ============================================================================ payroll profile and settings (decision 1)
PROFILE_PLC_2026 = "plc_2026"
PROFILE_LEGACY_1969 = "legacy_1969"
PAYROLL_PROFILES = (PROFILE_PLC_2026, PROFILE_LEGACY_1969)
DEFAULT_PAYROLL_PROFILE = PROFILE_PLC_2026
# Limits the engine ENFORCES per profile (basis points of the period's pay, or multiples of the monthly minimum wage).
# Values are also seeded as statutory_rates rows (A) so a rate change is data, not code; these are the fallbacks.
PLC_LIMITS = {"advance_cap_min_wages": 3, "advance_instalment_cap_bp": 2000, "fine_cap_bp": 300,
              "one_open_advance": True, "cashless_only": True, "ot_multiplier_x100": 200, "holiday_ot_multiplier_x100": 300}
LEGACY_LIMITS = {"advance_cap_min_wages": None, "advance_instalment_cap_bp": None, "fine_cap_bp": 312,
                 "one_open_advance": False, "cashless_only": False, "ot_multiplier_x100": 200, "holiday_ot_multiplier_x100": 200,
                 "deduction_cap_bp_warn": 5000}
PROFILE_LIMITS = {PROFILE_PLC_2026: PLC_LIMITS, PROFILE_LEGACY_1969: LEGACY_LIMITS}
# settings keys (existing `settings` table) and their defaults. Read with repo.setting(key, PAYROLL_SETTINGS_DEFAULTS[key]);
# base.DEFAULT_SETTINGS is NOT extended (not a Stream 0 file), so repo.settings() omits them until set.
# DEVIATION: the plan's `payroll_clerk_access` setting is dropped -- decision 2 makes clerk access fixed (attendance only).
PAYROLL_SETTINGS_DEFAULTS: dict[str, str] = {
    "payroll_profile": DEFAULT_PAYROLL_PROFILE,
    "payroll_province": "punjab",
    "eobi_registered": "0",
    "ss_registered": "0",
    "payroll_pay_day": "7",
    "payroll_tax_round_rupee": "1",
    "payroll_enabled": "0",
    "finance_start_date": "",
}
PAYROLL_PROVINCES = ("punjab", "sindh", "kp", "balochistan", "ict")
# PLC s.165(15) payslip content, in order (A builds the payslip table's rows in this order, §7.1)
PAYSLIP_SECTIONS = ("basic", "allowances", "gross", "deductions", "total_deductions", "net", "other_payments",
                    "total_payment", "leave_balances")
BOUNDARY_TEXT = {
    "en": ("Rates are settings, last checked on {verified_on} from {source}. Munshi calculates; it does not give tax or "
           "legal advice. Review with your tax advisor before filing or paying."),
    "ru": ("Yeh rates settings hain, aakhri dafa {verified_on} ko check kiye gaye. Munshi hisaab lagata hai, qanooni ya tax "
           "mashwara nahi deta. Jama karwane se pehle apne tax advisor se tasdeeq karwa lein."),
}
# What a surface shared with non-owners (account books, cashbook, audit list, approval queue for clerks) shows in place
# of a salary / advance / statutory line's employee and amount detail. Decision 2. Stream B: account_book(... ,
# redact_payroll=True) for callers without payroll:read aggregates these lines per day under this label.
PAYROLL_REDACTED_LABEL = "Staff payments (owner only)"

# ============================================================================ document series (numbering.DOC_SERIES)
# series name -> prefix. DEVIATIONS from the plan: statutory is 'STY' (the plan's 'STP' is the delivery-stop id prefix,
# new_id("STP"), and chat parses STP-... as a stop); transfer is 'XFR' (the plan's 'TRF' is already the stock-transfer
# ref, new_id("TRF") in master.transfer_stock).
SERIES_PAYROLL, SERIES_PAYSLIP, SERIES_SALARY_PAYMENT = "payroll", "payslip", "salary_payment"
SERIES_ADVANCE, SERIES_STATUTORY, SERIES_JOURNAL, SERIES_TRANSFER = "advance", "statutory", "journal", "transfer"
MONEY_SERIES = {SERIES_PAYROLL: "PAY", SERIES_PAYSLIP: "PSL", SERIES_SALARY_PAYMENT: "SPM", SERIES_ADVANCE: "ADV",
                SERIES_STATUTORY: "STY", SERIES_JOURNAL: "JV", SERIES_TRANSFER: "XFR"}
# random (non-gapless) master-data ids: new_id(prefix)
ID_PREFIX = {"employee": "EMP", "commission_rule": "COM", "adjustment": "ADJ", "loan": "LN", "fixed_asset": "FA",
             "cash_count": "CC", "attachment": "ATT"}

# ============================================================================ audit actions (the safety audit's contract)
# gated tool -> the ONE audit action its approved execution writes (eval/run_eval.py ACTION_TOOL maps it back; the
# safety audit consumes one approval_granted per mapped row). Rules for A/B/E:
#  * a gated repository write called by an agent writes EXACTLY ONE row with this action (a batch -- pay_salaries,
#    record_attendance for many people, an Eid-bonus card -- is still one row, the details in its payload);
#  * any extra rows the same call writes use SECONDARY_ACTIONS (never mapped, never consuming an approval);
#  * the audit payload never holds a PIN, a full CNIC, or bytes of an attachment.
AUDIT_ACTION: dict[str, str] = {t: t for t in (
    # payroll (Stream A)
    "record_attendance", "add_employee", "update_employee", "rehire_employee", "set_pay_structure", "set_commission_rule",
    "end_employment", "add_payroll_adjustment", "void_payroll_adjustment", "approve_payroll_run", "reverse_payroll_run",
    "pay_salaries", "reverse_salary_payment", "give_staff_advance", "repay_staff_advance", "reverse_staff_advance",
    "record_statutory_payment", "add_statutory_rate", "set_payroll_settings",
    # finance (Stream B)
    "transfer_between_accounts", "count_cash", "mark_cleared", "save_reconciliation", "add_money_account",
    "set_method_route", "record_capital", "record_drawing", "record_loan", "repay_loan", "add_fixed_asset",
    "dispose_fixed_asset", "run_depreciation", "post_journal_entry", "reverse_journal_entry", "reverse_account_transfer",
    "post_cash_difference", "record_opening_balances", "close_period", "reopen_period")}
SECONDARY_ACTIONS = frozenset({
    "employee_login_created", "employee_login_disabled", "employee_pin_reset", "employees_synced",
    "payroll_expense_posted", "journal_posted",
    "attachment_uploaded", "attachment_linked",     # Stream E: uploading/linking a proof is evidence, not a money write
})
# chat specialists Stream D adds (their audit rows are checked by the safety audit: eval/run_eval.AGENT_ACTORS)
MONEY_AGENT_ACTORS = ("tankhwa_munshi", "accounts_munshi")

# ============================================================================ who may see / ask for which tool (decision 2)
# tool -> permission (auth/principal.PERMISSIONS). Stream D builds each role's tool list from this, so a clerk's agent
# never even SEES payroll_register. Every §6 tool is here, reads included.
TOOL_PERMISSION: dict[str, str] = {
    # payroll reads: owner only, except the employee's own slips and the name list for attendance
    "list_employees": "employees:read", "find_employee": "employees:read",
    "payroll_preview": "payroll:read", "payroll_register": "payroll:read", "payslip": "payroll:read",
    "staff_advances_report": "payroll:read", "statutory_summary": "payroll:read",
    "my_payslips": "payroll:self",
    # payroll writes
    "record_attendance": "attendance:write",
    **{t: "payroll:write" for t in ("add_employee", "update_employee", "rehire_employee", "set_pay_structure",
                                    "set_commission_rule", "end_employment", "add_payroll_adjustment", "void_payroll_adjustment",
                                    "pay_salaries", "reverse_salary_payment", "give_staff_advance", "repay_staff_advance",
                                    "reverse_staff_advance", "record_statutory_payment", "add_statutory_rate", "set_payroll_settings")},
    "approve_payroll_run": "payroll:approve", "reverse_payroll_run": "payroll:approve",
    # books: owner + clerk (salary lines redacted for the clerk -- PAYROLL_REDACTED_LABEL)
    "money_accounts": "books:read", "account_book": "books:read", "reconciliation_status": "books:read", "period_status": "books:read",
    "transfer_between_accounts": "books:write", "count_cash": "books:write", "mark_cleared": "books:write", "save_reconciliation": "books:write",
    # company finance: owner only
    **{t: "finance:read" for t in ("trial_balance", "income_statement", "balance_sheet", "cash_flow", "owner_kpis",
                                   "margins_report", "fixed_assets_register", "loans_report")},
    **{t: "finance:write" for t in ("add_money_account", "set_method_route", "record_capital", "record_drawing", "record_loan",
                                    "repay_loan", "add_fixed_asset", "dispose_fixed_asset", "run_depreciation", "post_journal_entry",
                                    "reverse_journal_entry", "reverse_account_transfer", "post_cash_difference",
                                    "record_opening_balances", "close_period", "reopen_period")},
    # payment proofs (Stream E)
    "list_attachments": "attachments:read",
}
# NOTE for Stream D (decision 4): a clerk may REQUEST an owner-tier write it can see (e.g. "Rafiq ki 1000 kaat lo");
# visibility for requests is a product call -- today payroll:write is owner-only, so the clerk's agent does not see
# add_payroll_adjustment. If the owner wants clerks to raise such cards, expose the tool to the clerk's agent WITHOUT
# changing its tier (HIGH_RISK -> owner card) and without showing any pay figure in the clerk's reply.

# ============================================================================ payment proofs (Stream E)
# what a proof can be linked to: entity -> table. 'approval' links an upload to a pending card before its entry exists;
# the approved write then links the same attachment to the entry it created, in its own transaction.
ATTACHMENT_ENTITIES = {
    "approval": "approvals", "ledger": "ledger", "supplier_ledger": "supplier_ledger", "expense": "expenses",
    "purchase": "purchases", "salary_payment": "salary_payments", "staff_advance": "staff_advances",
    "statutory_payment": "statutory_payments", "account_transfer": "account_transfers", "journal_entry": "journal_entries",
}
# sniffed from the bytes (magic numbers), never trusted from the client's Content-Type; SVG/HTML are refused (stored XSS)
ATTACHMENT_CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp", "application/pdf")
ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024

# ============================================================================ the table shape (llm/answers.make_table)
# {title, lead, columns: [{key, label, align, kind (, badge)}], rows, totals, note, count, lang, text} -- EXACTLY the
# chat renderer's shape, so the office console and chat render one dict. A row may carry "_em": True (a statement's
# subtotal / total line). Money cells are rupees (floats). DEVIATION from the plan's §7 sketch: kinds are the
# renderer's (no 'int' -- use 'qty'; no 'badge' kind -- a text column with badge=True); '_style' is '_em'.
TABLE_KINDS = ("text", "money", "qty", "days", "pct", "date")
_RIGHT = frozenset(("money", "qty", "days", "pct"))
_TOTAL_WORD = {"en": "Total", "ru": "Kul", "ur": "کل"}


def col(key: str, label: str, kind: str = "text", align: str | None = None, badge: bool = False) -> dict:
    """One column. align defaults like make_table: right for money/qty/days/pct, left otherwise."""
    if kind not in TABLE_KINDS:
        raise ValueError(f"column kind must be one of {TABLE_KINDS}, got {kind!r}")
    c = {"key": key, "label": label, "align": align or ("right" if kind in _RIGHT else "left"), "kind": kind}
    if badge:
        c["badge"] = True
    return c


def table(title: str, columns: list[dict], rows: list[dict], totals: dict | None = None, note: str | None = None,
          lead: str = "", lang: str = "en", max_rows: int | None = None) -> dict:
    """A table payload in make_table's shape. Rows keep only the column keys (plus _em). `totals` is keyed like a row;
    its first column defaults to the word Total. `max_rows` cuts the rows (chat passes answers.MAX_ROWS; the console
    passes None) and says so in the note. Pure."""
    lang = lang if lang in ("en", "ru", "ur") else "en"
    keys = [c["key"] for c in columns]
    shown = rows if max_rows is None else rows[:max_rows]
    out = []
    for r in shown:
        row = {k: r.get(k) for k in keys}
        if r.get("_em"):
            row["_em"] = True
        out.append(row)
    if totals is not None and keys:
        totals = {keys[0]: _TOTAL_WORD[lang]} | totals
    if max_rows is not None and len(rows) > max_rows:
        more = f"Showing the first {max_rows} of {len(rows)}."
        note = f"{note} {more}" if note else more
    cap = lambda s: (s[:1].upper() + s[1:]) if s else s  # noqa: E731
    return {"title": cap(title), "lead": cap(lead), "columns": [dict(c) for c in columns], "rows": out, "totals": totals,
            "note": (note or "").strip() or None, "count": len(rows), "lang": lang, "text": ""}
