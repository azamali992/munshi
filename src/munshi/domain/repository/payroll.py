"""Employees (+ optional app logins), pay structures, commission, attendance, adjustments, staff advances, the payroll
engine's runs / payslips / salary payments, statutory summaries and rates. OWNED BY STREAM A (plan §4.2, §4.3, §5,
§7, §9 Stream A). Rupees at the edge, integer paisa inside; every write takes actor + approved_by, runs in one
immediate_tx and writes exactly ONE audit row named accounts.AUDIT_ACTION[tool] (extra rows use SECONDARY_ACTIONS).

THE OWNER'S DECISIONS implemented here (full text in domain/accounts.py):
  1. plc_2026 by default; accounts.PROFILE_LIMITS[profile] are ENFORCED as refusals (StateError naming the switch):
     advance cap 3 x monthly minimum wage, one open advance, instalment <= 20% of pay, fine <= 3% (legacy 3.125%),
     salary / advance / repayment only by accounts.CASHLESS_METHODS -- unless the owner has marked that employee
     CASH ALLOWED (employees.cash_allowed: an owner-only, audited, per-employee exemption with a reason; every payment
     that relies on it is flagged cash_exemption=1 and the payslip says "paid in cash with the owner's exemption").
  2. Owner-only pay: every method returning a rupee is for payroll:read callers (the web layer enforces it);
     list_employees / get_employee / find_employee / attendance return NO pay field unless include_pay; my_payslips
     resolves the employee from the caller's user_id only. Audit payloads carry ids and codes, never a rupee figure
     (the audit list is readable by clerks) and never a PIN or a full CNIC.
  3. Accrual at approve_payroll: _payroll_postings() books Dr 6100-6200 / Cr 2100 (+2110/2120/2130/1150/2140,
     6000:cash_shortage for loss recoveries, 8000 for other deductions) and the PAYROLL_METHOD expense rows are
     INSERTed directly (never record_expense).
Statutory figures come from the dated statutory_rates table; a seeded figure the plan could not verify (grade U from
the seed, or 'unknown') is REFUSED until the owner adds a row of their own.

Before V9 is applied to a file (it is registered by the lead after B's V8) every payroll method raises
NotImplementedError except the four working defaults, which answer as if there were no payroll.
Consumes from B: self.resolve_account(method, account_id, on_date), self.assert_period_open(on_date)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta

from munshi.domain import payroll_rules as R
from munshi.domain.accounts import (
    ALLOWANCES,
    BONUS,
    BOUNDARY_TEXT,
    CASH_ACCOUNT_ID,
    COMMISSION,
    DEFAULT_PAYROLL_PROFILE,
    EMPLOYER_CONTRIBUTIONS,
    EOBI_PAYABLE,
    INCOME_TAX_WITHHELD,
    MONEY,
    MONEY_METHODS,
    OTHER_INCOME,
    OTHER_PAYABLES,
    PAYROLL_METHOD,
    PAYROLL_PROFILES,
    PAYROLL_PROVINCES,
    PAYROLL_SETTINGS_DEFAULTS,
    PROFILE_LIMITS,
    RECOVERY_NOTE_PREFIX,
    SALARIES,
    SALARIES_PAYABLE,
    SOCIAL_SECURITY_PAYABLE,
    STAFF_ADVANCES,
    STAFF_WELFARE_FUND,
    UNASSIGNED_ACCOUNT_ID,
    WAGES,
    Posting,
    col,
    expense_code,
    signed,
    table,
)
from munshi.domain.models import mul_div, now_iso, sql_business_date, to_paisa, to_rupees, today_iso
from munshi.domain.repository.base import NotFoundError, RepositoryBase, StateError, new_id
from munshi.domain.repository.guarded import immediate_tx
from munshi.domain.repository.numbering import next_doc_no

ROLE_HINTS = ("clerk", "salesman", "driver", "helper", "loader", "godown", "guard", "accountant", "other")
ADJUSTMENT_CODES = tuple(R.ADJUSTMENT_CODE)
STATUTORY_KINDS = ("eobi", "pessi", "sessi", "kpessi", "income_tax")
SS_KIND = {"punjab": "pessi", "sindh": "sessi", "kp": "kpessi"}
STAT_CODE = {"eobi": EOBI_PAYABLE, "pessi": SOCIAL_SECURITY_PAYABLE, "sessi": SOCIAL_SECURITY_PAYABLE,
             "kpessi": SOCIAL_SECURITY_PAYABLE, "income_tax": INCOME_TAX_WITHHELD}
STAT_LINES = {"eobi": ("EOBI_EE", "EOBI_ER"), "pessi": ("SS_EE", "SS_ER"), "sessi": ("SS_EE", "SS_ER"),
              "kpessi": ("SS_EE", "SS_ER"), "income_tax": ("TAX",)}
RATE_JURISDICTIONS = ("pk",) + PAYROLL_PROVINCES + PAYROLL_PROFILES
# extra payroll settings Stream A reads with a default of its own (not in the frozen PAYROLL_SETTINGS_DEFAULTS)
EXTRA_SETTINGS = {"ss_above_ceiling": "exclude"}
_SETTING_VALUES = {"payroll_profile": PAYROLL_PROFILES, "payroll_province": PAYROLL_PROVINCES, "eobi_registered": ("0", "1"),
                   "ss_registered": ("0", "1"), "payroll_tax_round_rupee": ("0", "1"), "payroll_enabled": ("0", "1"),
                   "ss_above_ceiling": ("exclude", "cap")}
CASH_EXEMPTION_NOTE = "Paid in cash with the owner's exemption"
# staff-cost category of each earning line; BASIC-like lines follow the pay basis
_BASIC_LIKE = ("BASIC", "OT", "ARREARS", "OTHER_EARN", "ABSENCE")
_CATEGORY = {"ALW": "staff_allowances", "COMM": "staff_commission", "BONUS": "staff_bonus",
             "EOBI_ER": "employer_contributions", "SS_ER": "employer_contributions"}
_CATEGORY_CODE = {"staff_salaries": SALARIES, "staff_wages": WAGES, "staff_allowances": ALLOWANCES,
                  "staff_commission": COMMISSION, "staff_bonus": BONUS, "employer_contributions": EMPLOYER_CONTRIBUTIONS}
_CREDIT = {"TAX": INCOME_TAX_WITHHELD, "EOBI_EE": EOBI_PAYABLE, "EOBI_ER": EOBI_PAYABLE, "SS_EE": SOCIAL_SECURITY_PAYABLE,
           "SS_ER": SOCIAL_SECURITY_PAYABLE, "ADV": STAFF_ADVANCES, "FINE": STAFF_WELFARE_FUND,
           "LOSS": expense_code("cash_shortage"), "OTHER_DED": OTHER_INCOME, "IN_LIEU": OTHER_PAYABLES}
_PUBLIC_FIELDS = ("employee_id", "emp_no", "name", "designation", "role_hint", "status", "joined_on", "left_on", "phone")
_TEXT_FIELDS = {"father_name": 60, "designation": 40, "eobi_no": 20, "ss_no": 20, "payee_ref": 40, "default_vehicle_id": 20}
_STANDING_SQL = "NOT EXISTS (SELECT 1 FROM payroll_runs x WHERE x.reversal_of = {r}.run_id)"


def _p(amount) -> int:
    return to_paisa(amount)


def _r(paisa) -> float:
    return to_rupees(int(paisa or 0))


def _day(value, what: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a date YYYY-MM-DD, got {value!r}") from None


def _half_days(value, what: str) -> int:
    v = float(value or 0)
    if v < 0 or abs(v * 2 - round(v * 2)) > 1e-9:
        raise ValueError(f"{what} must be a whole or half day, got {value!r}")
    return int(round(v * 2))


def _phone(phone: str) -> str:
    """Pakistani mobile -> 03XXXXXXXXX (the registry's rule; the domain layer does not import auth)."""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if digits.startswith("92") and len(digits) == 12:
        digits = "0" + digits[2:]
    if len(digits) == 10 and digits.startswith("3"):
        digits = "0" + digits
    if not (len(digits) == 11 and digits.startswith("03")):
        raise ValueError("enter a mobile number like 0300-1234567")
    return digits


def _approver(approved_by: str | None) -> str:
    if not approved_by or not str(approved_by).strip():
        raise PermissionError("this needs the owner's approval")
    return str(approved_by)


def _month_name(period: str) -> str:
    return date(int(period[:4]), int(period[5:7]), 1).strftime("%B %Y")


def _leg(on: str, code: str, amount: int, debit: bool, **kw) -> Posting | None:
    """One leg of a multi-leg entry; a negative amount (a reversal run) flips the side."""
    if amount == 0:
        return None
    if amount < 0:
        amount, debit = -amount, not debit
    return Posting(on, code, debit_paisa=amount if debit else 0, credit_paisa=0 if debit else amount, **kw)


class PayrollMixin(RepositoryBase):
    # ================================================================== working defaults (safe before V9)
    def payroll_setting(self, key: str) -> str:
        if key in EXTRA_SETTINGS:
            return self.setting(key, EXTRA_SETTINGS[key])
        return self.setting(key, PAYROLL_SETTINGS_DEFAULTS[key])

    def payroll_profile(self) -> str:
        p = self.payroll_setting("payroll_profile")
        return p if p in PAYROLL_PROFILES else DEFAULT_PAYROLL_PROFILE

    def payroll_ready(self) -> bool:
        """True once V9's tables exist in this business's file."""
        return self._one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='payroll_runs'") is not None

    def _pr_need(self) -> None:
        if not self.payroll_ready():
            raise NotImplementedError("payroll is not enabled on this business yet (schema V9 is not applied)")

    def _pr_limits(self) -> dict:
        return dict(PROFILE_LIMITS[self.payroll_profile()])

    # ================================================================== statutory rates
    def _pr_rate_row(self, key: str, jurisdiction: str, on: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM statutory_rates WHERE key=? AND jurisdiction=? AND effective_from<=? "
                         "ORDER BY effective_from DESC, rate_id DESC LIMIT 1", (key, jurisdiction, on))

    @staticmethod
    def _pr_needs_verify(row) -> bool:
        return row is not None and (row["value"] == "unknown" or (row["set_by"] == "seed" and row["grade"] == "U"))

    @staticmethod
    def _pr_rate_out(row, lang: str = "en") -> dict:
        d = dict(row)
        d["needs_verify"] = PayrollMixin._pr_needs_verify(row)
        d["boundary"] = BOUNDARY_TEXT.get(lang, BOUNDARY_TEXT["en"]).format(verified_on=row["verified_on"], source=row["source"])
        return d

    def statutory_rate(self, key: str, jurisdiction: str = "pk", on: str | None = None) -> dict:
        self._pr_need()
        row = self._pr_rate_row(key, jurisdiction, on or today_iso())
        if row is None:
            raise NotFoundError(f"no statutory rate {key}/{jurisdiction} in effect on {on or today_iso()}")
        return self._pr_rate_out(row)

    def statutory_rates_list(self, on: str | None = None) -> dict:
        """Every rate in effect on a date (the latest row per key and jurisdiction), with grade and 'last verified'."""
        self._pr_need()
        on = on or today_iso()
        rows = self._all("SELECT s.* FROM statutory_rates s WHERE s.effective_from<=? AND s.rate_id = (SELECT t.rate_id FROM statutory_rates t "
                         "WHERE t.key=s.key AND t.jurisdiction=s.jurisdiction AND t.effective_from<=? ORDER BY t.effective_from DESC, t.rate_id DESC LIMIT 1) "
                         "ORDER BY s.key, s.jurisdiction", (on, on))
        out = [self._pr_rate_out(r) for r in rows]
        oldest = min((r["verified_on"] for r in out), default=on)
        tbl = table("Statutory rates", [col("key", "Rate"), col("jurisdiction", "Where"), col("value", "Value"),
                                        col("effective_from", "From", "date"), col("grade", "Grade", badge=True),
                                        col("verified_on", "Last checked", "date"), col("status", "Status", badge=True), col("source", "Source")],
                    [r | {"value": r["value"] if len(r["value"]) < 40 else "(tax table)", "status": "VERIFY" if r["needs_verify"] else "set"} for r in out],
                    note=BOUNDARY_TEXT["en"].format(verified_on=oldest, source="the sources listed"))
        return {"rates": out, "table": tbl, "as_of": on}

    def add_statutory_rate(self, key: str, jurisdiction: str, value: str, effective_from: str, source: str, source_url: str,
                           verified_on: str, grade: str, note: str, actor: str, approved_by: str | None) -> dict:
        self._pr_need()
        from munshi.domain.migrations_payroll import SEEDS
        approved_by = _approver(approved_by)
        known = {k for k, *_ in SEEDS}
        if key not in known:
            raise ValueError(f"unknown statutory rate {key!r}; one of {', '.join(sorted(known))}")
        if jurisdiction not in RATE_JURISDICTIONS:
            raise ValueError(f"jurisdiction must be one of {', '.join(RATE_JURISDICTIONS)}")
        effective_from, verified_on = _day(effective_from, "effective_from"), _day(verified_on or today_iso(), "verified_on")
        if grade not in ("A", "B", "C", "D", "U"):
            raise ValueError("grade must be A, B, C, D or U")
        if len((source or "").strip()) < 3:
            raise ValueError("say where the figure comes from (source)")
        value = str(value).strip()
        if key == "salary_tax_slabs":
            slabs = json.loads(value) if isinstance(value, str) else value
            if not isinstance(slabs, dict) or not isinstance(slabs.get("tax_year"), int):
                raise ValueError("tax slabs need a tax_year and bands")
            R.validate_slabs(slabs)
            value = json.dumps(slabs)
        elif not value.isdigit():
            raise ValueError(f"{key} is a whole number (paisa, basis points or a count), got {value!r}")
        with immediate_tx(self) as c:
            c.execute("INSERT INTO statutory_rates (key, jurisdiction, value, effective_from, source, source_url, verified_on, grade, note, set_by, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (key, jurisdiction, value, effective_from, source.strip()[:200], (source_url or "")[:300],
                                                         verified_on, grade, (note or "")[:300], self._current_user() or actor, now_iso()))
            rid = c.lastrowid
            self.audit(actor, "add_statutory_rate", "statutory_rate", str(rid), {"key": key, "jurisdiction": jurisdiction,
                                                                              "effective_from": effective_from, "grade": grade}, approved_by)
        return self._pr_rate_out(self._one("SELECT * FROM statutory_rates WHERE rate_id=?", (rid,)))

    # ================================================================== settings
    def payroll_settings(self) -> dict:
        return {k: self.payroll_setting(k) for k in (*PAYROLL_SETTINGS_DEFAULTS, *EXTRA_SETTINGS)}

    def set_payroll_settings(self, changes: dict, actor: str, approved_by: str | None) -> dict:
        approved_by = _approver(approved_by)
        allowed = set(PAYROLL_SETTINGS_DEFAULTS) | set(EXTRA_SETTINGS)
        clean = {}
        for k, v in (changes or {}).items():
            if k not in allowed:
                raise ValueError(f"unknown payroll setting {k!r}")
            v = str(int(v) if isinstance(v, bool) else v).strip()
            if k in _SETTING_VALUES and v not in _SETTING_VALUES[k]:
                raise ValueError(f"{k} must be one of {', '.join(_SETTING_VALUES[k])}")
            if k == "payroll_pay_day" and not (v.isdigit() and 1 <= int(v) <= 28):
                raise ValueError("payroll_pay_day is a day of the month, 1-28")
            if k == "finance_start_date" and v:
                v = _day(v, "finance_start_date")
            clean[k] = v
        if not clean:
            raise ValueError("nothing to change")
        with immediate_tx(self) as c:
            for k, v in clean.items():
                c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, v))
            self.audit(actor, "set_payroll_settings", "settings", "payroll", {"changes": clean}, approved_by)
        return self.payroll_settings()

    # ================================================================== employees
    def _pr_emp(self, employee_id: str) -> sqlite3.Row:
        self._pr_need()
        r = self._one("SELECT * FROM employees WHERE employee_id=? OR emp_no=?", (employee_id, employee_id))
        if not r:
            raise NotFoundError(f"no such employee: {employee_id}")
        return r

    def _pr_emp_out(self, r, include_pay: bool) -> dict:
        d = {k: r[k] for k in _PUBLIC_FIELDS}
        d["aliases"] = json.loads(r["aliases"] or "[]")
        d["has_login"] = r["user_id"] is not None
        if not include_pay:
            return d
        d.update({k: r[k] for k in ("father_name", "cnic", "date_of_birth", "province", "eobi_no", "ss_no", "tax_mode",
                                    "opening_tax_year", "pay_method", "pay_account_id", "payee_ref", "default_vehicle_id",
                                    "user_id", "cash_allowed_note", "cash_allowed_by", "cash_allowed_at")})
        d.update(eobi_covered=bool(r["eobi_covered"]), ss_covered=bool(r["ss_covered"]), cash_allowed=bool(r["cash_allowed"]),
                 route_ids=json.loads(r["route_ids"] or "[]"), opening_ytd_taxable=_r(r["opening_ytd_taxable_paisa"]),
                 opening_ytd_tax=_r(r["opening_ytd_tax_paisa"]))
        s = self._pr_structure_at(r["employee_id"], "9999-12-31")
        d["pay_structure"] = self._pr_structure_out(s) if s else None
        d["commission_rules"] = [self._pr_rule_out(x) for x in self._all(
            "SELECT * FROM commission_rules WHERE employee_id=? AND effective_to IS NULL ORDER BY effective_from", (r["employee_id"],))]
        d["advance_outstanding"] = _r(sum(a["outstanding"] for a in self._pr_open_advances(r["employee_id"])))
        return d

    def list_employees(self, status: str = "active", include_pay: bool = False) -> dict:
        self._pr_need()
        if status not in ("active", "left", "all"):
            raise ValueError("status must be active, left or all")
        rows = self._all("SELECT * FROM employees" + ("" if status == "all" else " WHERE status=?") + " ORDER BY emp_no",
                         () if status == "all" else (status,))
        emps = [self._pr_emp_out(r, include_pay) for r in rows]
        cols = [col("emp_no", "No."), col("name", "Name"), col("designation", "Designation"), col("status", "Status", badge=True),
                col("login", "App login", badge=True)]
        if include_pay:
            cols += [col("basis", "Pay basis"), col("pay", "Basic / daily", "money"), col("method", "Paid by")]
        trows = [{"emp_no": e["emp_no"], "name": e["name"], "designation": e["designation"] or e["role_hint"], "status": e["status"],
                  "login": "yes" if e["has_login"] else "no",
                  **({"basis": (e["pay_structure"] or {}).get("pay_basis") or "not set",
                      "pay": (e["pay_structure"] or {}).get("basic") or (e["pay_structure"] or {}).get("daily_rate"),
                      "method": e["pay_method"] + (" (cash allowed)" if e["cash_allowed"] else "")} if include_pay else {})}
                 for e in emps]
        return {"employees": emps, "table": table("Employees", cols, trows), "count": len(emps)}

    def get_employee(self, employee_id: str, include_pay: bool = False) -> dict:
        return self._pr_emp_out(self._pr_emp(employee_id), include_pay)

    def employee_by_user(self, user_id: str) -> dict | None:
        if not self.payroll_ready() or not user_id:
            return None
        r = self._one("SELECT * FROM employees WHERE user_id=?", (user_id,))
        return self._pr_emp_out(r, False) if r else None

    def find_employee(self, text: str) -> dict:
        self._pr_need()
        q = (text or "").strip().lower()
        if not q:
            return {"match": None, "candidates": []}
        exact, partial = [], []
        for r in self._all("SELECT * FROM employees ORDER BY status, emp_no"):
            names = [r["name"].lower(), *[a.lower() for a in json.loads(r["aliases"] or "[]")]]
            keys = {r["employee_id"].lower(), r["emp_no"].lower(), r["phone"], *names}
            if q in keys or (q.replace("-", "").replace(" ", "") == r["phone"] and r["phone"]):
                exact.append(r)
            elif any(q in n or n.split()[0] == q.split()[0] for n in names if n):
                partial.append(r)
        pick = exact or partial
        out = [self._pr_emp_out(r, False) for r in pick]
        return {"match": out[0] if len(out) == 1 else None, "candidates": out}

    def _pr_next_emp_no(self) -> str:
        n = 0
        for r in self._all("SELECT emp_no FROM employees WHERE emp_no LIKE 'E-%'"):
            tail = r["emp_no"][2:]
            if tail.isdigit():
                n = max(n, int(tail))
        return f"E-{n + 1:03d}"

    def _pr_clean(self, data: dict, partial: bool) -> dict:
        """Validate employee fields -> column values. Unknown keys are refused (a typo must not be silently dropped)."""
        out: dict = {}
        allowed = {"name", "emp_no", "aliases", "father_name", "cnic", "phone", "date_of_birth", "designation", "role_hint",
                   "joined_on", "province", "eobi_covered", "eobi_no", "ss_covered", "ss_no", "tax_mode", "opening_tax_year",
                   "opening_ytd_taxable", "opening_ytd_tax", "pay_method", "pay_account_id", "payee_ref", "default_vehicle_id",
                   "route_ids", "cash_allowed", "cash_allowed_note", "app_login"}
        bad = set(data) - allowed
        if bad:
            raise ValueError(f"unknown employee field(s): {', '.join(sorted(bad))}")
        if "name" in data or not partial:
            name = str(data.get("name") or "").strip()
            if not 2 <= len(name) <= 60:
                raise ValueError("name must be 2-60 characters")
            out["name"] = name
        if data.get("emp_no"):
            emp_no = str(data["emp_no"]).strip().upper()
            if not 1 <= len(emp_no) <= 12:
                raise ValueError("employee number must be 1-12 characters")
            out["emp_no"] = emp_no
        if "aliases" in data:
            al = data["aliases"] or []
            if not isinstance(al, list) or any(not isinstance(a, str) or not 1 <= len(a.strip()) <= 40 for a in al) or len(al) > 10:
                raise ValueError("aliases are up to 10 short names")
            out["aliases"] = json.dumps([a.strip() for a in al])
        for k, n in _TEXT_FIELDS.items():
            if k in data:
                v = str(data[k] or "").strip()
                if len(v) > n:
                    raise ValueError(f"{k} is at most {n} characters")
                out[k] = v or (None if k == "default_vehicle_id" else "")
        if "cnic" in data:
            cn = "".join(ch for ch in str(data["cnic"] or "") if ch.isdigit())
            if cn and len(cn) != 13:
                raise ValueError("CNIC must be 13 digits")
            out["cnic"] = cn
        if "phone" in data:
            out["phone"] = _phone(data["phone"]) if data["phone"] else ""
        if "date_of_birth" in data:
            out["date_of_birth"] = _day(data["date_of_birth"], "date_of_birth") if data["date_of_birth"] else None
        if "role_hint" in data:
            if data["role_hint"] not in ROLE_HINTS:
                raise ValueError(f"role must be one of {', '.join(ROLE_HINTS)}")
            out["role_hint"] = data["role_hint"]
        if "joined_on" in data or not partial:
            out["joined_on"] = _day(data.get("joined_on") or today_iso(), "joined_on")
        if "province" in data or not partial:
            pv = data.get("province") or self.payroll_setting("payroll_province")
            if pv not in PAYROLL_PROVINCES:
                raise ValueError(f"province must be one of {', '.join(PAYROLL_PROVINCES)}")
            out["province"] = pv
        for k in ("eobi_covered", "ss_covered"):
            if k in data:
                out[k] = int(bool(data[k]))
        if "tax_mode" in data:
            if data["tax_mode"] not in ("auto", "off"):
                raise ValueError("tax_mode must be auto or off")
            out["tax_mode"] = data["tax_mode"]
        if "opening_tax_year" in data:
            ty = data["opening_tax_year"]
            if ty is not None and not (isinstance(ty, int) and 2020 <= ty <= 2100):
                raise ValueError("opening_tax_year is a tax year like 2027")
            out["opening_tax_year"] = ty
        for k in ("opening_ytd_taxable", "opening_ytd_tax"):
            if k in data:
                v = _p(data[k] or 0)
                if v < 0:
                    raise ValueError(f"{k} cannot be negative")
                out[k + "_paisa"] = v
        if "pay_method" in data:
            if data["pay_method"] not in MONEY_METHODS:
                raise ValueError(f"pay_method must be one of {', '.join(MONEY_METHODS)}")
            out["pay_method"] = data["pay_method"]
        if "pay_account_id" in data:
            acc = data["pay_account_id"] or None
            if acc and not self._one("SELECT 1 FROM money_accounts WHERE account_id=?", (acc,)):
                raise NotFoundError(f"no such money account: {acc}")
            out["pay_account_id"] = acc
        if "route_ids" in data:
            ri = data["route_ids"] or []
            if not isinstance(ri, list) or any(not isinstance(x, str) for x in ri):
                raise ValueError("route_ids is a list of route ids")
            out["route_ids"] = json.dumps(ri)
        if "cash_allowed" in data:
            out["cash_allowed"] = int(bool(data["cash_allowed"]))
            note = str(data.get("cash_allowed_note") or "").strip()
            if out["cash_allowed"] and len(note) < 3:
                raise ValueError("say why this employee may be paid in cash (e.g. daily-wage loader, no bank account)")
            out["cash_allowed_note"] = note[:200] if out["cash_allowed"] else ""
        return out

    def add_employee(self, data: dict, actor: str, approved_by: str | None = None) -> dict:
        self._pr_need()
        approved_by = _approver(approved_by)
        vals = self._pr_clean(dict(data or {}), partial=False)
        login_requested = bool((data or {}).get("app_login"))
        now = now_iso()
        with immediate_tx(self) as c:
            vals.setdefault("emp_no", self._pr_next_emp_no())
            if self._one("SELECT 1 FROM employees WHERE emp_no=?", (vals["emp_no"],)):
                raise StateError(f"employee number {vals['emp_no']} is taken")
            eid = new_id("EMP")
            if vals.get("cash_allowed"):
                vals.update(cash_allowed_by=approved_by, cash_allowed_at=now)
            cols = {"employee_id": eid, **vals, "created_at": now, "updated_at": now}
            c.execute(f"INSERT INTO employees ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values()))
            self._pr_event(c, eid, "joined", {"emp_no": vals["emp_no"], "joined_on": vals["joined_on"]}, approved_by)
            if vals.get("cash_allowed"):
                self._pr_event(c, eid, "cash_exemption", {"allowed": True, "reason": vals["cash_allowed_note"]}, approved_by)
            self.audit(actor, "add_employee", "employee", eid, {"emp_no": vals["emp_no"], "name": vals["name"],
                                                                "role": vals.get("role_hint", "other"), "login_requested": login_requested,
                                                                **({"cash_allowed": True, "reason": vals["cash_allowed_note"]} if vals.get("cash_allowed") else {})},
                       approved_by)
        out = self.get_employee(eid, include_pay=True)
        return out | {"login_requested": login_requested}

    def _pr_event(self, c, employee_id: str, kind: str, detail: dict, approved_by: str | None = None) -> None:
        c.execute("INSERT INTO employee_events (employee_id, kind, detail, at, by_user, approved_by) VALUES (?,?,?,?,?,?)",
                  (employee_id, kind, json.dumps(detail, default=str), now_iso(), self._current_user(), approved_by))

    def employee_events(self, employee_id: str) -> list[dict]:
        emp = self._pr_emp(employee_id)
        return [dict(r) | {"detail": json.loads(r["detail"])} for r in
                self._all("SELECT * FROM employee_events WHERE employee_id=? ORDER BY event_id", (emp["employee_id"],))]

    def update_employee(self, employee_id: str, changes: dict, actor: str, approved_by: str | None = None) -> dict:
        approved_by = _approver(approved_by)
        emp = self._pr_emp(employee_id)
        changes = dict(changes or {})
        changes.pop("app_login", None)
        vals = self._pr_clean(changes, partial=True)
        if not vals:
            raise ValueError("nothing to change")
        now = now_iso()
        exemption = None
        if "cash_allowed" in vals and vals["cash_allowed"] != emp["cash_allowed"]:
            exemption = {"allowed": bool(vals["cash_allowed"]), "reason": vals["cash_allowed_note"]}
            vals.update(cash_allowed_by=approved_by if vals["cash_allowed"] else None, cash_allowed_at=now if vals["cash_allowed"] else None)
        elif "cash_allowed" in vals:
            vals.pop("cash_allowed"); vals.pop("cash_allowed_note", None)
        if not vals:
            return self.get_employee(emp["employee_id"], include_pay=True)
        with immediate_tx(self) as c:
            if "emp_no" in vals and self._one("SELECT 1 FROM employees WHERE emp_no=? AND employee_id<>?", (vals["emp_no"], emp["employee_id"])):
                raise StateError(f"employee number {vals['emp_no']} is taken")
            sets = ", ".join(f"{k}=?" for k in vals)
            c.execute(f"UPDATE employees SET {sets}, updated_at=? WHERE employee_id=?", (*vals.values(), now, emp["employee_id"]))
            fields = sorted(k for k in vals if not k.startswith("cash_allowed"))
            if fields:
                self._pr_event(c, emp["employee_id"], "updated", {"fields": fields}, approved_by)
            if exemption:
                self._pr_event(c, emp["employee_id"], "cash_exemption", exemption, approved_by)
            self.audit(actor, "update_employee", "employee", emp["employee_id"], {"emp_no": emp["emp_no"], "fields": fields,
                                                                                    **({"cash_allowed": exemption} if exemption else {})}, approved_by)
        return self.get_employee(emp["employee_id"], include_pay=True)

    def end_employment(self, employee_id: str, left_on: str, reason: str, actor: str, approved_by: str | None = None) -> dict:
        approved_by = _approver(approved_by)
        emp = self._pr_emp(employee_id)
        left_on = _day(left_on, "left_on")
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise ValueError("give a reason (at least 3 characters)")
        if emp["status"] != "active":
            raise StateError(f"{emp['name']} has already left (on {emp['left_on']})")
        if left_on < emp["joined_on"]:
            raise ValueError(f"left_on is before {emp['name']} joined ({emp['joined_on']})")
        with immediate_tx(self) as c:
            c.execute("UPDATE employees SET status='left', left_on=?, updated_at=? WHERE employee_id=?", (left_on, now_iso(), emp["employee_id"]))
            self._pr_event(c, emp["employee_id"], "left", {"left_on": left_on, "reason": reason[:200]}, approved_by)
            self.audit(actor, "end_employment", "employee", emp["employee_id"], {"emp_no": emp["emp_no"], "left_on": left_on,
                                                                                   "reason": reason[:200], "had_login": emp["user_id"] is not None}, approved_by)
        return {"employee": self.get_employee(emp["employee_id"], include_pay=True), "user_id": emp["user_id"]}

    def rehire_employee(self, employee_id: str, rejoined_on: str, actor: str, approved_by: str | None = None) -> dict:
        approved_by = _approver(approved_by)
        emp = self._pr_emp(employee_id)
        rejoined_on = _day(rejoined_on, "rejoined_on")
        if emp["status"] != "left":
            raise StateError(f"{emp['name']} is still employed")
        if rejoined_on <= emp["left_on"]:
            raise ValueError(f"rejoined_on must be after {emp['name']} left ({emp['left_on']})")
        with immediate_tx(self) as c:
            c.execute("UPDATE employees SET status='active', left_on=NULL, joined_on=?, updated_at=? WHERE employee_id=?",
                      (rejoined_on, now_iso(), emp["employee_id"]))
            self._pr_event(c, emp["employee_id"], "rehired", {"rejoined_on": rejoined_on, "previous_joined_on": emp["joined_on"],
                                                              "previous_left_on": emp["left_on"]}, approved_by)
            self.audit(actor, "rehire_employee", "employee", emp["employee_id"], {"emp_no": emp["emp_no"], "rejoined_on": rejoined_on}, approved_by)
        return {"employee": self.get_employee(emp["employee_id"], include_pay=True), "user_id": emp["user_id"]}

    def link_login(self, employee_id: str, user_id: str, role: str, actor: str, generated: bool = False) -> dict:
        """Tie a registry login to an employee (the PIN never reaches this database). Secondary audit only: the gated
        action is the employee's creation, and staff:manage is checked by the web layer."""
        emp = self._pr_emp(employee_id)
        if emp["user_id"]:
            raise StateError(f"{emp['name']} already has an app login")
        with immediate_tx(self) as c:
            if self._one("SELECT 1 FROM employees WHERE user_id=?", (user_id,)):
                raise StateError("that login already belongs to another employee")
            c.execute("UPDATE employees SET user_id=?, updated_at=? WHERE employee_id=?", (user_id, now_iso(), emp["employee_id"]))
            self._pr_event(c, emp["employee_id"], "login_created", {"role": role, "generated": bool(generated)})
            self.audit(actor, "employee_login_created", "employee", emp["employee_id"], {"role": role, "generated": bool(generated)})
        return self.get_employee(emp["employee_id"], include_pay=True)

    def login_event(self, user_id: str, kind: str, actor: str, detail: dict | None = None) -> None:
        """Record a login change made in the registry (disabled / enabled / PIN reset / role change) on the employee's
        life history. Never a PIN in `detail`. No-op when payroll is not enabled or the user has no employee row."""
        if not self.payroll_ready():
            return
        r = self._one("SELECT employee_id FROM employees WHERE user_id=?", (user_id,))
        if not r:
            return
        secondary = {"login_disabled": "employee_login_disabled", "pin_reset": "employee_pin_reset"}.get(kind)
        detail = {k: v for k, v in (detail or {}).items() if "pin" not in k.lower() or k == "generated"}
        with immediate_tx(self) as c:
            self._pr_event(c, r["employee_id"], kind, detail)
            if secondary:
                self.audit(actor, secondary, "employee", r["employee_id"], detail)

    def sync_employees_from_users(self, users: list[dict], actor: str) -> int:
        """Every registry user of this business without an employee row gets one (no pay terms: excluded from payroll
        until the owner sets them). Returns how many were created."""
        self._pr_need()
        hint = {"owner": "other", "clerk": "clerk", "salesman": "salesman", "driver": "driver"}
        made = []
        with immediate_tx(self) as c:
            for u in users:
                if not u.get("user_id") or self._one("SELECT 1 FROM employees WHERE user_id=?", (u["user_id"],)):
                    continue
                name = (u.get("name") or "").strip()[:60]
                if len(name) < 2:
                    continue
                now = now_iso()
                eid, emp_no = new_id("EMP"), self._pr_next_emp_no()
                joined = (u.get("created_at") or now)[:10]
                c.execute("INSERT INTO employees (employee_id, emp_no, name, phone, designation, role_hint, joined_on, province, user_id, created_at, updated_at) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (eid, emp_no, name, u.get("phone") or "", u.get("role", ""), hint.get(u.get("role"), "other"), joined,
                           self.payroll_setting("payroll_province"), u["user_id"], now, now))
                self._pr_event(c, eid, "joined", {"emp_no": emp_no, "from_login": True})
                if not u.get("active", True):
                    c.execute("UPDATE employees SET status='left', left_on=? WHERE employee_id=?", (today_iso(), eid))
                made.append(emp_no)
            if made:
                self.audit(actor, "employees_synced", "employee", "sync", {"created": made})
        return len(made)

    # ================================================================== pay terms
    def _pr_structure_at(self, employee_id: str, on: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM pay_structures WHERE employee_id=? AND effective_from<=? "
                         "ORDER BY effective_from DESC, structure_id DESC LIMIT 1", (employee_id, on))

    @staticmethod
    def _pr_structure_out(s) -> dict:
        comps = json.loads(s["components"] or "[]")
        return {"structure_id": s["structure_id"], "effective_from": s["effective_from"], "pay_basis": s["pay_basis"],
                "basic": _r(s["basic_paisa"]), "daily_rate": _r(s["daily_rate_paisa"]), "ot_eligible": bool(s["ot_eligible"]),
                "components": [{k: v for k, v in c.items() if k not in ("amount_paisa", "rate_bp")}
                               | {"amount": _r(c.get("amount_paisa", 0)), "rate_pct": c.get("rate_bp", 0) / 100} for c in comps]}

    @staticmethod
    def _pr_components(components) -> list[dict]:
        out = []
        for i, c in enumerate(components or ()):
            if not isinstance(c, dict):
                raise ValueError("each pay component is an object")
            calc = c.get("calc", "fixed")
            if calc not in ("fixed", "per_day", "per_trip", "pct_basic"):
                raise ValueError("component calc must be fixed, per_day, per_trip or pct_basic")
            side = c.get("side", "earning")
            if side not in ("earning", "deduction"):
                raise ValueError("component side must be earning or deduction")
            label = str(c.get("label") or c.get("code") or "").strip()
            if not 2 <= len(label) <= 40:
                raise ValueError("each component needs a label of 2-40 characters")
            comp = {"code": str(c.get("code") or f"C{i + 1}")[:20], "label": label, "side": side, "calc": calc,
                    "taxable": int(bool(c.get("taxable", side == "earning"))), "prorate": int(bool(c.get("prorate", False)))}
            if calc == "pct_basic":
                bp = round(float(c.get("rate_pct", 0)) * 100)
                if not 0 < bp <= 10000:
                    raise ValueError("a % of basic component needs rate_pct between 0 and 100")
                comp["rate_bp"] = bp
            else:
                amt = _p(c.get("amount", 0))
                if amt <= 0:
                    raise ValueError(f"component {label!r} needs a positive amount")
                comp["amount_paisa"] = amt
            out.append(comp)
        return out

    def set_pay_structure(self, employee_id: str, effective_from: str, pay_basis: str, basic: float = 0.0, daily_rate: float = 0.0,
                          components: tuple = (), ot_eligible: bool = True, actor: str = "", approved_by: str | None = None) -> dict:
        approved_by = _approver(approved_by)
        emp = self._pr_emp(employee_id)
        effective_from = _day(effective_from, "effective_from")
        if pay_basis not in ("monthly", "daily"):
            raise ValueError("pay_basis must be monthly or daily")
        basic_p, daily_p = _p(basic or 0), _p(daily_rate or 0)
        if pay_basis == "monthly" and (basic_p <= 0 or daily_p):
            raise ValueError("a monthly structure needs a basic salary (and no daily rate)")
        if pay_basis == "daily" and (daily_p <= 0 or basic_p):
            raise ValueError("a daily structure needs a daily rate (and no monthly basic)")
        comps = self._pr_components(components)
        warnings = []
        mw = self._pr_rate_int(f"min_wage_{'monthly' if pay_basis == 'monthly' else 'daily'}", emp["province"], effective_from)
        if pay_basis == "daily" and mw is None:
            mm = self._pr_rate_int("min_wage_monthly", emp["province"], effective_from)
            mw = mul_div(mm, 1, 26) if mm else None
        if mw and (basic_p or daily_p) < mw:
            warnings.append(f"below the {emp['province'].title()} minimum wage (Rs {mw / 100:,.2f})")
        with immediate_tx(self) as c:
            c.execute("INSERT INTO pay_structures (employee_id, effective_from, pay_basis, basic_paisa, daily_rate_paisa, ot_eligible, components, set_by, approved_by, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)", (emp["employee_id"], effective_from, pay_basis, basic_p, daily_p, int(bool(ot_eligible)),
                                                        json.dumps(comps), self._current_user() or actor, approved_by, now_iso()))
            sid = c.lastrowid
            self._pr_event(c, emp["employee_id"], "updated", {"pay_structure": sid, "effective_from": effective_from}, approved_by)
            self.audit(actor, "set_pay_structure", "employee", emp["employee_id"], {"emp_no": emp["emp_no"], "structure_id": sid,
                                                                                      "effective_from": effective_from, "pay_basis": pay_basis}, approved_by)
        return self._pr_structure_out(self._one("SELECT * FROM pay_structures WHERE structure_id=?", (sid,))) | {"warnings": warnings}

    @staticmethod
    def _pr_rule_out(r) -> dict:
        return {"rule_id": r["rule_id"], "employee_id": r["employee_id"], "basis": r["basis"], "sku": r["sku"],
                "rate_pct": r["rate_bp"] / 100, "per_unit": _r(r["per_unit_paisa"]), "min_basis": _r(r["min_basis_paisa"]),
                "effective_from": r["effective_from"], "effective_to": r["effective_to"]}

    def set_commission_rule(self, employee_id: str, basis: str, rate_pct: float = 0.0, per_unit: float = 0.0, sku: str | None = None,
                            min_basis: float = 0.0, effective_from: str | None = None, actor: str = "", approved_by: str | None = None) -> dict:
        """A new rule for (employee, basis, sku) ends the previous one the day before; rate 0 and per-unit 0 just ends it."""
        approved_by = _approver(approved_by)
        emp = self._pr_emp(employee_id)
        if basis not in ("booked_sales", "route_collections", "booked_qty"):
            raise ValueError("basis must be booked_sales, route_collections or booked_qty")
        effective_from = _day(effective_from or today_iso(), "effective_from")
        rate_bp, unit_p, min_p = round(float(rate_pct or 0) * 100), _p(per_unit or 0), _p(min_basis or 0)
        if not 0 <= rate_bp <= 10000 or unit_p < 0 or min_p < 0:
            raise ValueError("commission rate is 0-100% and amounts are not negative")
        if basis == "booked_qty" and rate_bp:
            raise ValueError("a per-unit (booked_qty) rule takes per_unit, not rate_pct")
        if basis != "booked_qty" and unit_p:
            raise ValueError("per_unit applies to booked_qty rules only")
        sku = (sku or "").strip().upper() or None
        if sku and basis != "booked_qty":
            raise ValueError("a SKU applies to booked_qty rules only")
        ending = (not rate_bp and not unit_p)
        day_before = (date.fromisoformat(effective_from) - timedelta(days=1)).isoformat()
        with immediate_tx(self) as c:
            prev = self._all("SELECT * FROM commission_rules WHERE employee_id=? AND basis=? AND sku IS ? AND effective_to IS NULL",
                             (emp["employee_id"], basis, sku))
            for p in prev:
                if p["effective_from"] >= effective_from:
                    raise StateError(f"rule {p['rule_id']} already starts on {p['effective_from']}; pick a later date")
                c.execute("UPDATE commission_rules SET effective_to=? WHERE rule_id=?", (day_before, p["rule_id"]))
            rid = None
            if not ending:
                rid = new_id("COM")
                c.execute("INSERT INTO commission_rules (rule_id, employee_id, basis, sku, rate_bp, per_unit_paisa, min_basis_paisa, effective_from, set_by, approved_by, created_at) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (rid, emp["employee_id"], basis, sku, rate_bp, unit_p, min_p, effective_from,
                                                            self._current_user() or actor, approved_by, now_iso()))
            elif not prev:
                raise ValueError("no open rule to end; give a rate or a per-unit amount")
            self.audit(actor, "set_commission_rule", "employee", emp["employee_id"], {"emp_no": emp["emp_no"], "rule_id": rid, "basis": basis,
                                                                                        "ended": [p["rule_id"] for p in prev], "effective_from": effective_from}, approved_by)
        if rid is None:
            return {"rule_id": None, "ended": [p["rule_id"] for p in prev]}
        return self._pr_rule_out(self._one("SELECT * FROM commission_rules WHERE rule_id=?", (rid,)))

    def _pr_names(self, emp) -> list[str]:
        return sorted({emp["name"].strip().lower(), *[a.strip().lower() for a in json.loads(emp["aliases"] or "[]") if a.strip()]})

    def _pr_commission_basis(self, rule, emp, start: str, end: str) -> tuple[int, int]:
        """(basis, commission) for one rule over the part of [start, end] it is in force. Booked sales and quantities come
        from the salesman's OWN delivered sales: sale_lines of orders whose created_by is his name or an alias."""
        lo = max(start, rule["effective_from"])
        hi = min(end, rule["effective_to"]) if rule["effective_to"] else end
        if hi < lo:
            return 0, 0
        day = sql_business_date("sl.created_at")
        names = self._pr_names(emp)
        marks = ", ".join("?" * len(names))
        if rule["basis"] in ("booked_sales", "booked_qty"):
            what = "sl.revenue_paisa" if rule["basis"] == "booked_sales" else "sl.qty"
            sql = (f"SELECT COALESCE(SUM({what}), 0) v FROM sale_lines sl JOIN orders o ON o.order_id = sl.order_id "
                   f"WHERE lower(trim(o.created_by)) IN ({marks}) AND {day} BETWEEN ? AND ?")
            args: tuple = (*names, lo, hi)
            if rule["basis"] == "booked_qty" and rule["sku"]:
                sql += " AND sl.sku = ?"; args += (rule["sku"],)
            basis = int(self._one(sql, args)["v"])
        else:
            routes = json.loads(emp["route_ids"] or "[]")
            if not routes:
                return 0, 0
            rm = ", ".join("?" * len(routes))
            basis = -int(self._one(f"SELECT COALESCE(SUM(l.amount), 0) v FROM ledger l JOIN customers cu ON cu.customer_id = l.customer_id "
                                   f"WHERE l.kind='payment' AND cu.route_id IN ({rm}) AND {sql_business_date('l.created_at')} BETWEEN ? AND ?",
                                   (*routes, lo, hi))["v"])
        if basis < rule["min_basis_paisa"] or basis <= 0:
            return basis, 0
        if rule["basis"] == "booked_qty":
            return basis, basis * rule["per_unit_paisa"]
        return basis, mul_div(basis, rule["rate_bp"], 10000)

    # ================================================================== the month: attendance and adjustments
    def _pr_standing_run(self, period: str) -> sqlite3.Row | None:
        return self._one(f"SELECT * FROM payroll_runs r WHERE r.period=? AND r.kind='regular' AND {_STANDING_SQL.format(r='r')} "
                         "ORDER BY generation DESC LIMIT 1", (period,))

    def _pr_unlocked(self, period: str) -> None:
        run = self._pr_standing_run(period)
        if run:
            raise StateError(f"{_month_name(period)} payroll is approved ({run['run_id']}): reverse the run to change attendance or adjustments")

    def _pr_employed(self, period: str, employee_ids: list[str] | None = None) -> list[sqlite3.Row]:
        start, end, _ = R.period_bounds(period)
        rows = self._all("SELECT * FROM employees WHERE joined_on<=? AND (left_on IS NULL OR left_on>=?) ORDER BY emp_no", (end, start))
        if employee_ids:
            want = set(employee_ids)
            rows = [r for r in rows if r["employee_id"] in want or r["emp_no"] in want]
        return rows

    def set_attendance(self, period: str, rows: list[dict], actor: str, approved_by: str | None = None) -> dict:
        """Monthly attendance totals (days in half-day steps, leave, OT minutes, trips). No rupee in or out: clerks use it."""
        self._pr_need()
        start, end, dim = R.period_bounds(period)
        if not rows:
            raise ValueError("no attendance rows")
        employed = {r["employee_id"]: r for r in self._pr_employed(period)}
        by_no = {r["emp_no"]: r for r in employed.values()}
        clean = []
        seen = set()
        for row in rows:
            key = row.get("employee_id") or row.get("emp_no")
            emp = employed.get(key) or by_no.get(key)
            if not emp:
                raise NotFoundError(f"{key} is not employed in {_month_name(period)}")
            if emp["employee_id"] in seen:
                raise ValueError(f"{emp['name']} appears twice")
            seen.add(emp["employee_id"])
            v = {"days_worked_x2": _half_days(row.get("days_worked"), "days_worked"),
                 "unpaid_absent_x2": _half_days(row.get("unpaid_absent"), "unpaid_absent"),
                 "annual_leave_x2": _half_days(row.get("annual_leave"), "annual_leave"),
                 "casual_leave_x2": _half_days(row.get("casual_leave"), "casual_leave"),
                 "sick_leave_x2": _half_days(row.get("sick_leave"), "sick_leave")}
            if sum(v.values()) > 2 * dim:
                raise ValueError(f"{emp['name']}: days worked, absent and on leave add up to more than {dim} days")
            ot = row.get("ot_minutes")
            if ot is None and row.get("ot_hours") is not None:
                ot = round(float(row["ot_hours"]) * 60)
            for k, val in (("ot_minutes", ot), ("restday_ot_minutes", row.get("restday_ot_minutes")),
                           ("holiday_ot_minutes", row.get("holiday_ot_minutes")), ("trips", row.get("trips"))):
                iv = int(val or 0)
                if iv < 0 or iv > 100000:
                    raise ValueError(f"{emp['name']}: {k} out of range")
                v[k] = iv
            src = row.get("source") or "register"
            if src not in ("register", "whatsapp", "app", "import"):
                raise ValueError("source must be register, whatsapp, app or import")
            v["source"] = src
            clean.append((emp["employee_id"], v))
        with immediate_tx(self) as c:
            self._pr_unlocked(period)
            who, now = self._current_user() or actor, now_iso()
            for eid, v in clean:
                cols = list(v)
                c.execute(f"INSERT INTO attendance_months (employee_id, period, {', '.join(cols)}, recorded_by, recorded_at) "
                          f"VALUES (?,?,{', '.join('?' * len(cols))},?,?) ON CONFLICT(employee_id, period) DO UPDATE SET "
                          + ", ".join(f"{k}=excluded.{k}" for k in cols) + ", recorded_by=excluded.recorded_by, recorded_at=excluded.recorded_at",
                          (eid, period, *v.values(), who, now))
            self.audit(actor, "record_attendance", "attendance", period, {"period": period, "employees": [e for e, _ in clean]}, approved_by)
        return self.attendance(period)

    def attendance(self, period: str) -> dict:
        """The month's register for everyone employed in it: days, leave, OT and trips only -- never pay."""
        self._pr_need()
        R.period_bounds(period)
        got = {r["employee_id"]: r for r in self._all("SELECT * FROM attendance_months WHERE period=?", (period,))}
        rows = []
        for e in self._pr_employed(period):
            a = got.get(e["employee_id"])
            rows.append({"employee_id": e["employee_id"], "emp_no": e["emp_no"], "name": e["name"], "designation": e["designation"] or e["role_hint"],
                         "recorded": a is not None,
                         "days_worked": (a["days_worked_x2"] / 2) if a else None, "unpaid_absent": (a["unpaid_absent_x2"] / 2) if a else None,
                         "annual_leave": (a["annual_leave_x2"] / 2) if a else None, "casual_leave": (a["casual_leave_x2"] / 2) if a else None,
                         "sick_leave": (a["sick_leave_x2"] / 2) if a else None, "ot_hours": round(a["ot_minutes"] / 60, 2) if a else None,
                         "restday_ot_minutes": a["restday_ot_minutes"] if a else None, "holiday_ot_minutes": a["holiday_ot_minutes"] if a else None,
                         "trips": a["trips"] if a else None, "source": a["source"] if a else None,
                         "recorded_by": a["recorded_by"] if a else None})
        locked = self._pr_standing_run(period) is not None
        tbl = table(f"Attendance {_month_name(period)}", [col("emp_no", "No."), col("name", "Name"), col("days_worked", "Days", "days"),
                                                          col("unpaid_absent", "Absent", "days"), col("annual_leave", "Annual", "days"),
                                                          col("casual_leave", "Casual", "days"), col("sick_leave", "Sick", "days"),
                                                          col("ot_hours", "OT h", "qty"), col("trips", "Trips", "qty")], rows,
                    note="Locked: the month's payroll is approved." if locked else None)
        return {"period": period, "locked": locked, "rows": rows, "table": tbl}

    def add_payroll_adjustment(self, employee_id: str, period: str, code: str, amount: float, note: str, ref: str | None = None,
                               taxable: bool = True, actor: str = "", approved_by: str | None = None) -> dict:
        approved_by = _approver(approved_by)
        emp = self._pr_emp(employee_id)
        start, end, _ = R.period_bounds(period)
        if code not in ADJUSTMENT_CODES:
            raise ValueError(f"code must be one of {', '.join(ADJUSTMENT_CODES)}")
        amt = _p(amount)
        if amt <= 0:
            raise ValueError("an adjustment amount is positive")
        note = (note or "").strip()
        if len(note) < 3:
            raise ValueError("an adjustment needs a note" + (" recording the show-cause" if code == "fine" else ""))
        if emp["joined_on"] > end or (emp["left_on"] and emp["left_on"] < start):
            raise StateError(f"{emp['name']} was not employed in {_month_name(period)}")
        earning = code in ("bonus", "arrears", "other_earning")
        if code == "fine":
            age = R.age_on(emp["date_of_birth"], end)
            if age is not None and age < 18:
                raise StateError(f"{emp['name']} is under 18 and may not be fined")
        if code == "loss_recovery":
            if not ref:
                raise ValueError("a loss recovery names the cash-shortage entry it recovers (ref)")
            open_p = self._pr_open_shortage(ref, exclude_adj=None)
            if amt > open_p:
                raise StateError(f"only Rs {open_p / 100:,.2f} of shortage {ref} is still open")
        elif ref:
            ref = str(ref)[:40]
        with immediate_tx(self) as c:
            self._pr_unlocked(period)
            aid = new_id("ADJ")
            c.execute("INSERT INTO payroll_adjustments (adj_id, employee_id, period, code, amount_paisa, taxable, note, ref, created_by, approved_by, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (aid, emp["employee_id"], period, code, amt, int(bool(taxable) and earning),
                                                        note[:200], ref, self._current_user() or actor, approved_by, now_iso()))
            self.audit(actor, "add_payroll_adjustment", "payroll_adjustment", aid, {"employee_id": emp["employee_id"], "period": period, "code": code}, approved_by)
        return self._pr_adj_out(self._one("SELECT * FROM payroll_adjustments WHERE adj_id=?", (aid,)))

    @staticmethod
    def _pr_adj_out(a) -> dict:
        return {"adj_id": a["adj_id"], "employee_id": a["employee_id"], "period": a["period"], "code": a["code"], "amount": _r(a["amount_paisa"]),
                "taxable": bool(a["taxable"]), "note": a["note"], "ref": a["ref"], "voided": a["voided_by"] is not None,
                "created_by": a["created_by"], "created_at": a["created_at"]}

    def payroll_adjustments(self, period: str) -> dict:
        self._pr_need()
        R.period_bounds(period)
        rows = [self._pr_adj_out(a) | {"name": a["name"], "emp_no": a["emp_no"]} for a in self._all(
            "SELECT a.*, e.name, e.emp_no FROM payroll_adjustments a JOIN employees e USING (employee_id) WHERE a.period=? ORDER BY a.created_at", (period,))]
        return {"period": period, "adjustments": rows,
                "table": table(f"Adjustments {_month_name(period)}", [col("emp_no", "No."), col("name", "Name"), col("code", "Kind"),
                                                                     col("amount", "Amount", "money"), col("note", "Note"), col("state", "State", badge=True)],
                               [r | {"state": "void" if r["voided"] else "open"} for r in rows])}

    def void_payroll_adjustment(self, adj_id: str, actor: str, approved_by: str | None = None) -> dict:
        self._pr_need()
        approved_by = _approver(approved_by)
        a = self._one("SELECT * FROM payroll_adjustments WHERE adj_id=?", (adj_id,))
        if not a:
            raise NotFoundError(f"no such adjustment: {adj_id}")
        if a["voided_by"]:
            raise StateError(f"{adj_id} is already void")
        with immediate_tx(self) as c:
            self._pr_unlocked(a["period"])
            c.execute("UPDATE payroll_adjustments SET voided_by=?, voided_at=? WHERE adj_id=?", (self._current_user() or actor, now_iso(), adj_id))
            self.audit(actor, "void_payroll_adjustment", "payroll_adjustment", adj_id, {"employee_id": a["employee_id"], "period": a["period"], "code": a["code"]}, approved_by)
        return self._pr_adj_out(self._one("SELECT * FROM payroll_adjustments WHERE adj_id=?", (adj_id,)))

    def _pr_shortage_plan(self, expense_id: str) -> str:
        r = self._one("SELECT category, note, reversal_of FROM expenses WHERE expense_id=?", (expense_id,))
        if not r or r["category"] != "cash_shortage" or r["reversal_of"] or not r["note"] or r["note"].startswith(RECOVERY_NOTE_PREFIX):
            raise NotFoundError(f"{expense_id} is not a cash-shortage entry")
        return r["note"].split(" ", 1)[0]

    def _pr_open_shortage(self, expense_id: str, exclude_adj: str | None) -> int:
        """What is still unrecovered of the shortage plan an expense belongs to: the plan's booked shortages and
        recoveries (deposits and payroll RECOVERY rows, net of reversals), less loss recoveries awaiting approval."""
        plan = self._pr_shortage_plan(expense_id)
        own = ("SELECT expense_id FROM expenses WHERE category='cash_shortage' AND reversal_of IS NULL AND "
               "(substr(note, 1, ?) = ? OR substr(note, 1, ?) = ?)")
        rec = f"{RECOVERY_NOTE_PREFIX} {plan} "
        key = (len(plan) + 1, plan + " ", len(rec), rec)
        booked = int(self._one(f"SELECT COALESCE(SUM(amount), 0) s FROM expenses WHERE expense_id IN ({own}) OR reversal_of IN ({own})",
                               key + key)["s"])
        pending = 0
        for a in self._all("SELECT * FROM payroll_adjustments WHERE code='loss_recovery' AND voided_by IS NULL"):
            if a["adj_id"] == exclude_adj or self._pr_standing_run(a["period"]):
                continue
            try:
                if self._pr_shortage_plan(a["ref"]) == plan:
                    pending += a["amount_paisa"]
            except NotFoundError:
                continue
        return max(0, booked - pending)

    # ================================================================== advances (outstanding)
    def _pr_open_advances(self, employee_id: str, period: str | None = None) -> list[dict]:
        rows = self._all(
            "SELECT a.*, a.amount_paisa "
            " + COALESCE((SELECT SUM(r.amount_paisa) FROM staff_advances r WHERE r.reversal_of = a.advance_id), 0)"
            " + COALESCE((SELECT SUM(x.amount_paisa) FROM staff_advances x WHERE x.applies_to = a.advance_id), 0)"
            " - COALESCE((SELECT SUM(l.amount_paisa) FROM payroll_lines l WHERE l.code='ADV' AND l.ref = a.advance_id), 0) AS outstanding "
            "FROM staff_advances a WHERE a.employee_id=? AND a.kind IN ('advance','loan') AND a.reversal_of IS NULL ORDER BY a.given_on, a.created_at",
            (employee_id,))
        out = []
        for r in rows:
            if r["outstanding"] <= 0:
                continue
            if period and r["start_period"] and r["start_period"] > period:
                continue
            out.append({"advance_id": r["advance_id"], "installment": r["installment_paisa"], "outstanding": int(r["outstanding"]),
                        "start_period": r["start_period"]})
        return out

    # ================================================================== the engine, over one period
    def _pr_rate_int(self, key: str, jurisdiction: str, on: str) -> int | None:
        row = self._pr_rate_row(key, jurisdiction, on)
        if row is None or self._pr_needs_verify(row) or not row["value"].isdigit():
            return None
        return int(row["value"])

    def _pr_rules(self, period: str, province: str, used: dict) -> R.Rules:
        _, end, _ = R.period_bounds(period)
        profile = self.payroll_profile()
        limits = dict(PROFILE_LIMITS[profile])

        def get(key, jur):
            row = self._pr_rate_row(key, jur, end)
            if row is not None:
                used[f"{key}/{jur}"] = {"value": row["value"] if key != "salary_tax_slabs" else json.loads(row["value"]),
                                        "effective_from": row["effective_from"], "source": row["source"], "verified_on": row["verified_on"],
                                        "grade": row["grade"], "needs_verify": self._pr_needs_verify(row), "note": row["note"]}
            if row is None or self._pr_needs_verify(row):
                return None
            return row["value"]

        for k in ("fine_cap_bp", "ot_multiplier_x100", "holiday_ot_multiplier_x100", "advance_cap_min_wages", "advance_instalment_cap_bp"):
            v = get(k, profile)
            if v is not None and v.isdigit() and k in limits and limits[k] is not None:
                limits[k] = int(v)
        slabs = get("salary_tax_slabs", "pk")
        mm = get("min_wage_monthly", province)
        md = get("min_wage_daily", province)
        basis = int(get("working_days_basis", "pk") or 26)
        ss_ceiling = get("ss_wage_ceiling", province)
        return R.Rules(profile=profile, limits=limits, working_days_basis=basis,
                       eobi_registered=self.payroll_setting("eobi_registered") == "1",
                       eobi_base=int(v) if (v := get("eobi_wage_base", "pk")) else None,
                       eobi_ee_bp=int(get("eobi_employee_bp", "pk") or 100), eobi_er_bp=int(get("eobi_employer_bp", "pk") or 500),
                       ss_registered=self.payroll_setting("ss_registered") == "1", ss_rate_bp=int(get("ss_rate_bp", province) or 0),
                       ss_ceiling=int(ss_ceiling) if ss_ceiling and ss_ceiling.isdigit() else None,
                       ss_mode=self.payroll_setting("ss_above_ceiling"), ss_worker_share=int(get("ss_worker_share", province) or 0),
                       slabs=json.loads(slabs) if slabs else None, round_rupee=self.payroll_setting("payroll_tax_round_rupee") == "1",
                       min_wage_monthly=int(mm) if mm else 0, min_wage_daily=int(md) if md else (mul_div(int(mm), 1, basis) if mm else 0))

    def _pr_ytd(self, emp, period: str) -> tuple[int, int]:
        ty = R.tax_year_of(period)
        r = self._one(f"SELECT COALESCE(SUM(s.taxable_paisa), 0) t, COALESCE(SUM(s.tax_paisa), 0) x FROM payslips s JOIN payroll_runs r ON r.run_id = s.run_id "
                      f"WHERE s.employee_id=? AND s.tax_year=? AND s.period<? AND {_STANDING_SQL.format(r='r')}", (emp["employee_id"], ty, period))
        t, x = int(r["t"]), int(r["x"])
        if emp["opening_tax_year"] == ty:
            t += emp["opening_ytd_taxable_paisa"]; x += emp["opening_ytd_tax_paisa"]
        return t, x

    def _pr_leave(self, employee_id: str, period: str, profile: str) -> tuple[dict, dict]:
        """(paid leave this month, balance left this calendar year) in days. v1: entitlement minus leave recorded."""
        year = period[:4]
        r = self._one("SELECT COALESCE(SUM(annual_leave_x2),0) a, COALESCE(SUM(casual_leave_x2),0) c, COALESCE(SUM(sick_leave_x2),0) s "
                      "FROM attendance_months WHERE employee_id=? AND period LIKE ? AND period<=?", (employee_id, f"{year}-%", period))
        m = self._one("SELECT annual_leave_x2 a, casual_leave_x2 c, sick_leave_x2 s FROM attendance_months WHERE employee_id=? AND period=?", (employee_id, period))
        ent = R.LEAVE_ENTITLEMENT[profile]
        bal = {"annual": ent["annual"] - r["a"] / 2, "casual": ent["casual"] - r["c"] / 2, "sick": ent["sick"] - r["s"] / 2}
        this = {"annual": m["a"] / 2, "casual": m["c"] / 2, "sick": m["s"] / 2} if m else {"annual": 0, "casual": 0, "sick": 0}
        return this, bal

    def _pr_compute(self, period: str, employee_ids: list[str] | None = None) -> dict:
        start, end, dim = R.period_bounds(period)
        profile = self.payroll_profile()
        used: dict = {}
        rules_by_prov: dict = {}
        fp_inputs: dict = {"period": period, "profile": profile, "settings": self.payroll_settings(), "employees": {}}
        results, skipped, basis_rows = [], [], []
        for emp in self._pr_employed(period, employee_ids):
            s = self._pr_structure_at(emp["employee_id"], end)
            if s is None:
                skipped.append({"employee_id": emp["employee_id"], "emp_no": emp["emp_no"], "name": emp["name"], "reason": "no pay terms set"})
                continue
            prov = emp["province"]
            if prov not in rules_by_prov:
                rules_by_prov[prov] = self._pr_rules(period, prov, used)
            rules = rules_by_prov[prov]
            att = self._one("SELECT * FROM attendance_months WHERE employee_id=? AND period=?", (emp["employee_id"], period))
            if s["pay_basis"] == "daily" and att is None:
                skipped.append({"employee_id": emp["employee_id"], "emp_no": emp["emp_no"], "name": emp["name"], "reason": "daily wages: no attendance recorded"})
                continue
            adjs = [dict(a) for a in self._all("SELECT * FROM payroll_adjustments WHERE employee_id=? AND period=? AND voided_by IS NULL ORDER BY created_at",
                                               (emp["employee_id"], period))]
            comms = []
            for rule in self._all("SELECT * FROM commission_rules WHERE employee_id=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?) "
                                  "ORDER BY effective_from", (emp["employee_id"], end, start)):
                b, amt = self._pr_commission_basis(rule, emp, start, end)
                what = {"booked_sales": "delivered sales booked", "route_collections": "route collections", "booked_qty": "units booked"}[rule["basis"]]
                label = f"Commission {rule['rate_bp'] / 100:g}% of {what}" if rule["basis"] != "booked_qty" else f"Commission Rs {rule['per_unit_paisa'] / 100:g} x {b} units"
                comms.append({"rule_id": rule["rule_id"], "label": label, "basis": b, "amount": amt})
                basis_rows.append({"emp_no": emp["emp_no"], "name": emp["name"], "rule_id": rule["rule_id"], "basis_kind": rule["basis"],
                                   "basis": b if rule["basis"] == "booked_qty" else _r(b), "commission": _r(amt)})
            advances = self._pr_open_advances(emp["employee_id"], period)
            ytd_t, ytd_x = self._pr_ytd(emp, period)
            age = R.age_on(emp["date_of_birth"], end)
            attd = {k: att[k] for k in ("days_worked_x2", "unpaid_absent_x2", "annual_leave_x2", "casual_leave_x2", "sick_leave_x2",
                                        "ot_minutes", "restday_ot_minutes", "holiday_ot_minutes", "trips")} if att else None
            em = R.EmployeeMonth(employee_id=emp["employee_id"], name=emp["name"], period=period, pay_basis=s["pay_basis"],
                                 basic=s["basic_paisa"], daily_rate=s["daily_rate_paisa"], components=json.loads(s["components"] or "[]"),
                                 ot_eligible=bool(s["ot_eligible"]), days_employed=R.days_employed(period, emp["joined_on"], emp["left_on"]),
                                 attendance=attd,
                                 adjustments=[{"adj_id": a["adj_id"], "code": a["code"], "amount": a["amount_paisa"], "taxable": a["taxable"],
                                               "note": a["note"], "ref": a["ref"]} for a in adjs],
                                 commissions=comms, advances=advances, eobi_covered=bool(emp["eobi_covered"]), ss_covered=bool(emp["ss_covered"]),
                                 tax_mode=emp["tax_mode"], ytd_taxable=ytd_t, ytd_tax=ytd_x, age=age, role_hint=emp["role_hint"])
            res = R.compute_employee(em, rules)
            for a in adjs:           # a loss recovery is capped at the shortage still open (refusal)
                if a["code"] == "loss_recovery":
                    try:
                        open_p = self._pr_open_shortage(a["ref"], exclude_adj=a["adj_id"])
                        if a["amount_paisa"] > open_p:
                            res["errors"].append(f"{emp['name']}: loss recovery {a['adj_id']} is more than the Rs {open_p / 100:,.2f} still open on {a['ref']}")
                    except NotFoundError as ex:
                        res["errors"].append(f"{emp['name']}: {ex}")
            paid_leave, balances = self._pr_leave(emp["employee_id"], period, profile)
            res.update(emp=emp, structure=s, days_worked_x2=(attd or {}).get("days_worked_x2", 0),
                       unpaid_absent_x2=(attd or {}).get("unpaid_absent_x2", 0), paid_leave=paid_leave, leave_balances=balances,
                       tax_year=R.tax_year_of(period), ytd_before=(ytd_t, ytd_x))
            results.append(res)
            fp_inputs["employees"][emp["employee_id"]] = {
                "emp": {k: emp[k] for k in emp.keys() if k not in ("updated_at", "created_at")}, "structure": dict(s), "attendance": dict(att) if att else None,
                "adjustments": [a["adj_id"] for a in adjs], "commissions": comms, "advances": advances, "ytd": [ytd_t, ytd_x]}
        fp_inputs["rules"] = used
        fingerprint = hashlib.sha256(json.dumps(fp_inputs, sort_keys=True, default=str).encode()).hexdigest()
        warnings = [w for r in results for w in r["warnings"]]
        errors = [e for r in results for e in r["errors"]]
        headcount = len(self._pr_employed(period))
        if headcount >= int(self._pr_rate_int("eobi_min_headcount", "pk", end) or 5) and self.payroll_setting("eobi_registered") != "1":
            warnings.append(f"{headcount} staff: EOBI registration is required at 5 or more employees (eobi_registered is off)")
        if self.payroll_setting("payroll_enabled") == "1":
            hand = self._one("SELECT COUNT(*) n FROM expenses WHERE category='salary' AND expense_date BETWEEN ? AND ? AND reversal_of IS NULL", (start, end))["n"]
            if hand:
                warnings.append(f"{hand} hand-keyed 'salary' expense(s) this month: they would count staff cost twice")
        for key, u in used.items():
            if key.startswith("min_wage_monthly/") and "check" in (u.get("note") or ""):
                warnings.append(f"the minimum wage ({key.split('/')[1]}) was last checked {u['verified_on']}: check for a newer notification")
        verified = min((u["verified_on"] for u in used.values()), default=today_iso())
        return {"period": period, "period_start": start, "period_end": end, "profile": profile, "fingerprint": fingerprint,
                "results": results, "skipped": skipped, "warnings": warnings, "errors": errors, "rules": used,
                "rules_verified_on": verified, "commission_basis": basis_rows}

    # ---------------------------------------------------------------- register shape
    @staticmethod
    def _pr_sum(lines, *codes) -> int:
        return sum(ln["amount"] for ln in lines if ln["code"] in codes)

    def _pr_register_row(self, emp_no, name, designation, basis, days_x2, absent_x2, lines, method, paid=0, net=None) -> dict:
        s = lambda *c: self._pr_sum(lines, *c)  # noqa: E731
        gross = s(*R.EARNING_CODES)
        ded = s(*R.DEDUCTION_CODES)
        net = gross - ded if net is None else net
        return {"emp_no": emp_no, "name": name, "designation": designation, "basis": basis, "days": days_x2 / 2, "absent": absent_x2 / 2,
                "basic": _r(s("BASIC")), "overtime": _r(s("OT")), "allowances": _r(s("ALW")), "commission": _r(s("COMM")),
                "bonus": _r(s("BONUS", "ARREARS", "OTHER_EARN")), "gross": _r(gross), "absence": _r(s("ABSENCE")), "eobi_ee": _r(s("EOBI_EE")),
                "ss_ee": _r(s("SS_EE")), "income_tax": _r(s("TAX")), "advance": _r(s("ADV")), "fines_other": _r(s("FINE", "LOSS", "OTHER_DED", "IN_LIEU")),
                "total_deductions": _r(ded), "net_pay": _r(net), "paid": _r(paid), "balance_due": _r(net - paid), "method": method,
                "eobi_er": _r(s("EOBI_ER")), "ss_er": _r(s("SS_ER")), "employer_cost": _r(gross - s("ABSENCE") + s("EOBI_ER", "SS_ER"))}

    _REG_MONEY = ("basic", "overtime", "allowances", "commission", "bonus", "gross", "absence", "eobi_ee", "ss_ee", "income_tax", "advance",
                  "fines_other", "total_deductions", "net_pay", "paid", "balance_due", "eobi_er", "ss_er", "employer_cost")

    def _pr_register_table(self, title: str, rows: list[dict], note: str) -> dict:
        labels = {"basic": "Basic", "overtime": "Overtime", "allowances": "Allowances", "commission": "Commission", "bonus": "Bonus / other",
                  "gross": "Gross", "absence": "Absence", "eobi_ee": "EOBI (ee)", "ss_ee": "Soc. sec. (ee)", "income_tax": "Income tax",
                  "advance": "Advance", "fines_other": "Fines / other", "total_deductions": "Total deductions", "net_pay": "Net pay",
                  "paid": "Paid", "balance_due": "Balance due", "eobi_er": "EOBI (er)", "ss_er": "Soc. sec. (er)", "employer_cost": "Employer cost"}
        cols = [col("emp_no", "No."), col("name", "Name"), col("designation", "Designation"), col("basis", "Basis"),
                col("days", "Days", "days"), col("absent", "Absent", "days")]
        cols += [col(k, labels[k], "money") for k in self._REG_MONEY[:14]] + [col("paid", "Paid", "money"), col("balance_due", "Balance due", "money"),
                                                                               col("method", "Method")]
        cols += [col(k, labels[k], "money") for k in ("eobi_er", "ss_er", "employer_cost")]
        totals = {k: round(sum(r[k] for r in rows), 2) for k in self._REG_MONEY}
        return table(title, cols, rows, totals=totals, note=note)

    def preview_payroll(self, period: str, employee_ids: list[str] | None = None) -> dict:
        """The month as it would be approved: register, every line, warnings, refusals and the input fingerprint."""
        self._pr_need()
        pv = self._pr_compute(period, employee_ids)
        rows, emps = [], []
        for r in pv["results"]:
            e, s = r["emp"], r["structure"]
            rows.append(self._pr_register_row(e["emp_no"], e["name"], e["designation"] or e["role_hint"], s["pay_basis"], r["days_worked_x2"],
                                              r["unpaid_absent_x2"], r["lines"], e["pay_method"] + (" (cash allowed)" if e["cash_allowed"] else "")))
            emps.append({"employee_id": e["employee_id"], "emp_no": e["emp_no"], "name": e["name"], "pay_basis": s["pay_basis"],
                         "gross": _r(r["gross"]), "deductions": _r(r["deductions"]), "net": _r(r["net"]), "employer": _r(r["employer"]),
                         "taxable": _r(r["taxable"]), "tax": _r(r["tax"]),
                         "withholding": {k: (_r(v) if k != "months_remaining" else v) for k, v in (r["withholding"] or {}).items()},
                         "lines": [ln | {"amount": _r(ln["amount"])} for ln in r["lines"]], "warnings": r["warnings"], "errors": r["errors"]})
        existing = self._pr_standing_run(period)
        errors = list(pv["errors"]) + ([f"{_month_name(period)} is already approved ({existing['run_id']})"] if existing else [])
        note = f"{len(pv['warnings'])} warning(s), {len(errors)} problem(s). " + BOUNDARY_TEXT["en"].format(
            verified_on=pv["rules_verified_on"], source="the statutory rates table")
        return {"period": period, "period_start": pv["period_start"], "period_end": pv["period_end"], "profile": pv["profile"],
                "fingerprint": pv["fingerprint"], "employees": emps, "skipped": pv["skipped"], "warnings": pv["warnings"], "errors": errors,
                "can_approve": not errors and bool(emps),
                "totals": {k: _r(sum(r[x] for r in pv["results"])) for k, x in (("gross", "gross"), ("deductions", "deductions"),
                                                                                 ("net", "net"), ("employer", "employer"))},
                "commission_basis": pv["commission_basis"], "rules": pv["rules"], "rules_verified_on": pv["rules_verified_on"],
                "boundary": BOUNDARY_TEXT["en"].format(verified_on=pv["rules_verified_on"], source="the statutory rates table"),
                "table": self._pr_register_table(f"Payroll preview {_month_name(period)}", rows, note)}

    # ---------------------------------------------------------------- approve / reverse
    def approve_payroll(self, period: str, fingerprint: str, actor: str, approved_by: str | None, kind: str = "regular") -> dict:
        self._pr_need()
        approved_by = _approver(approved_by)
        if kind != "regular":
            raise ValueError("only regular monthly runs are supported in v1 (leavers are prorated in the regular run)")
        with immediate_tx(self) as c:
            existing = self._pr_standing_run(period)
            if existing:
                raise StateError(f"{_month_name(period)} payroll is already approved ({existing['run_id']})")
            pv = self._pr_compute(period)
            if pv["fingerprint"] != fingerprint:
                raise StateError("the preview is out of date (attendance, pay terms or rates changed since): preview again, then approve")
            if pv["errors"]:
                raise StateError("payroll cannot be approved: " + "; ".join(pv["errors"][:5]))
            if not pv["results"]:
                raise StateError(f"nobody to pay for {_month_name(period)}")
            end = pv["period_end"]
            self.assert_period_open(end)
            now = now_iso()
            run_id = next_doc_no(self, c, "payroll", now)
            gen = int(self._one("SELECT COALESCE(MAX(generation), 0) g FROM payroll_runs WHERE period=? AND kind='regular'", (period,))["g"]) + 1
            res = pv["results"]
            c.execute("INSERT INTO payroll_runs (run_id, period, kind, generation, period_start, period_end, posted_on, rules, profile, fingerprint, "
                      "gross_paisa, deductions_paisa, net_paisa, employer_paisa, headcount, created_by, approved_by, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (run_id, period, "regular", gen, pv["period_start"], end, end, json.dumps(pv["rules"], default=str), pv["profile"], fingerprint,
                       sum(r["gross"] for r in res), sum(r["deductions"] for r in res), sum(r["net"] for r in res), sum(r["employer"] for r in res),
                       len(res), self._current_user() or actor, approved_by, now))
            for r in res:
                e = r["emp"]
                for ln in r["lines"]:
                    c.execute("INSERT INTO payroll_lines (run_id, employee_id, code, side, label, amount_paisa, qty_x100, rate_paisa, taxable, ref) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?)", (run_id, e["employee_id"], ln["code"], ln["side"], ln["label"], ln["amount"],
                                                              ln["qty_x100"], ln["rate_paisa"], ln["taxable"], ln["ref"]))
                slip = next_doc_no(self, c, "payslip", now)
                w = r["withholding"] or {}
                c.execute("INSERT INTO payslips (slip_id, run_id, employee_id, period, emp_no, name, designation, pay_basis, gross_paisa, deductions_paisa, "
                          "net_paisa, employer_paisa, taxable_paisa, tax_paisa, tax_year, ytd_taxable_paisa, ytd_tax_paisa, days_worked_x2, unpaid_absent_x2, "
                          "paid_leave, leave_balances, tax_detail, pay_method, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (slip, run_id, e["employee_id"], period, e["emp_no"], e["name"], e["designation"] or e["role_hint"], r["structure"]["pay_basis"],
                           r["gross"], r["deductions"], r["net"], r["employer"], r["taxable"], r["tax"], r["tax_year"], r["ytd_taxable"], r["ytd_tax"],
                           r["days_worked_x2"], r["unpaid_absent_x2"], json.dumps(r["paid_leave"]), json.dumps(r["leave_balances"]),
                           json.dumps({"ytd_taxable_before": r["ytd_before"][0], "ytd_tax_before": r["ytd_before"][1], **w}),
                           e["pay_method"], now))
            expense_ids = self._pr_post_expenses(c, run_id, period, end, res)
            self.audit(actor, "approve_payroll_run", "payroll_run", run_id, {"period": period, "headcount": len(res), "generation": gen}, approved_by)
            self.audit(actor, "payroll_expense_posted", "payroll_run", run_id, {"expenses": expense_ids})
        return self.payroll_register(run_id=run_id)

    def _pr_post_expenses(self, c, run_id: str, period: str, on: str, results: list[dict]) -> list[str]:
        """Decision 3: one PAYROLL_METHOD expense row per staff-cost category (so profit_summary includes staff cost), plus
        a negative cash_shortage row per loss recovery (note 'RECOVERY <plan_id> <run_id>')."""
        cats: dict[str, int] = {}
        losses = []
        for r in results:
            basic_cat = "staff_salaries" if r["structure"]["pay_basis"] == "monthly" else "staff_wages"
            for ln in r["lines"]:
                if ln["code"] in _BASIC_LIKE:
                    cats[basic_cat] = cats.get(basic_cat, 0) + (-ln["amount"] if ln["code"] == "ABSENCE" else ln["amount"])
                elif ln["code"] in _CATEGORY:
                    cats[_CATEGORY[ln["code"]]] = cats.get(_CATEGORY[ln["code"]], 0) + ln["amount"]
                elif ln["code"] == "LOSS":
                    losses.append(ln)
        ids = []
        who, now = self._current_user() or "payroll", now_iso()
        for cat in ("staff_salaries", "staff_wages", "staff_allowances", "staff_commission", "staff_bonus", "employer_contributions"):
            if cats.get(cat):
                eid = new_id("EXP")
                c.execute("INSERT INTO expenses (expense_id, category, amount, note, method, paid_by, expense_date, created_at, reversal_of) VALUES (?,?,?,?,?,?,?,?,NULL)",
                          (eid, cat, cats[cat], f"{run_id} {_month_name(period)} payroll", PAYROLL_METHOD, who, on, now))
                ids.append(eid)
        for ln in losses:
            adj = self._one("SELECT ref FROM payroll_adjustments WHERE adj_id=?", (ln["ref"],))
            plan = self._pr_shortage_plan(adj["ref"])
            eid = new_id("EXP")
            c.execute("INSERT INTO expenses (expense_id, category, amount, note, method, paid_by, expense_date, created_at, reversal_of) VALUES (?,?,?,?,?,?,?,?,NULL)",
                      (eid, "cash_shortage", -ln["amount"], f"{RECOVERY_NOTE_PREFIX} {plan} {run_id}", PAYROLL_METHOD, who, on, now))
            ids.append(eid)
        return ids

    def reverse_payroll_run(self, run_id: str, reason: str, actor: str, approved_by: str | None) -> dict:
        """Owner only; only while no salary payment (or statutory payment for the month) stands. Negates every line,
        payslip total and expense row, dated the day of the reversal; the month's attendance unlocks."""
        self._pr_need()
        approved_by = _approver(approved_by)
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise ValueError("a reversal needs a reason (at least 3 characters)")
        with immediate_tx(self) as c:
            run = self._one("SELECT * FROM payroll_runs WHERE run_id=?", (run_id,))
            if not run:
                raise NotFoundError(f"no such payroll run: {run_id}")
            if run["kind"] == "reversal":
                raise StateError(f"{run_id} is itself a reversal")
            done = self._one("SELECT run_id FROM payroll_runs WHERE reversal_of=?", (run_id,))
            if done:
                raise StateError(f"{run_id} was already reversed by {done['run_id']}")
            paid = int(self._one("SELECT COALESCE(SUM(p.amount_paisa), 0) s FROM salary_payments p JOIN payslips s ON s.slip_id = p.slip_id WHERE s.run_id=?", (run_id,))["s"])
            if paid:
                raise StateError(f"Rs {paid / 100:,.2f} of salary is paid against {run_id}: reverse those payments first")
            stat = int(self._one("SELECT COALESCE(SUM(amount_paisa), 0) s FROM statutory_payments WHERE period=?", (run["period"],))["s"])
            if stat:
                raise StateError(f"statutory payments are recorded for {_month_name(run['period'])}: a reversal would leave them unmatched")
            today = today_iso()
            self.assert_period_open(today)
            now = now_iso()
            rev = next_doc_no(self, c, "payroll", now)
            c.execute("INSERT INTO payroll_runs (run_id, period, kind, generation, period_start, period_end, posted_on, rules, profile, fingerprint, "
                      "gross_paisa, deductions_paisa, net_paisa, employer_paisa, headcount, created_by, approved_by, created_at, reversal_of, note) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rev, run["period"], "reversal", run["generation"], run["period_start"], run["period_end"], today, run["rules"], run["profile"],
                       run["fingerprint"], -run["gross_paisa"], -run["deductions_paisa"], -run["net_paisa"], -run["employer_paisa"], run["headcount"],
                       self._current_user() or actor, approved_by, now, run_id, reason[:200]))
            c.execute("INSERT INTO payroll_lines (run_id, employee_id, code, side, label, amount_paisa, qty_x100, rate_paisa, taxable, ref) "
                      "SELECT ?, employee_id, code, side, label, -amount_paisa, qty_x100, rate_paisa, taxable, ref FROM payroll_lines WHERE run_id=? ORDER BY line_id",
                      (rev, run_id))
            exps = self._all("SELECT * FROM expenses WHERE method=? AND reversal_of IS NULL AND (note LIKE ? OR note LIKE ?)",
                             (PAYROLL_METHOD, f"{run_id} %", f"{RECOVERY_NOTE_PREFIX} % {run_id}"))
            who = self._current_user() or actor
            for x in exps:
                c.execute("INSERT INTO expenses (expense_id, category, amount, note, method, paid_by, expense_date, created_at, reversal_of) VALUES (?,?,?,?,?,?,?,?,?)",
                          (new_id("EXP"), x["category"], -int(x["amount"]), f"reversal of {x['expense_id']}: {rev} reverses {run_id}"[:120],
                           PAYROLL_METHOD, who, today, now, x["expense_id"]))
            self.audit(actor, "reverse_payroll_run", "payroll_run", run_id, {"reversal": rev, "period": run["period"], "reason": reason[:200]}, approved_by)
        return {"run_id": run_id, "reversal": rev, "period": run["period"], "reversed_on": today}

    # ---------------------------------------------------------------- reads
    def _pr_slip_paid(self, slip_id: str) -> int:
        return int(self._one("SELECT COALESCE(SUM(amount_paisa), 0) s FROM salary_payments WHERE slip_id=?", (slip_id,))["s"])

    def payroll_register(self, run_id: str | None = None, period: str | None = None) -> dict:
        self._pr_need()
        if run_id:
            run = self._one("SELECT * FROM payroll_runs WHERE run_id=?", (run_id,))
        elif period:
            R.period_bounds(period)
            run = self._pr_standing_run(period) or self._one("SELECT * FROM payroll_runs WHERE period=? AND kind='regular' ORDER BY generation DESC LIMIT 1", (period,))
        else:
            raise ValueError("give a run id or a period")
        if not run:
            raise NotFoundError(f"no payroll run for {run_id or period}")
        base_id = run["reversal_of"] or run["run_id"]
        rows, slips = [], []
        for s in self._all("SELECT * FROM payslips WHERE run_id=? ORDER BY emp_no", (base_id,)):
            lines = [dict(ln) | {"amount": ln["amount_paisa"]} for ln in self._all("SELECT * FROM payroll_lines WHERE run_id=? AND employee_id=? ORDER BY line_id",
                                                                                     (run["run_id"], s["employee_id"]))]
            paid = self._pr_slip_paid(s["slip_id"]) if run["kind"] != "reversal" else 0
            methods = [m["method"] for m in self._all("SELECT DISTINCT method FROM salary_payments WHERE slip_id=? AND reversal_of IS NULL", (s["slip_id"],))]
            net = -s["net_paisa"] if run["kind"] == "reversal" else s["net_paisa"]
            rows.append(self._pr_register_row(s["emp_no"], s["name"], s["designation"], s["pay_basis"], s["days_worked_x2"], s["unpaid_absent_x2"],
                                              lines, ", ".join(methods) or s["pay_method"], paid, net))
            slips.append({"slip_id": s["slip_id"], "employee_id": s["employee_id"], "emp_no": s["emp_no"], "name": s["name"], "net": _r(net),
                          "paid": _r(paid), "balance_due": _r(net - paid)})
        reversed_by = self._one("SELECT run_id, created_at FROM payroll_runs WHERE reversal_of=?", (run["run_id"],))
        status = "reversal" if run["kind"] == "reversal" else ("reversed" if reversed_by else "approved")
        rules = json.loads(run["rules"] or "{}")
        verified = min((u.get("verified_on", "") for u in rules.values()), default="")
        title = f"Payroll {_month_name(run['period'])} ({run['run_id']}" + (", reversed" if reversed_by else "") + ")"
        return {"run": {"run_id": run["run_id"], "period": run["period"], "kind": run["kind"], "generation": run["generation"], "status": status,
                        "posted_on": run["posted_on"], "profile": run["profile"], "gross": _r(run["gross_paisa"]), "deductions": _r(run["deductions_paisa"]),
                        "net": _r(run["net_paisa"]), "employer": _r(run["employer_paisa"]), "headcount": run["headcount"],
                        "approved_by": run["approved_by"], "created_at": run["created_at"], "reversal_of": run["reversal_of"],
                        "reversed_by": reversed_by["run_id"] if reversed_by else None},
                "payslips": slips, "rules_verified_on": verified,
                "table": self._pr_register_table(title, rows, BOUNDARY_TEXT["en"].format(verified_on=verified or "-", source="the statutory rates table"))}

    def _pr_slip_row(self, slip_id: str | None, employee_id: str | None, period: str | None) -> sqlite3.Row:
        self._pr_need()
        if slip_id:
            s = self._one("SELECT * FROM payslips WHERE slip_id=?", (slip_id,))
        elif employee_id and period:
            emp = self._pr_emp(employee_id)
            s = self._one(f"SELECT s.* FROM payslips s JOIN payroll_runs r ON r.run_id = s.run_id WHERE s.employee_id=? AND s.period=? "
                          f"ORDER BY {_STANDING_SQL.format(r='r')} DESC, r.generation DESC LIMIT 1", (emp["employee_id"], period))
        else:
            raise ValueError("give a slip id, or an employee and a period")
        if not s:
            raise NotFoundError(f"no payslip {slip_id or f'for {employee_id} in {period}'}")
        return s

    def payslip_owner(self, slip_id: str) -> str | None:
        """The registry user_id of the slip's employee (None: no login). For the web layer's own-slip check."""
        s = self._pr_slip_row(slip_id, None, None)
        r = self._one("SELECT user_id FROM employees WHERE employee_id=?", (s["employee_id"],))
        return r["user_id"] if r else None

    def payslip(self, slip_id: str | None = None, employee_id: str | None = None, period: str | None = None, lang: str = "en") -> dict:
        """PLC s.165(15) payslip: basic, allowances, gross, itemised deductions, total deductions, net, other payments,
        total payment, leave balances; plus YTD tax, employer EOBI (memo), payments and the boundary text."""
        s = self._pr_slip_row(slip_id, employee_id, period)
        emp = self._one("SELECT * FROM employees WHERE employee_id=?", (s["employee_id"],))
        run = self._one("SELECT * FROM payroll_runs WHERE run_id=?", (s["run_id"],))
        reversed_by = self._one("SELECT run_id FROM payroll_runs WHERE reversal_of=?", (run["run_id"],))
        lines = [dict(ln) for ln in self._all("SELECT * FROM payroll_lines WHERE run_id=? AND employee_id=? ORDER BY line_id", (s["run_id"], s["employee_id"]))]
        order = {c: i for i, c in enumerate(R.SLIP_ORDER)}
        lines.sort(key=lambda ln: order.get(ln["code"], 99))
        pays = [dict(p) for p in self._all("SELECT * FROM salary_payments WHERE slip_id=? ORDER BY created_at", (s["slip_id"],))]
        paid = sum(p["amount_paisa"] for p in pays)
        rows = []
        earn_main = [ln for ln in lines if ln["side"] == "earning" and ln["code"] not in R.OTHER_PAYMENTS]
        other = [ln for ln in lines if ln["side"] == "earning" and ln["code"] in R.OTHER_PAYMENTS]
        deds = [ln for ln in lines if ln["side"] == "deduction"]
        for ln in earn_main:
            shows_rate = ln["qty_x100"] and ln["rate_paisa"] and (ln["code"] == "ALW" or (ln["code"] == "BASIC" and s["pay_basis"] == "daily"))
            q = f" ({ln['qty_x100'] / 100:g} x Rs {ln['rate_paisa'] / 100:,.2f})" if shows_rate else ""
            rows.append({"item": ln["label"] + q, "earnings": _r(ln["amount_paisa"]), "deductions": None})
        main_gross = sum(ln["amount_paisa"] for ln in earn_main)
        rows.append({"item": "Gross (basic and allowances)", "earnings": _r(main_gross), "deductions": None, "_em": True})
        for ln in deds:
            rows.append({"item": ln["label"], "earnings": None, "deductions": _r(ln["amount_paisa"])})
        total_ded = sum(ln["amount_paisa"] for ln in deds)
        rows.append({"item": "Total deductions", "earnings": None, "deductions": _r(total_ded), "_em": True})
        rows.append({"item": "Net remuneration", "earnings": _r(main_gross - total_ded), "deductions": None, "_em": True})
        for ln in other:
            rows.append({"item": ln["label"], "earnings": _r(ln["amount_paisa"]), "deductions": None})
        rows.append({"item": "Total payment", "earnings": _r(s["net_paisa"]), "deductions": None, "_em": True})
        cash_ex = any(p["cash_exemption"] and p["reversal_of"] is None for p in pays)
        rules = json.loads(run["rules"] or "{}")
        verified = min((u.get("verified_on", "") for u in rules.values()), default="")
        eobi_er = sum(ln["amount_paisa"] for ln in lines if ln["code"] == "EOBI_ER")
        ss_er = sum(ln["amount_paisa"] for ln in lines if ln["code"] == "SS_ER")
        status = "reversed" if reversed_by else ("paid" if paid >= s["net_paisa"] else ("part paid" if paid else "unpaid"))
        boundary = BOUNDARY_TEXT.get(lang, BOUNDARY_TEXT["en"]).format(verified_on=verified or "-", source="the statutory rates table")
        meta = {"slip_no": s["slip_id"], "run_id": s["run_id"], "period": s["period"], "month": _month_name(s["period"]), "emp_no": s["emp_no"],
                "name": s["name"], "designation": s["designation"], "cnic_last4": (emp["cnic"] or "")[-4:], "joined_on": emp["joined_on"],
                "pay_basis": s["pay_basis"], "days_worked": s["days_worked_x2"] / 2, "unpaid_absent": s["unpaid_absent_x2"] / 2,
                "paid_leave": json.loads(s["paid_leave"]), "leave_balances": json.loads(s["leave_balances"]), "pay_method": s["pay_method"],
                "paid_status": status, "paid": _r(paid), "balance_due": _r(s["net_paisa"] - paid if not reversed_by else 0),
                "tax_year": s["tax_year"], "ytd_taxable": _r(s["ytd_taxable_paisa"]), "ytd_tax": _r(s["ytd_tax_paisa"]),
                "employer_eobi": _r(eobi_er), "employer_social_security": _r(ss_er), "rules_verified_on": verified, "profile": run["profile"],
                "cash_exemption": cash_ex, "cash_exemption_note": CASH_EXEMPTION_NOTE if cash_ex else None,
                "cash_exemption_reason": emp["cash_allowed_note"] if cash_ex else None, "boundary": boundary,
                "business_name": self.business_name}
        tbl = table(f"Payslip {s['slip_id']} · {s['name']} · {_month_name(s['period'])}",
                    [col("item", "Item"), col("earnings", "Earnings", "money"), col("deductions", "Deductions", "money")], rows,
                    totals={"earnings": _r(main_gross + sum(ln["amount_paisa"] for ln in other)), "deductions": _r(total_ded)},
                    note=(f"{CASH_EXEMPTION_NOTE}. " if cash_ex else "") + boundary)
        return {"slip_id": s["slip_id"], "employee_id": s["employee_id"], "meta": meta, "table": tbl,
                "lines": [{"code": ln["code"], "side": ln["side"], "label": ln["label"], "amount": _r(ln["amount_paisa"])} for ln in lines],
                "payments": [{"payment_id": p["payment_id"], "amount": _r(p["amount_paisa"]), "method": p["method"], "account_id": p["account_id"],
                              "paid_on": p["paid_on"], "ref": p["ref"], "cash_exemption": bool(p["cash_exemption"]), "reversal_of": p["reversal_of"]} for p in pays],
                "gross": _r(s["gross_paisa"]), "deductions": _r(s["deductions_paisa"]), "net": _r(s["net_paisa"]), "status": status}

    def my_payslips(self, user_id: str, limit: int = 12) -> dict:
        """payroll:self. The employee is resolved from the caller's user_id ONLY (never a client-supplied id)."""
        self._pr_need()
        emp = self._one("SELECT * FROM employees WHERE user_id=?", (user_id,))
        if not emp:
            return {"employee": None, "payslips": [], "table": table("My payslips", [col("period", "Month")], [], note="No employee record is linked to your login.")}
        slips = []
        for s in self._all(f"SELECT s.*, {_STANDING_SQL.format(r='r')} AS standing FROM payslips s JOIN payroll_runs r ON r.run_id = s.run_id "
                           "WHERE s.employee_id=? ORDER BY s.period DESC, r.generation DESC LIMIT ?", (emp["employee_id"], max(1, min(int(limit), 36)))):
            paid = self._pr_slip_paid(s["slip_id"])
            slips.append({"slip_id": s["slip_id"], "period": s["period"], "month": _month_name(s["period"]), "gross": _r(s["gross_paisa"]),
                          "deductions": _r(s["deductions_paisa"]), "net": _r(s["net_paisa"]), "paid": _r(paid),
                          "status": "reversed" if not s["standing"] else ("paid" if paid >= s["net_paisa"] else ("part paid" if paid else "unpaid"))})
        return {"employee": self._pr_emp_out(emp, False), "payslips": slips,
                "table": table("My payslips", [col("month", "Month"), col("slip_id", "Slip"), col("gross", "Gross", "money"),
                                               col("deductions", "Deductions", "money"), col("net", "Net", "money"), col("status", "Status", badge=True)], slips)}

    # ================================================================== money out
    def _pr_account(self, method: str, account_id: str | None, on: str) -> str:
        acc = self.resolve_account(method, account_id, on)
        if not acc or acc == UNASSIGNED_ACCOUNT_ID:
            raise StateError(f"which account is this {method} payment made from? Add the bank or wallet account first, or name it")
        if not self._one("SELECT 1 FROM money_accounts WHERE account_id=?", (acc,)):
            raise NotFoundError(f"no such money account: {acc}")
        return acc

    def pay_salaries(self, run_id: str, payments: list[dict], actor: str, approved_by: str | None) -> dict:
        """[{slip_id|employee_id, amount?, method?, account_id?, ref?, paid_on?}] against one approved run; partial allowed,
        never more than the slip's balance. Under plc_2026 cash is refused unless the employee is 'cash allowed'."""
        self._pr_need()
        approved_by = _approver(approved_by)
        if not payments:
            raise ValueError("no payments")
        limits = self._pr_limits()
        out = []
        with immediate_tx(self) as c:
            run = self._one("SELECT * FROM payroll_runs WHERE run_id=?", (run_id,))
            if not run or run["kind"] != "regular":
                raise NotFoundError(f"no approved payroll run {run_id}")
            if self._one("SELECT 1 FROM payroll_runs WHERE reversal_of=?", (run_id,)):
                raise StateError(f"{run_id} was reversed")
            for p in payments:
                if p.get("slip_id"):
                    s = self._one("SELECT * FROM payslips WHERE slip_id=? AND run_id=?", (p["slip_id"], run_id))
                else:
                    emp = self._pr_emp(str(p.get("employee_id") or ""))
                    s = self._one("SELECT * FROM payslips WHERE employee_id=? AND run_id=?", (emp["employee_id"], run_id))
                if not s:
                    raise NotFoundError(f"no payslip for {p.get('slip_id') or p.get('employee_id')} in {run_id}")
                emp = self._one("SELECT * FROM employees WHERE employee_id=?", (s["employee_id"],))
                due = s["net_paisa"] - self._pr_slip_paid(s["slip_id"])
                amt = _p(p["amount"]) if p.get("amount") is not None else due
                if amt <= 0:
                    raise StateError(f"nothing is due to {s['name']} on {s['slip_id']}")
                if amt > due:
                    raise StateError(f"{s['name']} is owed Rs {due / 100:,.2f} on {s['slip_id']}; Rs {amt / 100:,.2f} is more than that")
                method = p.get("method") or emp["pay_method"]
                if method not in MONEY_METHODS:
                    raise ValueError(f"method must be one of {', '.join(MONEY_METHODS)}")
                try:
                    exempt = R.check_payment_method(method, limits, bool(emp["cash_allowed"]), f"{s['name']}'s salary")
                except R.PayrollRefusal as ex:
                    raise StateError(str(ex)) from None
                paid_on = _day(p.get("paid_on") or today_iso(), "paid_on")
                if paid_on < run["period_start"]:
                    raise ValueError("a salary payment cannot be dated before the month it pays")
                self.assert_period_open(paid_on)
                acc_hint = p.get("account_id") or (emp["pay_account_id"] if method != "cash" else None)
                acc = self._pr_account(method, acc_hint, paid_on)
                pid = next_doc_no(self, c, "salary_payment", now_iso())
                c.execute("INSERT INTO salary_payments (payment_id, slip_id, employee_id, amount_paisa, method, account_id, cash_exemption, ref, paid_on, paid_by, approved_by, created_at) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (pid, s["slip_id"], s["employee_id"], amt, method, acc, int(exempt),
                                                              str(p.get("ref") or "")[:60], paid_on, self._current_user() or actor, approved_by, now_iso()))
                out.append({"payment_id": pid, "slip_id": s["slip_id"], "employee_id": s["employee_id"], "name": s["name"], "amount": _r(amt),
                            "method": method, "account_id": acc, "paid_on": paid_on, "cash_exemption": exempt, "balance_due": _r(due - amt)})
            self.audit(actor, "pay_salaries", "payroll_run", run_id, {"payments": [o["payment_id"] for o in out], "count": len(out),
                                                                       "cash_exemption_used": [o["employee_id"] for o in out if o["cash_exemption"]]}, approved_by)
        return {"run_id": run_id, "payments": out}

    def reverse_salary_payment(self, payment_id: str, reason: str, actor: str, approved_by: str | None) -> dict:
        self._pr_need()
        approved_by = _approver(approved_by)
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise ValueError("a reversal needs a reason (at least 3 characters)")
        with immediate_tx(self) as c:
            p = self._one("SELECT * FROM salary_payments WHERE payment_id=?", (payment_id,))
            if not p:
                raise NotFoundError(f"no such salary payment: {payment_id}")
            if p["reversal_of"]:
                raise StateError(f"{payment_id} is itself a reversal")
            done = self._one("SELECT payment_id FROM salary_payments WHERE reversal_of=?", (payment_id,))
            if done:
                raise StateError(f"{payment_id} was already reversed by {done['payment_id']}")
            today = today_iso()
            self.assert_period_open(today)
            rid = next_doc_no(self, c, "salary_payment", now_iso())
            c.execute("INSERT INTO salary_payments (payment_id, slip_id, employee_id, amount_paisa, method, account_id, cash_exemption, ref, paid_on, paid_by, approved_by, created_at, reversal_of) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (rid, p["slip_id"], p["employee_id"], -p["amount_paisa"], p["method"], p["account_id"],
                                                            p["cash_exemption"], reason[:60], today, self._current_user() or actor, approved_by, now_iso(), payment_id))
            self.audit(actor, "reverse_salary_payment", "salary_payment", payment_id, {"reversal": rid, "reason": reason[:200]}, approved_by)
        return {"payment_id": payment_id, "reversal": rid, "reversed_on": today}

    def _pr_monthly_pay(self, employee_id: str, on: str) -> int | None:
        s = self._pr_structure_at(employee_id, on)
        if not s:
            return None
        if s["pay_basis"] == "monthly":
            return s["basic_paisa"]
        basis = self._pr_rate_int("working_days_basis", "pk", on) or 26
        return s["daily_rate_paisa"] * basis

    def advance_schedule(self, amount_paisa: int, installment_paisa: int, start_period: str) -> list[dict]:
        """Planned recovery months for an advance (the payroll takes at most the instalment each month)."""
        out, left, per = [], amount_paisa, start_period
        inst = installment_paisa or amount_paisa
        while left > 0 and len(out) < 120:
            take = min(inst, left)
            left -= take
            out.append({"period": per, "instalment": _r(take), "balance_after": _r(left)})
            y, m = int(per[:4]), int(per[5:])
            per = f"{y + (m == 12)}-{(m % 12) + 1:02d}"
        return out

    def give_staff_advance(self, employee_id: str, amount: float, method: str, account_id: str | None = None, kind: str = "advance",
                           installment: float = 0.0, start_period: str | None = None, note: str = "", actor: str = "",
                           approved_by: str | None = None, given_on: str | None = None) -> dict:
        approved_by = _approver(approved_by)
        emp = self._pr_emp(employee_id)
        if emp["status"] != "active":
            raise StateError(f"{emp['name']} has left")
        if kind not in ("advance", "loan"):
            raise ValueError("kind must be advance or loan (a repayment is repay_staff_advance)")
        if method not in MONEY_METHODS:
            raise ValueError(f"method must be one of {', '.join(MONEY_METHODS)}")
        amt, inst = _p(amount), _p(installment or 0)
        if amt <= 0 or inst < 0:
            raise ValueError("an advance is a positive amount; the instalment cannot be negative")
        given_on = _day(given_on or today_iso(), "given_on")
        start_period = start_period or given_on[:7]
        R.period_bounds(start_period)
        if start_period < given_on[:7]:
            raise ValueError("recovery cannot start before the advance is given")
        limits = self._pr_limits()
        with immediate_tx(self) as c:
            open_p = sum(a["outstanding"] for a in self._pr_open_advances(emp["employee_id"]))
            try:
                exempt = R.check_payment_method(method, limits, bool(emp["cash_allowed"]), "an advance")
                mw = self._pr_rate_int("min_wage_monthly", emp["province"], given_on) or 0
                R.check_advance(amt, mw, limits, open_p)
                if limits.get("advance_instalment_cap_bp") is not None:
                    pay = self._pr_monthly_pay(emp["employee_id"], given_on)
                    if pay is None:
                        raise R.PayrollRefusal("set this employee's pay terms first: the instalment is checked against pay")
                    R.check_instalment(inst, pay, limits)
            except R.PayrollRefusal as ex:
                raise StateError(str(ex)) from None
            self.assert_period_open(given_on)
            acc = self._pr_account(method, account_id, given_on)
            aid = next_doc_no(self, c, "advance", now_iso())
            c.execute("INSERT INTO staff_advances (advance_id, employee_id, kind, amount_paisa, given_on, method, account_id, installment_paisa, start_period, "
                      "cash_exemption, note, created_by, approved_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (aid, emp["employee_id"], kind, amt, given_on, method, acc, inst, start_period, int(exempt), (note or "")[:200],
                       self._current_user() or actor, approved_by, now_iso()))
            self.audit(actor, "give_staff_advance", "staff_advance", aid, {"employee_id": emp["employee_id"], "kind": kind, "method": method,
                                                                            "cash_exemption": exempt}, approved_by)
        return {"advance_id": aid, "employee_id": emp["employee_id"], "name": emp["name"], "kind": kind, "amount": _r(amt), "installment": _r(inst),
                "given_on": given_on, "method": method, "account_id": acc, "start_period": start_period, "cash_exemption": exempt,
                "schedule": self.advance_schedule(amt, inst, start_period)}

    def repay_staff_advance(self, employee_id: str, amount: float, method: str, account_id: str | None = None, actor: str = "",
                            approved_by: str | None = None, paid_on: str | None = None) -> dict:
        """Money handed back directly, oldest advance first (one ADV document per advance it pays down)."""
        approved_by = _approver(approved_by)
        emp = self._pr_emp(employee_id)
        if method not in MONEY_METHODS:
            raise ValueError(f"method must be one of {', '.join(MONEY_METHODS)}")
        amt = _p(amount)
        if amt <= 0:
            raise ValueError("a repayment is a positive amount")
        paid_on = _day(paid_on or today_iso(), "paid_on")
        limits = self._pr_limits()
        rows = []
        with immediate_tx(self) as c:
            opens = self._pr_open_advances(emp["employee_id"])
            owed = sum(a["outstanding"] for a in opens)
            if amt > owed:
                raise StateError(f"{emp['name']} owes Rs {owed / 100:,.2f} in advances; Rs {amt / 100:,.2f} is more than that")
            try:
                exempt = R.check_payment_method(method, limits, bool(emp["cash_allowed"]), "an advance repayment")
            except R.PayrollRefusal as ex:
                raise StateError(str(ex)) from None
            self.assert_period_open(paid_on)
            acc = self._pr_account(method, account_id, paid_on)
            left = amt
            for a in opens:
                if left <= 0:
                    break
                take = min(left, a["outstanding"])
                left -= take
                rid = next_doc_no(self, c, "advance", now_iso())
                c.execute("INSERT INTO staff_advances (advance_id, employee_id, kind, amount_paisa, given_on, method, account_id, applies_to, cash_exemption, "
                          "note, created_by, approved_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (rid, emp["employee_id"], "repayment", -take, paid_on, method, acc, a["advance_id"], int(exempt), "direct repayment",
                           self._current_user() or actor, approved_by, now_iso()))
                rows.append({"advance_id": rid, "applies_to": a["advance_id"], "amount": _r(take), "outstanding_after": _r(a["outstanding"] - take)})
            self.audit(actor, "repay_staff_advance", "employee", emp["employee_id"], {"repayments": [r["advance_id"] for r in rows],
                                                                                       "method": method, "cash_exemption": exempt}, approved_by)
        return {"employee_id": emp["employee_id"], "repayments": rows, "account_id": acc}

    def reverse_staff_advance(self, advance_id: str, reason: str, actor: str, approved_by: str | None) -> dict:
        self._pr_need()
        approved_by = _approver(approved_by)
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise ValueError("a reversal needs a reason (at least 3 characters)")
        with immediate_tx(self) as c:
            a = self._one("SELECT * FROM staff_advances WHERE advance_id=?", (advance_id,))
            if not a:
                raise NotFoundError(f"no such advance: {advance_id}")
            if a["reversal_of"]:
                raise StateError(f"{advance_id} is itself a reversal")
            done = self._one("SELECT advance_id FROM staff_advances WHERE reversal_of=?", (advance_id,))
            if done:
                raise StateError(f"{advance_id} was already reversed by {done['advance_id']}")
            if a["kind"] in ("advance", "loan"):
                rec = int(self._one("SELECT COALESCE(SUM(amount_paisa), 0) s FROM payroll_lines WHERE code='ADV' AND ref=?", (advance_id,))["s"])
                rep = int(self._one("SELECT COALESCE(SUM(amount_paisa), 0) s FROM staff_advances WHERE applies_to=?", (advance_id,))["s"])
                if rec or rep:
                    raise StateError(f"part of {advance_id} is already recovered or repaid: reverse those first")
            today = today_iso()
            self.assert_period_open(today)
            rid = next_doc_no(self, c, "advance", now_iso())
            c.execute("INSERT INTO staff_advances (advance_id, employee_id, kind, amount_paisa, given_on, method, account_id, installment_paisa, start_period, "
                      "applies_to, cash_exemption, note, created_by, approved_by, created_at, reversal_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, a["employee_id"], a["kind"], -a["amount_paisa"], today, a["method"], a["account_id"], 0, None, a["applies_to"],
                       a["cash_exemption"], f"reversal: {reason}"[:200], self._current_user() or actor, approved_by, now_iso(), advance_id))
            self.audit(actor, "reverse_staff_advance", "staff_advance", advance_id, {"reversal": rid, "reason": reason[:200]}, approved_by)
        return {"advance_id": advance_id, "reversal": rid, "reversed_on": today}

    def staff_advances_report(self, employee_id: str | None = None, status: str = "open") -> dict:
        self._pr_need()
        if status not in ("open", "all"):
            raise ValueError("status must be open or all")
        limits = self._pr_limits()
        args: tuple = ()
        where = "a.kind IN ('advance','loan') AND a.reversal_of IS NULL"
        if employee_id:
            where += " AND a.employee_id=?"; args = (self._pr_emp(employee_id)["employee_id"],)
        rows = []
        for a in self._all(f"SELECT a.*, e.name, e.emp_no, e.province, (SELECT advance_id FROM staff_advances r WHERE r.reversal_of=a.advance_id) AS reversed_by "
                           f"FROM staff_advances a JOIN employees e USING (employee_id) WHERE {where} ORDER BY a.given_on, a.created_at", args):
            rec = int(self._one("SELECT COALESCE(SUM(amount_paisa), 0) s FROM payroll_lines WHERE code='ADV' AND ref=?", (a["advance_id"],))["s"])
            rep = -int(self._one("SELECT COALESCE(SUM(amount_paisa), 0) s FROM staff_advances WHERE applies_to=?", (a["advance_id"],))["s"])
            out = 0 if a["reversed_by"] else a["amount_paisa"] - rec - rep
            if status == "open" and out <= 0:
                continue
            flags = []
            mw = self._pr_rate_int("min_wage_monthly", a["province"], a["given_on"]) or 0
            if limits.get("advance_cap_min_wages") and mw and a["amount_paisa"] > limits["advance_cap_min_wages"] * mw:
                flags.append("above PLC cap")
            if a["method"] == "cash":
                flags.append("cash (owner's exemption)" if a["cash_exemption"] else "cash")
            if a["reversed_by"]:
                flags.append(f"reversed by {a['reversed_by']}")
            nxt = None
            if out > 0:
                last = self._one("SELECT MAX(r.period) p FROM payroll_lines l JOIN payroll_runs r ON r.run_id=l.run_id WHERE l.code='ADV' AND l.ref=?", (a["advance_id"],))["p"]
                base = max(a["start_period"] or a["given_on"][:7], last or "")
                if last and base == last:
                    y, m = int(last[:4]), int(last[5:])
                    base = f"{y + (m == 12)}-{(m % 12) + 1:02d}"
                nxt = base
            rows.append({"adv_no": a["advance_id"], "employee_id": a["employee_id"], "emp_no": a["emp_no"], "name": a["name"], "kind": a["kind"],
                         "given_on": a["given_on"], "amount": _r(a["amount_paisa"]), "recovered": _r(rec), "repaid_direct": _r(rep),
                         "outstanding": _r(max(out, 0)), "instalment": _r(a["installment_paisa"]), "next_period": nxt, "method": a["method"],
                         "flags": ", ".join(flags),
                         "schedule": self.advance_schedule(max(out, 0), a["installment_paisa"], nxt) if nxt else []})
        tbl = table("Staff advances" + (" (open)" if status == "open" else ""),
                    [col("adv_no", "Advance"), col("name", "Name"), col("kind", "Kind"), col("given_on", "Given", "date"), col("amount", "Amount", "money"),
                     col("recovered", "Recovered", "money"), col("repaid_direct", "Repaid", "money"), col("outstanding", "Outstanding", "money"),
                     col("instalment", "Instalment", "money"), col("next_period", "Next"), col("method", "Method"), col("flags", "Flags", badge=True)],
                    rows, totals={k: round(sum(r[k] for r in rows), 2) for k in ("amount", "recovered", "repaid_direct", "outstanding")})
        return {"advances": rows, "outstanding": round(sum(r["outstanding"] for r in rows), 2), "table": tbl}

    # ================================================================== statutory
    def statutory_summary(self, period: str, kind: str) -> dict:
        """EOBI, social security or salary withholding for an approved month, with what is paid and still due."""
        self._pr_need()
        R.period_bounds(period)
        if kind not in ("eobi", "ss", "income_tax"):
            raise ValueError("kind must be eobi, ss or income_tax")
        run = self._pr_standing_run(period)
        stat_kinds = ("eobi",) if kind == "eobi" else ("income_tax",) if kind == "income_tax" else ("pessi", "sessi", "kpessi")
        paid = int(self._one(f"SELECT COALESCE(SUM(amount_paisa), 0) s FROM statutory_payments WHERE period=? AND kind IN ({', '.join('?' * len(stat_kinds))})",
                             (period, *stat_kinds))["s"])
        rules = json.loads(run["rules"]) if run else {}
        rows = []
        if run:
            for s in self._all("SELECT s.*, e.cnic, e.eobi_no, e.ss_no, e.province FROM payslips s JOIN employees e USING (employee_id) WHERE s.run_id=? ORDER BY s.emp_no", (run["run_id"],)):
                ln = {r["code"]: r["a"] for r in self._all("SELECT code, SUM(amount_paisa) a FROM payroll_lines WHERE run_id=? AND employee_id=? GROUP BY code",
                                                          (run["run_id"], s["employee_id"]))}
                if kind == "eobi" and ("EOBI_EE" in ln or "EOBI_ER" in ln):
                    base = rules.get("eobi_wage_base/pk", {}).get("value")
                    rows.append({"emp_no": s["emp_no"], "name": s["name"], "cnic": s["cnic"], "eobi_no": s["eobi_no"], "wage_base": _r(int(base)) if base and str(base).isdigit() else None,
                                 "employee_1pct": _r(ln.get("EOBI_EE", 0)), "employer_5pct": _r(ln.get("EOBI_ER", 0)),
                                 "total": _r(ln.get("EOBI_EE", 0) + ln.get("EOBI_ER", 0))})
                elif kind == "ss" and ("SS_ER" in ln or "SS_EE" in ln):
                    wages = s["gross_paisa"] - ln.get("ABSENCE", 0)
                    ceil = rules.get(f"ss_wage_ceiling/{s['province']}", {}).get("value")
                    contrib = min(wages, int(ceil)) if ceil and str(ceil).isdigit() else wages
                    rows.append({"emp_no": s["emp_no"], "name": s["name"], "ss_no": s["ss_no"], "wages": _r(wages), "contributable_wage": _r(contrib),
                                 "employer_share": _r(ln.get("SS_ER", 0)), "worker_share": _r(ln.get("SS_EE", 0)),
                                 "total": _r(ln.get("SS_ER", 0) + ln.get("SS_EE", 0))})
                elif kind == "income_tax":
                    d = json.loads(s["tax_detail"] or "{}")
                    if not s["tax_paisa"] and not d.get("annual_tax"):
                        continue
                    rows.append({"emp_no": s["emp_no"], "name": s["name"], "cnic": s["cnic"], "taxable_this_month": _r(s["taxable_paisa"]),
                                 "ytd_taxable_before": _r(d.get("ytd_taxable_before", 0)), "projected_annual": _r(d.get("projected", 0)),
                                 "annual_tax": _r(d.get("annual_tax", 0)), "ytd_tax_before": _r(d.get("ytd_tax_before", 0)), "tax_this_month": _r(s["tax_paisa"])})
        total_key = {"eobi": "total", "ss": "total", "income_tax": "tax_this_month"}[kind]
        due = round(sum(r[total_key] for r in rows), 2)
        if kind == "eobi":
            u = rules.get("eobi_wage_base/pk", {})
            cols = [col("emp_no", "No."), col("name", "Name"), col("cnic", "CNIC"), col("eobi_no", "EOBI no."), col("wage_base", "Wage base", "money"),
                    col("employee_1pct", "Employee 1%", "money"), col("employer_5pct", "Employer 5%", "money"), col("total", "Total", "money")]
            note = f"Deposit with EOBI by the 15th of next month (verify); base last checked {u.get('verified_on', '-')}."
            src = u.get("source", "EOBI")
        elif kind == "ss":
            cols = [col("emp_no", "No."), col("name", "Name"), col("ss_no", "SS no."), col("wages", "Wages", "money"),
                    col("contributable_wage", "Contributable", "money"), col("employer_share", "Employer", "money"),
                    col("worker_share", "Worker", "money"), col("total", "Total", "money")]
            u = next((v for k, v in rules.items() if k.startswith("ss_wage_ceiling/")), {})
            note = f"Ceiling source: {u.get('source', '-')} (grade {u.get('grade', '-')})."
            src = u.get("source", "the provincial institution")
        else:
            cols = [col("emp_no", "No."), col("name", "Name"), col("cnic", "CNIC"), col("taxable_this_month", "Taxable", "money"),
                    col("ytd_taxable_before", "YTD before", "money"), col("projected_annual", "Projected year", "money"),
                    col("annual_tax", "Annual tax", "money"), col("ytd_tax_before", "Tax before", "money"), col("tax_this_month", "Tax this month", "money")]
            u = rules.get("salary_tax_slabs/pk", {})
            note = "Deposit and filing dates are contested: confirm on IRIS with your advisor."
            src = u.get("source", "the Finance Act")
        boundary = BOUNDARY_TEXT["en"].format(verified_on=u.get("verified_on", "-"), source=src)
        money_cols = [c["key"] for c in cols if c["kind"] == "money" and c["key"] not in ("wage_base", "contributable_wage")]
        tbl = table({"eobi": "EOBI", "ss": "Social security", "income_tax": "Salary tax withheld"}[kind] + f" {_month_name(period)}", cols, rows,
                    totals={k: round(sum((r[k] or 0) for r in rows), 2) for k in money_cols},
                    note=(note if run else f"{_month_name(period)} payroll is not approved yet. ") + " " + boundary)
        return {"period": period, "kind": kind, "run_id": run["run_id"] if run else None, "rows": rows, "due": due, "paid": _r(paid),
                "balance": round(due - _r(paid), 2), "boundary": boundary, "table": tbl}

    def record_statutory_payment(self, kind: str, period: str, amount: float, method: str, account_id: str | None, challan_ref: str,
                                 paid_on: str, actor: str, approved_by: str | None) -> dict:
        self._pr_need()
        approved_by = _approver(approved_by)
        if kind not in STATUTORY_KINDS:
            raise ValueError(f"kind must be one of {', '.join(STATUTORY_KINDS)}")
        R.period_bounds(period)
        if method not in MONEY_METHODS:
            raise ValueError(f"method must be one of {', '.join(MONEY_METHODS)}")
        amt = _p(amount)
        if amt <= 0:
            raise ValueError("a statutory payment is a positive amount")
        paid_on = _day(paid_on or today_iso(), "paid_on")
        challan_ref = (challan_ref or "").strip()[:40]
        with immediate_tx(self) as c:
            run = self._pr_standing_run(period)
            if not run:
                raise StateError(f"{_month_name(period)} payroll is not approved: nothing is due yet")
            codes = STAT_LINES[kind]
            due = int(self._one(f"SELECT COALESCE(SUM(amount_paisa), 0) s FROM payroll_lines WHERE run_id=? AND code IN ({', '.join('?' * len(codes))})",
                                (run["run_id"], *codes))["s"])
            same = [k for k, v in STAT_LINES.items() if v == codes]
            paid = int(self._one(f"SELECT COALESCE(SUM(amount_paisa), 0) s FROM statutory_payments WHERE period=? AND kind IN ({', '.join('?' * len(same))})",
                                 (period, *same))["s"])
            if amt > due - paid:
                raise StateError(f"Rs {(due - paid) / 100:,.2f} of {kind.replace('_', ' ')} is due for {_month_name(period)}; Rs {amt / 100:,.2f} is more than that")
            self.assert_period_open(paid_on)
            acc = self._pr_account(method, account_id, paid_on)
            sid = next_doc_no(self, c, "statutory", now_iso())
            c.execute("INSERT INTO statutory_payments (stat_id, kind, period, amount_paisa, method, account_id, challan_ref, paid_on, created_by, approved_by, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (sid, kind, period, amt, method, acc, challan_ref, paid_on, self._current_user() or actor, approved_by, now_iso()))
            self.audit(actor, "record_statutory_payment", "statutory_payment", sid, {"kind": kind, "period": period, "challan_ref": challan_ref}, approved_by)
        return {"stat_id": sid, "kind": kind, "period": period, "amount": _r(amt), "method": method, "account_id": acc, "challan_ref": challan_ref,
                "paid_on": paid_on, "balance": _r(due - paid - amt)}

    # ================================================================== postings (CONSUMED BY B) and liabilities
    def _payroll_postings(self, start: str | None = None, end: str | None = None) -> list:
        """Every posting the payroll tables imply, dated in [start, end] (inclusive; None = unbounded), plan §4.4:
          run (regular or reversal, on posted_on): Dr staff costs by category / Cr 2100 net (per employee), 2110 TAX,
              2120 EOBI_EE+ER, 2130 SS_EE+ER, 1150 ADV (per employee), 2140 FINE, 6000:cash_shortage LOSS,
              8000 other deductions, 2300 IN_LIEU. ABSENCE reduces the staff-cost debit. A reversal run is the negation.
          salary payment: Dr 2100 / Cr 1000 (its account). advance/loan: Dr 1150 / Cr 1000; repayment the reverse.
          statutory payment: Dr 2110 | 2120 | 2130 / Cr 1000.
        B's projection must SKIP expenses rows with method='payroll' (these postings are their double-entry)."""
        if not self.payroll_ready():
            return []
        lo, hi = start or "0000-00-00", end or "9999-12-31"
        out: list[Posting] = []
        for run in self._all("SELECT * FROM payroll_runs WHERE posted_on BETWEEN ? AND ? ORDER BY posted_on, created_at", (lo, hi)):
            on, rid = run["posted_on"], run["run_id"]
            base = run["reversal_of"] or rid
            basis = {r["employee_id"]: r["pay_basis"] for r in self._all("SELECT employee_id, pay_basis FROM payslips WHERE run_id=?", (base,))}
            cost: dict[str, int] = {}
            credit: dict[tuple, int] = {}
            for ln in self._all("SELECT employee_id, code, SUM(amount_paisa) a FROM payroll_lines WHERE run_id=? GROUP BY employee_id, code", (rid,)):
                code, amt, emp = ln["code"], int(ln["a"]), ln["employee_id"]
                if code in _BASIC_LIKE:
                    cat = "staff_salaries" if basis.get(emp, "monthly") == "monthly" else "staff_wages"
                    cost[cat] = cost.get(cat, 0) + (-amt if code == "ABSENCE" else amt)
                elif code in _CATEGORY:
                    cost[_CATEGORY[code]] = cost.get(_CATEGORY[code], 0) + amt
                if (code in R.DEDUCTION_CODES and code != "ABSENCE") or code in R.EMPLOYER_CODES:
                    key = (_CREDIT[code], emp if code == "ADV" else None)
                    credit[key] = credit.get(key, 0) + amt
            nets = self._all("SELECT employee_id, SUM(CASE WHEN side='earning' THEN amount_paisa WHEN side='deduction' THEN -amount_paisa ELSE 0 END) n "
                             "FROM payroll_lines WHERE run_id=? GROUP BY employee_id", (rid,))
            kw = {"source": "payroll_run", "source_id": rid}
            for cat in ("staff_salaries", "staff_wages", "staff_allowances", "staff_commission", "staff_bonus", "employer_contributions"):
                p = _leg(on, _CATEGORY_CODE[cat], cost.get(cat, 0), True, memo=cat, **kw)
                if p:
                    out.append(p)
            for n in nets:
                p = _leg(on, SALARIES_PAYABLE, int(n["n"]), False, party_kind="employee", party_id=n["employee_id"], memo="net pay", **kw)
                if p:
                    out.append(p)
            for (code, emp), amt in sorted(credit.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
                p = _leg(on, code, amt, False, **({"party_kind": "employee", "party_id": emp} if emp else {}), **kw)
                if p:
                    out.append(p)
        for p in self._all("SELECT * FROM salary_payments WHERE paid_on BETWEEN ? AND ? ORDER BY paid_on, created_at", (lo, hi)):
            out += signed(p["paid_on"], SALARIES_PAYABLE, MONEY, p["amount_paisa"], source="salary_payment", source_id=p["payment_id"],
                          debit_party_kind="employee", debit_party_id=p["employee_id"], credit_money_account_id=p["account_id"])
        for a in self._all("SELECT * FROM staff_advances WHERE given_on BETWEEN ? AND ? ORDER BY given_on, created_at", (lo, hi)):
            out += signed(a["given_on"], STAFF_ADVANCES, MONEY, a["amount_paisa"], source="staff_advance", source_id=a["advance_id"],
                          debit_party_kind="employee", debit_party_id=a["employee_id"], credit_money_account_id=a["account_id"] or CASH_ACCOUNT_ID)
        for s in self._all("SELECT * FROM statutory_payments WHERE paid_on BETWEEN ? AND ? ORDER BY paid_on, created_at", (lo, hi)):
            out += signed(s["paid_on"], STAT_CODE[s["kind"]], MONEY, s["amount_paisa"], source="statutory_payment", source_id=s["stat_id"],
                          credit_money_account_id=s["account_id"])
        return out

    def payroll_liabilities_paisa(self, as_of: str | None = None) -> dict:
        """Balances as of a date from _payroll_postings: liabilities credit-positive, 1150 staff advances debit-positive."""
        out = {code: 0 for code in (SALARIES_PAYABLE, INCOME_TAX_WITHHELD, EOBI_PAYABLE, SOCIAL_SECURITY_PAYABLE, STAFF_WELFARE_FUND, STAFF_ADVANCES)}
        for p in self._payroll_postings(None, as_of):
            if p.code in out:
                out[p.code] += (p.debit_paisa - p.credit_paisa) if p.code == STAFF_ADVANCES else (p.credit_paisa - p.debit_paisa)
        return out

    # ================================================================== idempotency for REST writes that move money
    def payroll_idempotent(self, key: str | None, action: str, request: dict, fn):
        """Run fn() once per idempotency key: a retried request with the same key and body returns the first result;
        the same key with a different body is refused. The key row and fn's writes commit together."""
        if not key:
            return fn()
        self._pr_need()
        key = str(key).strip()
        if not 8 <= len(key) <= 80:
            raise ValueError("Idempotency-Key must be 8-80 characters")
        h = hashlib.sha256(json.dumps({"a": action, "r": request}, sort_keys=True, default=str).encode()).hexdigest()
        with immediate_tx(self) as c:
            row = self._one("SELECT * FROM payroll_requests WHERE idem_key=?", (key,))
            if row:
                if row["action"] != action or row["request_hash"] != h:
                    raise StateError("that Idempotency-Key was already used for a different request")
                return json.loads(row["result"]) | {"replayed": True}
            result = fn()
            c.execute("INSERT INTO payroll_requests (idem_key, action, request_hash, result, created_at) VALUES (?,?,?,?,?)",
                      (key, action, h, json.dumps(result, default=str), now_iso()))
        return result
