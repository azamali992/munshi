"""The Tankhwa munshi (payroll) and the Accounts munshi (company finance). Same factory, same rules-first stub, same
approval gate as the other seven (agents/specialists.py).

WHO SEES WHAT (owner decision 2; domain/accounts.TOOL_PERMISSION x auth/principal.PERMISSIONS)
  A role is bound exactly the tools whose permission it holds: the owner everything; the clerk the staff list (no pay),
  attendance, the money accounts and books (payroll lines aggregated), transfers and cash counts; everyone their OWN
  payslips (my_payslips reads the signed-in user's id from the session, never a name from the message). Nobody but the
  owner is bound a tool that shows pay, so a clerk's "Rafiq ki salary kitni he" can't run -- the rule still reads the
  intent and the role's prompt answers with a plain refusal (the way a real model would).
DECISION 4 (owner-approved): a clerk may REQUEST an owner-tier (HIGH_RISK) write: those tools are bound for the clerk too,
  so the request becomes an approval card that only the owner can clear (approver_for is unchanged). The card and the
  clerk's reply never show a pay figure read from the books (platform: _open_card / viewer_decision).

OFFLINE RULES (the §8.1 intents of the payroll/finance plan), each firing only when code is sure: the employee resolved
from the message (llm/resolve.py's confidence rule over the staff list), the amount read by llm/parse.amount_in, the
method said. Under the Punjab Labour Code 2026 profile (decision 1) a cash salary or advance is refused BEFORE a card:
the munshi asks for a cashless method instead of raising a card the books would refuse."""
from __future__ import annotations

import re
from datetime import date, timedelta

from langchain_core.language_models.chat_models import BaseChatModel

from munshi.agents.factory import AgentBundle, build_specialist, is_real_model
from munshi.auth.principal import PERMISSIONS
from munshi.domain import accounts as ACC
from munshi.domain.models import business_today
from munshi.domain.repository import MunshiRepository
from munshi.llm import replies as RP
from munshi.llm.answers import _method_asked, lang_of
from munshi.llm.parse import amount_in, customer_resolution, numbers_said, supplier_resolution
from munshi.llm.resolve import Candidate, Resolution
from munshi.llm.stub_model import NotUnderstood, Rule, StubToolCallingModel, contains, history, memo
from munshi.llm.text import fold, is_urdu, romanize, skeleton, words
from munshi.safety.risk import MONEY_TOOL_TIERS, RiskTier
from munshi.tools.core import MunshiTools
from munshi.tools.langchain_tools import build_tools

ROLES = ("owner", "clerk", "salesman", "driver")


# (the same small helpers agents/specialists.py uses; kept here so this module doesn't import that one -- it imports this one)
def _biz(repo) -> str:
    return repo.business_name


def _lookups(model, *tools) -> list:
    return list(tools) if is_real_model(model) else []


def _model(model, rules, fallback, prompt_fallbacks=(), fallback_fn=None):
    return model or StubToolCallingModel(rules=rules, fallback_text=fallback, prompt_fallbacks=list(prompt_fallbacks), fallback_fn=fallback_fn)


def _urdu_didnt(text: str):
    return NotUnderstood(RP.t("didnt", True)) if is_urdu(text) else None


PAYROLL_TOOLS = ("list_employees", "find_employee", "payroll_preview", "payroll_register", "payslip", "my_payslips", "staff_advances_report",
                 "statutory_summary", "record_attendance", "add_employee", "update_employee", "rehire_employee", "set_pay_structure",
                 "set_commission_rule", "end_employment", "add_payroll_adjustment", "void_payroll_adjustment", "approve_payroll_run",
                 "reverse_payroll_run", "pay_salaries", "reverse_salary_payment", "give_staff_advance", "repay_staff_advance",
                 "reverse_staff_advance", "record_statutory_payment", "add_statutory_rate", "set_payroll_settings")
ACCOUNTS_TOOLS = tuple(t for t in ACC.TOOL_PERMISSION if t not in PAYROLL_TOOLS)
# the tools whose results or cards carry someone's pay: never shown to anyone but the owner, and never stored in the
# (business-wide) chat log -- see platform.PAY_PRIVATE
PAY_PRIVATE = frozenset(t for t, p in ACC.TOOL_PERMISSION.items() if p.startswith("payroll:"))


def tools_for(role: str, names, T: dict) -> list:
    """The role's tools: those whose permission it holds; plus, for the clerk, every owner-tier write (decision 4: the
    request becomes the owner's card -- the tier, and so who approves, is unchanged)."""
    out = []
    for n in names:
        holds = role in PERMISSIONS[ACC.TOOL_PERMISSION[n]]
        requests = role == "clerk" and MONEY_TOOL_TIERS[n] == RiskTier.HIGH_RISK
        if holds or requests:
            out.append(T[n])
    return out


# ------------------------------------------------------------------ reading the message
def _rxf(pattern: str) -> re.Pattern:
    """A regex over text.fold()ed text: its Urdu-script literals folded the same way."""
    return re.compile(re.sub(r"[؀-ۿ]+", lambda m: fold(m.group(0)), pattern))


_MONTHS = {m: i for i, ms in enumerate(["jan january jnwri جنوری", "feb february فروری", "mar march مارچ", "apr april اپریل", "may مئی",
                                        "jun june جون", "jul july جولائی", "aug august اگست", "sep sept september ستمبر", "oct october اکتوبر",
                                        "nov november نومبر", "dec december دسمبر"], 1) for m in ms.split()}


def month_named(text: str) -> int:
    """The month number a message names ('September', 'اگست'), else 0."""
    for w in words(fold(text)):
        if w in _MONTHS:
            return _MONTHS[w]
    for k, v in _MONTHS.items():
        if not k.isascii() and fold(k) in fold(text):
            return v
    return 0


def _latest(month: int) -> date:
    """The first day of the latest `month` not in the future."""
    t = business_today()
    return date(t.year if month <= t.month else t.year - 1, month, 1)


def period_of(text: str) -> str:
    """'YYYY-MM' the message is about: a month it names, 'pichle mahine', else this month."""
    f = fold(text)
    m = month_named(text)
    if m:
        return _latest(m).strftime("%Y-%m")
    if _LAST_MONTH.search(f):
        return (business_today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return business_today().strftime("%Y-%m")


_LAST_MONTH = _rxf(r"\b(pichl[ea]y? mahine|last month|pichla mahina)\b|پچھلے مہینے")


def month_end(text: str) -> str:
    """The last day of the month a message names ('August band kar do'), else ''."""
    m = month_named(text)
    if not m:
        return ""
    first = _latest(m)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (nxt - timedelta(days=1)).isoformat()


def _safe(fn, default):
    try:
        return fn()
    except Exception:          # a stream not merged yet (NotImplementedError) or a missing record: the rule doesn't fire
        return default


def employees(repo) -> list[dict]:
    """The active staff (names only -- no pay field is ever needed to recognise a name)."""
    return memo(("emps",), lambda: _safe(lambda: list((repo.list_employees("active", include_pay=False) or {}).get("employees") or []), []))


def _eid(e: dict) -> str:
    return str(e.get("employee_id") or e.get("id") or "")


def _name_hit(msg_tok: str, name_tok: str) -> bool:
    """A person's name is matched strictly: the same word (Roman), or -- Urdu script -- the same consonant skeleton. The
    customers' sound-alike leeway is deliberately NOT used: 'Rafiq' and 'Shafiq' are two different people, and a card
    for the wrong one moves someone's pay."""
    if "؀" <= msg_tok[:1] <= "ۿ":
        sk = skeleton(romanize(msg_tok))
        return len(sk) >= 3 and sk == skeleton(name_tok)
    return msg_tok == name_tok


def employee_res(text: str, repo) -> Resolution:
    """The employee a message names -- the customers' confidence rule (llm/resolve.py: the head word or half the name, a clear
    lead, another person on different words reported as `other`) over strict name matching (_name_hit); a phone number too."""
    def calc():
        emps = [e for e in employees(repo) if _eid(e)]
        if not emps:
            return Resolution("none")
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 10:
            by_phone = [e for e in emps if len(re.sub(r"\D", "", str(e.get("phone") or ""))) >= 10 and re.sub(r"\D", "", str(e.get("phone"))) in digits]
            if len(by_phone) == 1:
                e = by_phone[0]
                return Resolution("ok", _eid(e), str(e.get("name")), [Candidate(_eid(e), str(e.get("name")), 1.0, via="phone")])
        toks = words(fold(text))
        eligible: list[Candidate] = []
        for e in emps:
            name = [w for w in words(fold(str(e.get("name") or ""))) if len(w) >= 3]
            if not name:
                continue
            ev = frozenset(i for i, t in enumerate(toks) for n in name if _name_hit(t, n))
            head = any(_name_hit(t, name[0]) for t in toks)
            hits = sum(1 for n in name if any(_name_hit(t, n) for t in toks))
            if ev and (head or hits / len(name) >= 0.5):
                eligible.append(Candidate(_eid(e), str(e.get("name")), hits / len(name), ev, via="head" if head else "name"))
        if not eligible:
            return Resolution("none")
        eligible.sort(key=lambda c: (-len(c.evidence), -c.score, c.id))
        top = eligible[0]
        rivals = [c for c in eligible[1:] if c.evidence >= top.evidence]
        others = [c for c in eligible[1:] if not (c.evidence & top.evidence)]
        if rivals:
            return Resolution("ambiguous", None, "", [top, *rivals][:3], others[0] if others else None, top.evidence)
        return Resolution("ok", top.id, top.name, [top], others[0] if others else None, top.evidence)
    return memo(("emp", text), calc)


def employees_named(text: str, repo) -> list[str]:
    """Every employee a message names, in order (one per segment: 'Shafiq 22 din, Kashif 18')."""
    out = []
    for seg in _segments(text):
        r = employee_res(seg, repo)
        if r.ok and r.id not in out:
            out.append(r.id)
    return out


def _segments(text: str) -> list[str]:
    return [s for s in re.split(r"[,;\n،]|\b(?:aur|and|or)\b|:", text) if s.strip()]


def _name(repo, eid: str) -> str:
    return next((str(e.get("name")) for e in employees(repo) if _eid(e) == eid), eid)


# the signed-in person's OWN pay ('meri salary slip', 'my payslip', 'meri tankhwa kitni bani')
SELF = _rxf(r"\b(meri|mera|mere|my|apni|apna|hamari)\b.{0,15}\b(salary|salaries|tankhwa\w*|tankha|pay ?slips?|payslips?|slip|slips|pay)\b"
                  r"|\bmy (salary|pay|payslips?)\b|میری\s*(تنخواہ|سیلری|سلپ)|میرا\s*(سلپ|پے)")
_PAYWORD = contains("salary", "salaries", "tankhwa", "tankhwah", "tankha", "tankhah", "tanakhwa", "payroll", "payslip", "pay slip", "slip", "تنخواہ", "سیلری",
                    "سیلیری", "سلپ")
_SHEET = contains("sheet", "register", "list", "report", "شیٹ", "رجسٹر")
_MAKE = contains("bana do", "banao", "bana den", "bana dein", "banado", "approve", "pakki", "pakka", "final", "finalize", "book kar", "book karo", "chala do",
                 "process", "run", "lagao", "laga do", "بنا دو", "بناؤ", "منظور")
_PAY = contains("de do", "dedo", "de dein", "de den", "do", "dein", "pay", "pay karo", "pay kar do", "ada", "ada karo", "bhej do", "bhejo", "transfer", "دے دو", "ادا")
_ADVANCE = contains("advance", "peshgi", "pesgi", "ایڈوانس", "پیشگی")
_GIVE = contains("dena", "dene", "de do", "dedo", "de dein", "de den", "dein", "do", "give", "dijiye", "دینا", "دے دو", "دیں")
_ASKQ = contains("kitna", "kitni", "kitne", "baqi", "baaki", "report", "list", "dikhao", "balance", "outstanding", "kya", "کتنا", "کتنی", "باقی")
_ATTEND = contains("din", "days", "day", "aya", "aaya", "aye", "aaye", "hazri", "haazri", "haziri", "attendance", "chutti", "chuttiyan", "chhutti",
                   "chhuttiyan", "chuttian", "leave", "absent", "ghair hazir", "nahi aya", "دن", "حاضری", "چھٹی", "غیر حاضر")
_LEAVE = contains("chutti", "chuttiyan", "chhutti", "chhuttiyan", "chuttian", "leave", "leaves", "چھٹی", "چھٹیاں")
_ABSENT = contains("absent", "ghair hazir", "nahi aya", "nahi aaya", "غیر حاضر")
_CUT = contains("kaat lo", "kaat do", "kaato", "kat lo", "kaat", "kat do", "cut", "deduct", "fine", "jurmana", "جرمانہ", "کاٹ")
_LOSS = contains("short", "shortage", "kam tha", "kam thi", "nuksan", "nuqsan", "loss", "chori", "toot", "کم تھا", "نقصان")
_FINE = contains("fine", "jurmana", "جرمانہ")
_BONUS = contains("bonus", "inaam", "inam", "eidi", "بونس", "انعام", "عیدی")
_LEFT = contains("kaam chor diya", "kaam chhor diya", "kaam chod diya", "naukri chor di", "naukri chhor di", "chala gaya", "chali gayi", "nikal diya",
                 "nikaal diya", "resign", "left", "quit", "fire", "استعفی", "کام چھوڑ")
_NEW = re.compile(r"\b(naya|nayi|naye|new)\b.{0,12}\b(employee|mulazim|banda|staff|worker|driver|loader|salesman|helper)\b|\bnew hire\b")
_DESIGNATIONS = ("driver", "loader", "salesman", "clerk", "munshi", "helper", "guard", "chowkidar", "accountant", "manager", "cook", "cleaner", "mechanic")
_PHONE = re.compile(r"\b0?3\d{2}[- ]?\d{7}\b")
_STATUTORY = {"eobi": contains("eobi", "ای او بی آئی"), "ss": contains("social security", "pessi", "ss", "سوشل سیکیورٹی"),
              "income_tax": lambda t: bool(re.search(r"\btax\b|ٹیکس", fold(t)))}
_COMMISSION = contains("commission", "kamishan", "کمیشن")
_STAFF_LIST = contains("staff", "employees", "employee", "mulazim", "mulazimeen", "mulazmeen", "workers", "ملازم", "ملازمین")


_FRACTIONS = (("aadhi", 0.5, ("aadhi", "adhi", "aadha", "adha", "half", "آدھی", "آدھا")), ("poori", 1.0, ("poori", "puri", "poora", "pura", "full", "ek", "پوری")),
              ("chauthai", 0.25, ("chauthai", "quarter", "چوتھائی")))


def fraction_of(text: str) -> float | None:
    """'aadhi tankhwa' -> 0.5: a bonus given as a share of each person's pay (the amount is worked out from the pay, in code)."""
    if not _PAYWORD(text):
        return None
    return next((v for _, v, ws in _FRACTIONS if contains(*ws)(text)), None)


def fraction_word(text: str) -> str:
    return next((w for w, _, ws in _FRACTIONS if contains(*ws)(text)), "poori")


def bulk_bonus(text: str) -> bool:
    """'Eid bonus sab ko aadhi tankhwa': a bonus for everyone, as a share of pay."""
    return _BONUS(text) and contains("sab", "sabko", "sab ko", "everyone", "all", "har", "tamam", "سب")(text) and fraction_of(text) is not None


def method_said(text: str) -> str:
    """The method a message says (never a default)."""
    return _method_asked(text)


def cashless_only(repo) -> bool:
    """Owner decision 1: under the Punjab Labour Code 2026 profile a salary or advance is never paid in cash."""
    prof = _safe(repo.payroll_profile, ACC.DEFAULT_PAYROLL_PROFILE)
    return bool(ACC.PROFILE_LIMITS.get(prof, ACC.PLC_LIMITS).get("cashless_only"))


PLC_CASH = {"en": ("Under the Punjab Labour Code 2026 a salary or an advance is paid by bank, JazzCash, Easypaisa or cheque, never cash -- "
                   "which one? (The owner can switch payroll to the old law in Settings.) Nothing was done."),
            "ru": ("Punjab Labour Code 2026 ke tehat tankhwa ya advance cash mein nahi, bank, JazzCash, Easypaisa ya cheque se diya jata hai -- "
                   "kis tareeqe se? (Owner Settings mein purana qanoon chala sakta hai.) Kuch nahi kiya gaya."),
            "ur": "پنجاب لیبر کوڈ 2026 کے تحت تنخواہ یا ایڈوانس نقد نہیں، بینک، جاز کیش، ایزی پیسہ یا چیک سے دیا جاتا ہے -- کس طریقے سے؟ کچھ نہیں کیا گیا۔"}
OWNER_ONLY_PAY = {"en": "Pay is the owner's only: I can't show anyone's salary, payslip or advance. For your own, ask 'meri salary slip'.",
                  "ru": "Tankhwa ki maloomat sirf owner ke liye hai: main kisi ki salary, slip ya advance nahi dikha sakta. Apni ke liye poochein 'meri salary slip'.",
                  "ur": "تنخواہ کی معلومات صرف مالک (owner) کے لیے ہیں: میں کسی کی تنخواہ، سلپ یا ایڈوانس نہیں دکھا سکتا۔ اپنی کے لیے پوچھیں 'میری سیلری سلپ'۔"}
OWNER_ONLY_FINANCE = {"en": "The balance sheet, profit and loss, cash flow and the owner's figures are the owner's only -- ask the owner.",
                      "ru": "Balance sheet, profit loss, cash flow aur owner ke figures sirf owner ke liye hain -- owner se poochein.",
                      "ur": "بیلنس شیٹ، منافع نقصان اور کیش فلو صرف مالک (owner) کے لیے ہیں -- مالک سے پوچھیں۔"}
OFFICE_ONLY_BOOKS = {"en": "The money accounts are the office's -- ask the owner or the clerk.",
                     "ru": "Paison ke accounts office ke paas hain -- owner ya clerk se poochein.",
                     "ur": "رقم کے اکاؤنٹ دفتر کے پاس ہیں -- مالک یا کلرک سے پوچھیں۔"}


def _say(table: dict, text: str) -> str:
    lang = "ur" if is_urdu(text) else lang_of(text)
    return table.get(lang, table["en"])


def _recent_tool(names: tuple[str, ...]) -> str:
    """The last of these tools this munshi's thread called (what 'aur Imran ki?' continues)."""
    from langchain_core.messages import AIMessage
    for m in reversed(history()[:-1][-8:]):
        if isinstance(m, AIMessage):
            for c in reversed(m.tool_calls or []):
                if c["name"] in names:
                    return c["name"]
    return ""


# ------------------------------------------------------------------ Tankhwa (payroll)
def build_tankhwa_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None,
                         guarded: bool | None = None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    lookups = _lookups(model, T["find_employee"])
    role_tools = {r: tools_for(r, PAYROLL_TOOLS, T) + (lookups if r in ("owner", "clerk") else []) for r in ROLES}
    prompts = {
        "owner": f"You are the Tankhwa Munshi for {B}: staff, attendance, advances, payroll, payslips, salary payments and statutory dues. "
                 "Pay money only by the method the owner states. Never show or ask for a PIN.",
        "clerk": f"You are the Tankhwa Munshi for {B}. Record attendance (days, leave, overtime) -- no rupee figure. Pay figures are the owner's only. "
                 "A clerk may ask for an owner-tier payroll action; the owner decides it.",
        "salesman": "You are the Tankhwa Munshi. Pay figures are the owner's only; own payslips only.",
        "driver": "You are the Tankhwa Munshi. Pay figures are the owner's only; own payslips only.",
    }

    def emp(t: str) -> str:
        known = {_eid(e) for e in employees(repo)}
        ids = [x for x in re.findall(r"\bEMP-[A-Z0-9]+\b", t) if x in known]       # a chained step names its employee by id
        if len(ids) == 1:
            return ids[0]
        r = employee_res(t, repo)
        return r.id or "" if r.ok and r.other is None else ""

    def amount(t: str):
        return memo(("mamt", t), lambda: amount_in(_PHONE.sub(" ", t)).amount)

    def attendance(t: str) -> list[dict] | None:
        """[{employee_id, days_worked | casual_leave | unpaid_absent}] -- every segment with a number names one employee; else None."""
        def calc():
            if not _ATTEND(t):
                return None
            rows = []
            for seg in _segments(t):
                nums = [float(x) for x in re.findall(r"(?<![\w.-])\d+(?:\.\d+)?(?![\w-])", seg)]
                if not nums:
                    continue
                r = employee_res(seg, repo)
                if not r.ok or len(nums) != 1 or nums[0] > 31:
                    return None
                key = "casual_leave" if _LEAVE(seg) or (_LEAVE(t) and not rows and len(_segments(t)) == 1) else "unpaid_absent" if _ABSENT(seg) else "days_worked"
                rows.append({"employee_id": r.id, key: int(nums[0])})
            return rows or None
        return memo(("att", t), calc)

    def is_self(t: str) -> bool:
        return bool(SELF.search(fold(t)))

    def advance_ok(t: str) -> bool:
        m = method_said(t)
        return bool(emp(t)) and amount(t) is not None and bool(m) and not (m == "cash" and cashless_only(repo)) and instalment(t) is not None

    def monthly_pay(eid: str) -> float:
        e = _safe(lambda: repo.get_employee(eid, include_pay=True), {}) or {}
        s = e.get("pay_structure") or {}
        return float(s.get("basic") or 0) or float(s.get("daily_rate") or 0) * 26 or float(e.get("basic") or 0)

    def instalment(t: str) -> float | None:
        """The monthly recovery of an advance: what the message says ('qist 1000'), else -- under the Punjab Labour Code, which
        caps it at 20% of pay -- that cap (worked out in code from the pay terms, shown on the card for the owner to check),
        else the whole advance next month. None: the pay terms aren't set, so it can't be worked out (the owner sets them first)."""
        m = re.search(r"\b(qist|kist|installment|instalment|mahana|har mahine)\s*(?:rs\.?\s*)?([\d,]+)", fold(t))
        if m:
            return float(m.group(2).replace(",", ""))
        a = amount(t) or 0
        cap_bp = ACC.PROFILE_LIMITS.get(_safe(repo.payroll_profile, ACC.DEFAULT_PAYROLL_PROFILE), {}).get("advance_instalment_cap_bp")
        if not cap_bp:
            return a
        pay = monthly_pay(emp(t))
        if not pay:
            return None
        return float(min(a, (pay * cap_bp / 10000) // 100 * 100))

    def pay_args(t: str) -> dict:
        rid = _safe(ops.latest_run_id, "") or ""
        reg = _safe(lambda: repo.payroll_register(rid), {}) if rid else {}
        named = emp(t)
        said = method_said(t)
        method_of = lambda e: said or str((_safe(lambda: repo.get_employee(e, include_pay=True), {}) or {}).get("pay_method") or "")  # noqa: E731
        if named:
            return {"run_id": rid, "payments": [{"employee_id": named, "method": method_of(named)}]}
        due = [x for x in (reg or {}).get("payslips") or [] if isinstance(x, dict) and x.get("employee_id") and float(x.get("balance_due") or 0) > 0]
        return {"run_id": rid, "payments": [{"employee_id": str(x["employee_id"]), "method": method_of(str(x["employee_id"]))} for x in due]}

    def pay_ok(t: str) -> bool:
        if not (_PAYWORD(t) and _PAY(t)) or _MAKE(t) or _BONUS(t) or _CUT(t) or _ADVANCE(t):
            return False
        a = pay_args(t)
        if not a["run_id"] or not a["payments"]:
            return False
        if not contains("sab", "sabko", "sab ko", "everyone", "all", "سب")(t) and not emp(t):
            return False
        ms = [p["method"] for p in a["payments"]]
        return all(ms) and not (cashless_only(repo) and "cash" in ms)

    def approve_args(t: str) -> dict:
        period = period_of(t)
        fp = _safe(lambda: repo.preview_payroll(period, None).get("fingerprint"), "")
        return {"period": period, "fingerprint": str(fp or "")}

    def new_employee(t: str) -> dict | None:
        if not _NEW.search(fold(t)):
            return None
        toks = re.findall(r"[A-Za-z]+", _PHONE.sub(" ", t))
        low = [w.lower() for w in toks]
        des = next((d for d in _DESIGNATIONS if d in low), "")
        stop = {"naya", "nayi", "naye", "new", "employee", "mulazim", "banda", "staff", "worker", "tankhwa", "tankhwah", "salary", "rs", "hai", "he",
                "ka", "ki", "ke", "ko", "hire", "phone", "number", "mobile", *_DESIGNATIONS}
        names = [w for w in toks if w.lower() not in stop]
        basic = amount(t)
        if not names or basic is None:
            return None
        phone = _PHONE.search(t)
        return {"name": " ".join(names[:2]).title() if len(names) > 1 and names[1][0].isupper() else names[0].title(), "designation": des,
                "phone": phone.group(0) if phone else "", "basic": basic}

    def open_shortage() -> str:
        """The cash shortage a loss recovery recovers: the ONE open cash-shortage entry of the last two months ('' when there
        is none, or several -- then the munshi asks which)."""
        def calc():
            from munshi.domain.models import business_today
            end = business_today()
            rows = _safe(lambda: repo.expenses_between((end - timedelta(days=62)).isoformat(), end.isoformat()), []) or []
            gone = {x.reversal_of for x in rows if getattr(x, "reversal_of", None)}
            open_ = [x for x in rows if x.category == "cash_shortage" and float(x.amount) > 0 and not x.reversal_of and x.expense_id not in gone
                     and not str(x.note or "").startswith(ACC.RECOVERY_NOTE_PREFIX)]
            return open_[0].expense_id if len(open_) == 1 else ""
        return memo(("shortage",), calc)

    def adj(t: str) -> dict | None:
        e, a = emp(t), amount(t)
        if e and a is None and _BONUS(t) and fraction_of(t) is not None:       # 'aadhi tankhwa bonus': a share of the pay, read from the books
            a = round(monthly_pay(e) * fraction_of(t)) or None
        if not e or a is None:
            return None
        ref = ""
        if _BONUS(t):
            code = "bonus"
        elif _CUT(t):
            code = "loss_recovery" if _LOSS(t) and not _FINE(t) else "fine" if _FINE(t) else "other_deduction"
            if code == "loss_recovery":
                ref = open_shortage()
                if not ref:
                    return None                     # which shortage? (the fallback asks)
        else:
            return None
        return {"employee_id": e, "code": code, "amount": a, "note": t[:80], "period": period_of(t)} | ({"ref": ref} if ref else {})

    set_pay = contains("set karo", "set kar do", "set", "rakho", "rakh do", "fix karo", "fix kar do", "tay karo", "mahana", "monthly", "per month")

    def statutory_kind(t: str) -> str:
        for k in ("eobi", "ss", "income_tax"):
            if _STATUTORY[k](t):
                return k
        return ""

    def payslip_ok(t: str) -> bool:
        return (_PAYWORD(t) or bool(_recent_tool(("payslip",)))) and bool(emp(t)) and not is_self(t) and not _PAY(t) and not _MAKE(t)

    rules = [
        Rule(is_self, "my_payslips", lambda t: {}),
        Rule(lambda t: new_employee(t) is not None, "add_employee", lambda t: new_employee(t)),
        Rule(lambda t: _LEFT(t) and bool(emp(t)) and not _PAYWORD(t), "end_employment", lambda t: {"employee_id": emp(t), "reason": t[:80]}),
        Rule(lambda t: attendance(t) is not None and not _PAYWORD(t) and not _ADVANCE(t), "record_attendance",
             lambda t: {"period": period_of(t), "rows": attendance(t)}),
        Rule(lambda t: _ADVANCE(t) and advance_ok(t), "give_staff_advance",
             lambda t: {"employee_id": emp(t), "amount": amount(t), "method": method_said(t), "installment": instalment(t)}),
        # 'Sajid ki tankhwa 40000 mahana set karo' (and the card chained after a new employee): the pay terms
        Rule(lambda t: _PAYWORD(t) and set_pay(t) and bool(emp(t)) and amount(t) is not None and not (_ADVANCE(t) or _BONUS(t) or _CUT(t)),
             "set_pay_structure", lambda t: {"employee_id": emp(t), "pay_basis": "monthly", "basic": amount(t)}),
        Rule(lambda t: _ADVANCE(t) and not _GIVE(t) and (_ASKQ(t) or amount(t) is None), "staff_advances_report",
             lambda t: {"employee_id": emp(t)} if emp(t) else {}),
        Rule(lambda t: adj(t) is not None, "add_payroll_adjustment", lambda t: adj(t)),
        Rule(pay_ok, "pay_salaries", pay_args),
        Rule(lambda t: _PAYWORD(t) and _MAKE(t) and not emp(t) and not _SHEET(t), "approve_payroll_run", approve_args),
        Rule(lambda t: bool(statutory_kind(t)), "statutory_summary",
             lambda t: {"kind": statutory_kind(t)} | ({"period": period_of(t)} if month_named(t) or _LAST_MONTH.search(fold(t)) else {})),
        Rule(lambda t: _PAYWORD(t) and _SHEET(t) and not emp(t), "payroll_register", lambda t: {"period": period_of(t)} if month_named(t) else {}),
        Rule(payslip_ok, "payslip", lambda t: {"employee_id": emp(t)} | ({"period": period_of(t)} if month_named(t) else {})),
        Rule(lambda t: _COMMISSION(t) and bool(emp(t)), "payroll_preview", lambda t: {"period": period_of(t), "employee_ids": [emp(t)]}),
        Rule(lambda t: _PAYWORD(t) and not emp(t) and not _PAY(t), "payroll_preview", lambda t: {"period": period_of(t)}),
        Rule(lambda t: _STAFF_LIST(t) and not emp(t), "list_employees", lambda t: {}),
    ]

    def fallback(t: str, system: str) -> str | None:
        lang = "ur" if is_urdu(t) else lang_of(t)
        roman = lang == "ru"
        r = employee_res(t, repo)
        writes = _GIVE(t) or _PAY(t) or _CUT(t) or _BONUS(t) or _LEFT(t) or _MAKE(t) or bool(_NEW.search(fold(t)))
        may_request = "may ask for an owner-tier" in system          # the clerk (decision 4): a write request is asked about below
        if "owner's only" in system and not is_self(t) and (_PAYWORD(t) or _ADVANCE(t) or _COMMISSION(t) or statutory_kind(t)) \
                and not (may_request and writes):
            return _say(OWNER_ONLY_PAY, t)
        if r.status == "ambiguous":
            names = " or ".join(c.name for c in r.candidates[:3])
            return f"Which one -- {names}? Nothing was done yet."
        if _ADVANCE(t):
            who = _name(repo, emp(t)) if emp(t) else ""
            if not who:
                return "Whose advance? Name the employee, e.g. 'Rafiq ko 5000 advance jazzcash se'. Nothing was done."
            m = method_said(t)
            if amount(t) is None:
                ask = (f"{who} ko kitna advance dena he, aur kis tareeqe se -- bank, JazzCash, Easypaisa ya cheque?" if roman else
                       f"How much should {who}'s advance be, and how is it paid -- bank, JazzCash, Easypaisa or cheque?")
                return RP.Ask(ask, "amount")
            if m == "cash" and cashless_only(repo) or not m and cashless_only(repo):
                return PLC_CASH[lang if lang in PLC_CASH else "en"]
        if _PAYWORD(t) and _PAY(t) and cashless_only(repo) and method_said(t) == "cash":
            return PLC_CASH[lang if lang in PLC_CASH else "en"]
        if _PAYWORD(t) and _PAY(t):
            reg = _safe(lambda: repo.payroll_register(None, None), None)
            if not reg:
                return "There is no approved payroll to pay yet -- say 'is mahine ki tankhwa bana do' first. Nothing was done."
            return "Whose salary, and how -- e.g. 'sab ko tankhwa de do' or 'Bilal ko bank se salary do'? Nothing was done."
        if _CUT(t) and _LOSS(t) and r.ok and amount(t) is not None:
            return ("Which cash shortage is this recovering? I can't tell one open shortage from the books -- the recovery has to name "
                    "it. Nothing was done.")
        if _ADVANCE(t) and r.ok and amount(t) is not None and instalment(t) is None:
            return f"{_name(repo, emp(t))}'s pay terms aren't set, so the monthly recovery can't be worked out -- set the pay first. Nothing was done."
        if (_CUT(t) or _BONUS(t)) and not r.ok:
            return "Whose pay, and how much? e.g. 'Rafiq ki 1000 kaat lo, cash short tha'. Nothing was done."
        if _LEFT(t) and not r.ok:
            return "Which employee has left? Name them, e.g. 'Nadeem ne kaam chor diya'. Nothing was done."
        if _NEW.search(fold(t)):
            return "Tell me the new employee's name, job, phone and monthly pay, e.g. 'naya employee Sajid driver 0301-7654321 tankhwa 40,000'."
        return _urdu_didnt(t)

    # (no prompt fallbacks: the fallback answers an intent the role isn't bound for itself, in the message's own script)
    m = _model(model, rules, "I can show the staff list, record attendance, and -- for the owner -- run the payroll, advances, payslips and salary payments.",
               (), fallback)
    return build_specialist("tankhwa", "Tankhwa Munshi", m, role_tools, prompts, checkpointer, repo=repo, guarded=guarded)


# ------------------------------------------------------------------ Accounts (company finance)
_BOOKWORDS = contains("hisaab", "hisab", "statement", "book", "ledger", "kitab", "khata", "entries", "حساب", "کتاب")
_CAME_IN = contains("aya", "aaya", "aaye", "aye", "aayi", "ayi", "mila", "mile", "received", "collect", "collected", "collection", "wasool", "wusool", "آیا", "آئے")
_BALANCE_Q = contains("kitna", "kitne", "kitni", "balance", "he", "hai", "hain", "kya", "how much", "کتنا", "کتنے", "ہے")
_BANK_IN = contains("jama", "jama karwaye", "jama karaye", "jama kiye", "dale", "daale", "dala", "deposit", "deposited", "جمع")
_BANK_OUT = contains("nikale", "nikala", "nikali", "nikalwaye", "nikalwaya", "withdraw", "withdrew", "nikaal", "نکالے", "نکالا")
_DRAWING = contains("ghar ke liye", "ghar ka kharcha", "ghar kharch", "apne liye", "khud ke liye", "personal", "drawing", "drawings", "ذاتی", "گھر کے لیے")
_CAPITAL = contains("capital", "sarmaya", "apne paise dale", "apni jeb se", "سرمایہ")
_LOAN = contains("qarz", "qarza", "karz", "karza", "loan", "قرض")
_LOAN_IN = contains("liya", "liye", "li", "mila", "mile", "aaya", "aya", "liya hai", "لیا", "ملا")
_ASSET = contains("shehzore", "shahzore", "gaari", "gari", "gaadi", "truck", "mazda", "suzuki", "bike", "motorcycle", "rickshaw", "loader", "generator",
                  "computer", "laptop", "forklift", "ups", "shehzor", "گاڑی", "ٹرک")
_BOUGHT = contains("naya", "nayi", "naye", "new", "khareeda", "khareedi", "khareedi hai", "kharida", "kharidi", "liya", "li", "le li", "خریدی", "نیا", "نئی")
_DEPRECIATION = contains("depreciation", "ghisai", "ghasai", "فرسودگی")
_CLOSE = contains("band kar do", "band karo", "close kar do", "close karo", "close", "band", "lock", "بند")
_BS = lambda t: bool(re.search(r"\bbalance ?sheet\b", fold(t))) or contains("بیلنس شیٹ")(t)  # noqa: E731
_PL = lambda t: (bool(re.search(r"\bprofit (and |& ?|n )?loss\b|\bp ?& ?l\b|\bpnl\b|\bincome statement\b|\b(munafa|nafa) (nuqsan|nuksan)\b", fold(t)))  # noqa: E731
                 or contains("منافع نقصان", "نفع نقصان")(t))
_CF = lambda t: (bool(re.search(r"\bcash ?flow\b|\b(paisa|paise|paisay|pesa) kahan (gaya|gaye|gya|ja raha)\b", fold(t)))  # noqa: E731
                 or contains("پیسہ کہاں", "پیسے کہاں")(t))
_KPI = lambda t: bool(re.search(r"\b(dso|dpo|kpi|kpis|stock days|collection rate)\b", fold(t)))  # noqa: E731
_ROUTE_MARGIN = lambda t: bool(re.search(r"\b(route|routes)\b|روٹ", fold(t))) and bool(re.search(r"\b(munafa|margin|profit|nafa)\b|منافع", fold(t)))  # noqa: E731
_TB = contains("trial balance")
_COUNT = contains("gin liya", "gin liye", "gina", "gin", "ginti", "count", "counted", "گن")
_CASHWORD = contains("cash", "galla", "galle", "gala", "naqd", "naqad", "کیش", "گلہ", "نقد")


def accounts_of(repo) -> list[dict]:
    return memo(("accts",), lambda: _safe(lambda: [a for a in (repo.list_money_accounts() or {}).get("accounts") or [] if isinstance(a, dict)], []))


def _aid(a: dict) -> str:
    return str(a.get("account_id") or a.get("id") or "")


def _aname(a: dict) -> str:
    return str(a.get("name") or a.get("account") or _aid(a))


def account_named(text: str, repo, kinds: tuple = ()) -> list[str]:
    """The money accounts a message names, in the order named: by provider / name word ('HBL', 'Meezan', 'jazzcash'), 'galla' /
    'cash' for cash, and 'bank' for the ONLY bank account (with two banks, 'bank' alone names none)."""
    f = fold(text)
    hits: list[tuple[int, str]] = []
    accts = accounts_of(repo)
    for a in accts:
        if kinds and a.get("kind") not in kinds:
            continue
        toks = {fold(str(a.get("provider") or ""))} | {w for w in words(fold(_aname(a))) if len(w) >= 3 and w not in ("current", "account", "bank", "wallet", "hand", "cash")}
        for w in (x for x in toks if x):
            m = re.search(rf"(?<!\w){re.escape(w)}(?!\w)", f.replace("jazz cash", "jazzcash").replace("easy paisa", "easypaisa"))
            if m:
                hits.append((m.start(), _aid(a)))
                break
    for word, kind in ((r"\b(galla|galle|gala|cash|naqd|naqad)\b|" + "|".join(map(fold, ("گلہ", "نقد", "کیش"))), "cash"), (r"\bbank\b|" + fold("بینک"), "bank")):
        m = re.search(word, f)
        if m:
            of_kind = [a for a in accts if a.get("kind") == kind]
            if kind == "cash":
                of_kind = [a for a in of_kind if _aid(a) == ACC.CASH_ACCOUNT_ID] or of_kind
            if len(of_kind) == 1 and all(_aid(of_kind[0]) != x for _, x in hits):
                hits.append((m.start(), _aid(of_kind[0])))
    return [x for _, x in sorted(hits)]


def build_accounts_munshi(ops: MunshiTools, repo: MunshiRepository, model: BaseChatModel | None = None, checkpointer=None,
                          guarded: bool | None = None) -> AgentBundle:
    T = build_tools(ops); B = _biz(repo)
    role_tools = {r: tools_for(r, ACCOUNTS_TOOLS, T) for r in ROLES}
    prompts = {
        "owner": f"You are the Accounts Munshi for {B}: money accounts, transfers between them, cash counts, capital, drawings, loans, assets, "
                 "the P&L, balance sheet, cash flow, KPIs and closing a month. You only use amounts the owner typed.",
        "clerk": f"You are the Accounts Munshi for {B}. Show the money accounts and their books, record transfers and cash counts. "
                 "Company finance reports are the owner's only; a clerk may ask for an owner-tier entry, and the owner decides it.",
        "salesman": "You are the Accounts Munshi. Money accounts are for the office.",
        "driver": "You are the Accounts Munshi. Money accounts are for the office.",
    }

    def nobody(t: str) -> bool:
        return customer_resolution(t, repo).status == "none" and supplier_resolution(t, repo).status == "none"

    def amount(t: str):
        return memo(("aamt", t), lambda: amount_in(t).amount)

    def the_bank(t: str) -> str:
        named = [x for x in account_named(t, repo) if x != ACC.CASH_ACCOUNT_ID]
        return named[0] if len(named) == 1 else ""

    def transfer(t: str) -> dict | None:
        if amount(t) is None or not nobody(t) or _DRAWING(t) or re.search(r"\bne\b", fold(t)):
            return None
        named = account_named(t, repo)
        if len(named) == 2:                                    # 'HBL se jazzcash mein 5000': the one followed by 'se' is the source
            f = fold(t)
            src = named[0] if re.search(r"\b(se|say|sy)\b", f) else named[0]
            return {"from_account": src, "to_account": named[1], "amount": amount(t)}
        bank = the_bank(t)
        if not bank:
            return None
        if _BANK_OUT(t):
            return {"from_account": bank, "to_account": ACC.CASH_ACCOUNT_ID, "amount": amount(t)}
        if _BANK_IN(t):
            return {"from_account": ACC.CASH_ACCOUNT_ID, "to_account": bank, "amount": amount(t)}
        return None

    def lender(t: str) -> str:
        m = re.search(r"^(.*?)\s+(?:se|sy|say|from)\b", t.strip(), re.I)
        who = re.sub(r"(?i)\b(maine|mainay|humne|hum ne|ne|ko|ka|ki|ke)\b", " ", m.group(1)) if m else ""
        who = re.sub(r"[\d,.]+|\b(lakh|lac|hazar|hazaar|crore|rs|rupay|rupees)\b", " ", who, flags=re.I)
        return re.sub(r"\s+", " ", who).strip()

    def asset(t: str) -> dict | None:
        if not (_ASSET(t) and _BOUGHT(t)) or amount(t) is None:
            return None
        name = next((w for w in re.findall(r"[A-Za-z]+", t) if _ASSET(w)), "asset")
        return {"name": name, "cost": amount(t)}

    def book_args(t: str) -> dict:
        named = account_named(t, repo)
        return {"account_id": named[0]} if named else {}

    rules = [
        Rule(_BS, "balance_sheet", lambda t: {}),
        Rule(_PL, "income_statement", lambda t: {"start": _month_start(t), "end": _month_to(t)} if (month_named(t) or _this_month(t)) else {}),
        Rule(_CF, "cash_flow", lambda t: {"start": _month_start(t), "end": _month_to(t)} if (month_named(t) or _this_month(t)) else {}),
        Rule(_TB, "trial_balance", lambda t: {}),
        Rule(_KPI, "owner_kpis", lambda t: {}),
        Rule(_ROUTE_MARGIN, "margins_report", lambda t: {"by": "route"}),
        Rule(lambda t: _DRAWING(t) and amount(t) is not None, "record_drawing",
             lambda t: {"amount": amount(t), "method": method_said(t) or "cash", "note": t[:80]}),
        Rule(lambda t: _LOAN(t) and _LOAN_IN(t) and amount(t) is not None and bool(lender(t)), "record_loan",
             lambda t: {"lender": lender(t), "amount": amount(t), "method": method_said(t) or "cash"}),
        Rule(lambda t: _CAPITAL(t) and amount(t) is not None, "record_capital", lambda t: {"amount": amount(t), "method": method_said(t) or "cash"}),
        Rule(lambda t: asset(t) is not None and nobody(t), "add_fixed_asset", lambda t: asset(t)),
        Rule(lambda t: _DEPRECIATION(t) and bool(re.search(r"\b(laga|lagao|laga do|chala|chalao|run|book|kar do|karo)\b", fold(t))), "run_depreciation",
             lambda t: {"through_period": period_of(t)}),
        Rule(lambda t: _CLOSE(t) and bool(month_end(t)) and not _PL(t), "close_period", lambda t: {"through_date": month_end(t)}),
        Rule(lambda t: _COUNT(t) and _CASHWORD(t) and amount(t) is not None, "count_cash",
             lambda t: {"account_id": ACC.CASH_ACCOUNT_ID, "counted": amount(t)}),
        Rule(lambda t: transfer(t) is not None, "transfer_between_accounts", lambda t: transfer(t)),
        Rule(lambda t: _BOOKWORDS(t) and bool(account_named(t, repo)) and nobody(t), "account_book", book_args),
        Rule(lambda t: bool(account_named(t, repo) or _CASHWORD(t)) and _BALANCE_Q(t) and not _CAME_IN(t) and amount(t) is None and nobody(t),
             "money_accounts", lambda t: {}),
        Rule(lambda t: contains("loans", "qarz kitna", "loan kitna", "qarze")(t), "loans_report", lambda t: {}),
        Rule(lambda t: contains("assets", "asasay", "fixed assets")(t), "fixed_assets_register", lambda t: {}),
    ]

    def fallback(t: str, system: str) -> str | None:
        finance = _BS(t) or _PL(t) or _CF(t) or _KPI(t) or _ROUTE_MARGIN(t) or _TB(t)
        if "owner's only" in system and finance:
            return _say(OWNER_ONLY_FINANCE, t)
        if "for the office" in system:
            return _say(OFFICE_ONLY_BOOKS, t)
        if _CLOSE(t) and not month_end(t):
            return RP.Ask("Which month should be closed? e.g. 'August band kar do'. Closing locks it: nothing in it can change after. Nothing was done yet.", "date")
        if (_BANK_IN(t) or _BANK_OUT(t)) and amount(t) is not None and not the_bank(t):
            names = ", ".join(_aname(a) for a in accounts_of(repo) if a.get("kind") != "cash")
            return f"Which account -- {names or 'which bank'}? Nothing was done yet."
        if (_LOAN(t) or _DRAWING(t) or _CAPITAL(t)) and amount(t) is None:
            return RP.t("how_much", is_urdu(t))
        return _urdu_didnt(t)

    m = _model(model, rules, "I can show the money accounts and their books, move money between them, count the cash, and -- for the owner -- the P&L, "
                             "balance sheet, cash flow, loans, assets and closing a month.",
               (), fallback)
    return build_specialist("accounts", "Accounts Munshi", m, role_tools, prompts, checkpointer, repo=repo, guarded=guarded)


_THIS_MONTH = _rxf(r"\b(is mahine|this month|is maah|mahine ka|month)\b|اس مہینے")


def _this_month(t: str) -> bool:
    return bool(_THIS_MONTH.search(fold(t)))


def _month_start(t: str) -> str:
    m = month_named(t)
    return (_latest(m) if m else business_today().replace(day=1)).isoformat()


def _month_to(t: str) -> str:
    m = month_named(t)
    if not m:
        return business_today().isoformat()
    end = month_end(t)
    return min(end, business_today().isoformat())


def employee_ids_said(text: str, repo) -> set[str]:
    """Every employee the message names anywhere (the model engine's check: an employee on a card must be one the user named)."""
    ids = set(employees_named(text, repo))
    r = employee_res(text, repo)
    if r.ok:
        ids.add(r.id or "")
    return ids


def numbers_in(text: str, repo) -> set[float]:
    return numbers_said(text, repo)
