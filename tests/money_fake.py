"""A stand-in for Streams A (payroll), B (finance) and E (payment proofs) while their repository methods are skeletons
that raise NotImplementedError. It implements EXACTLY the SEAMS signatures (domain/repository/{payroll,finance,
attachments}.py), keeps its state as one JSON blob in the business's own `settings` table (so the gold runner's
database snapshot resets it between records), writes the ONE audit row per gated call the seams demand, and returns
reports in the frozen table shape (domain/accounts.table). Chat (Stream D) is tested against it; nothing in src/
imports it.

install() patches the methods onto MunshiRepository and returns an undo function."""
from __future__ import annotations

import json
from datetime import timedelta

from munshi.domain import accounts as A
from munshi.domain.models import business_today, now_iso
from munshi.domain.repository import MunshiRepository, NotFoundError, StateError

KEY = "_fake_money_state"
PAY_FIELDS = ("basic", "daily_rate", "pay_method", "components", "net", "gross")


def _period(d=None) -> str:
    return (d or business_today()).strftime("%Y-%m")


def _last_period() -> str:
    first = business_today().replace(day=1)
    return _period(first - timedelta(days=1))


def run_id() -> str:
    return f"PAY-{_last_period()[:4]}-000001"


def initial_state() -> dict:
    emp = lambda i, name, des, phone, basic, method, user="": {  # noqa: E731
        "employee_id": i, "emp_no": "E-" + i[-3:], "name": name, "designation": des, "phone": phone, "status": "active", "user_id": user,
        "basic": basic, "pay_method": method, "aliases": []}
    emps = [emp("EMP-RAFIQ", "Rafiq Ahmed", "driver", "0301-2000001", 32000, "jazzcash", "U-RAFIQ"),
            emp("EMP-BILAL", "Bilal Hussain", "clerk", "0301-2000002", 45000, "bank", "U-BILAL"),
            emp("EMP-IMRAN", "Imran Khan", "salesman", "0301-2000003", 38000, "bank", "U-IMRAN"),
            emp("EMP-SHAFIQ", "Shafiq Masih", "loader", "0301-2000004", 26000, "easypaisa"),
            emp("EMP-KASHIF", "Kashif Iqbal", "loader", "0301-2000005", 26000, "easypaisa"),
            emp("EMP-NADEEM", "Nadeem Abbas", "helper", "0301-2000006", 24000, "jazzcash")]
    last = _last_period()
    return {"employees": {e["employee_id"]: e for e in emps},
            "runs": {run_id(): {"run_id": run_id(), "period": last, "status": "approved", "paid": {}}},
            "advances": [{"advance_id": "ADV-2026-000001", "employee_id": "EMP-IMRAN", "amount": 10000.0, "recovered": 4000.0, "status": "open"}],
            "adjustments": [], "attendance": {}, "transfers": [], "counts": [], "journal": [], "closes": [], "assets": [], "loans": [],
            "accounts": {"CASH": {"account_id": "CASH", "name": "Cash in hand (galla)", "kind": "cash", "balance": 46500.0},
                         "ACC-HBL": {"account_id": "ACC-HBL", "name": "HBL current", "kind": "bank", "provider": "HBL", "balance": 385000.0},
                         "ACC-JAZZ": {"account_id": "ACC-JAZZ", "name": "JazzCash", "kind": "wallet", "provider": "jazzcash", "balance": 12500.0}},
            "attachments": {"ATT-OWNER1": {"att_id": "ATT-OWNER1", "content_type": "image/jpeg", "filename": "hbl.jpg", "uploaded_by": "U-OWNER", "size_bytes": 81234},
                            "ATT-OTHER": {"att_id": "ATT-OTHER", "content_type": "image/png", "filename": "x.png", "uploaded_by": "U-SOMEONE", "size_bytes": 5120},
                            "ATT-READ25": {"att_id": "ATT-READ25", "content_type": "image/jpeg", "filename": "fauji.jpg", "uploaded_by": "U-OWNER",
                                           "size_bytes": 60001, "read_amount": 25000.0},
                            "ATT-CLERK1": {"att_id": "ATT-CLERK1", "content_type": "application/pdf", "filename": "diesel.pdf", "uploaded_by": "U-BILAL",
                                           "size_bytes": 20480}},
            "links": [], "seq": 1}


USER_IDS = {"Rafiq Ahmed": "U-RAFIQ", "Bilal Hussain": "U-BILAL", "Imran Khan": "U-IMRAN", "Sultan Ahmed": "U-OWNER"}


class FakeMoney:
    """Mixed onto MunshiRepository by install(); every method uses the repository's own settings table and audit."""

    # ------------------------------------------------------------------ state
    def _fm(self) -> dict:
        raw = self.setting(KEY, "")
        return json.loads(raw) if raw else initial_state()

    def _fm_save(self, st: dict) -> None:
        self.set_setting(KEY, json.dumps(st))

    def _fm_id(self, st: dict, prefix: str) -> str:
        st["seq"] = int(st.get("seq", 1)) + 1
        return f"{prefix}-{business_today().year}-{st['seq']:06d}"

    def _fm_emp(self, st: dict, employee_id: str) -> dict:
        e = st["employees"].get(str(employee_id or ""))
        if not e:
            raise NotFoundError(f"no such employee: {employee_id}")
        return e

    @staticmethod
    def _fm_public(e: dict, include_pay: bool) -> dict:
        return dict(e) if include_pay else {k: v for k, v in e.items() if k not in PAY_FIELDS}

    def _fm_audit(self, action: str, entity_id: str, payload: dict, actor: str, approved_by) -> None:
        self.audit(actor or "fake", action, action, entity_id, payload, approved_by=approved_by)

    # ------------------------------------------------------------------ employees
    def list_employees(self, status: str = "active", include_pay: bool = False) -> dict:
        st = self._fm()
        rows = [self._fm_public(e, include_pay) for e in st["employees"].values() if not status or e["status"] == status]
        cols = [A.col("emp_no", "Emp no"), A.col("name", "Name"), A.col("designation", "Designation"), A.col("status", "Status", badge=True)]
        if include_pay:
            cols.append(A.col("basic", "Basic", "money"))
        return {"employees": rows, "table": A.table("Employees", cols, rows)}

    def get_employee(self, employee_id: str, include_pay: bool = False) -> dict:
        return self._fm_public(self._fm_emp(self._fm(), employee_id), include_pay)

    def find_employee(self, text: str) -> dict:
        t = str(text or "").lower()
        hits = [self._fm_public(e, False) for e in self._fm()["employees"].values() if e["name"].split()[0].lower() in t]
        return {"match": hits[0] if len(hits) == 1 else None, "candidates": hits}

    def add_employee(self, data: dict, actor: str, approved_by: str | None = None) -> dict:
        st = self._fm()
        eid = f"EMP-{len(st['employees']) + 1:03d}"
        e = {"employee_id": eid, "emp_no": "E-" + eid[-3:], "status": "active", "user_id": "", "aliases": []} | dict(data)
        st["employees"][eid] = e
        self._fm_save(st)
        self._fm_audit("add_employee", eid, {"name": e.get("name")}, actor, approved_by)
        return self._fm_public(e, True)

    def update_employee(self, employee_id: str, changes: dict, actor: str, approved_by: str | None = None) -> dict:
        st = self._fm()
        e = self._fm_emp(st, employee_id)
        e.update(changes)
        self._fm_save(st)
        self._fm_audit("update_employee", employee_id, {"fields": sorted(changes)}, actor, approved_by)
        return self._fm_public(e, True)

    def rehire_employee(self, employee_id: str, rejoined_on: str, actor: str, approved_by: str | None = None) -> dict:
        st = self._fm()
        e = self._fm_emp(st, employee_id)
        e["status"] = "active"
        self._fm_save(st)
        self._fm_audit("rehire_employee", employee_id, {}, actor, approved_by)
        return self._fm_public(e, True)

    def set_pay_structure(self, employee_id, effective_from, pay_basis, basic=0.0, daily_rate=0.0, components=(), ot_eligible=True, actor="",
                          approved_by=None) -> dict:
        st = self._fm()
        e = self._fm_emp(st, employee_id)
        e.update(basic=float(basic), pay_basis=pay_basis)
        self._fm_save(st)
        self._fm_audit("set_pay_structure", employee_id, {}, actor, approved_by)
        return {"employee_id": employee_id, "basic": float(basic)}

    def set_commission_rule(self, employee_id, basis, rate_pct=0.0, per_unit=0.0, sku=None, min_basis=0.0, effective_from=None, actor="",
                            approved_by=None) -> dict:
        self._fm_audit("set_commission_rule", employee_id, {}, actor, approved_by)
        return {"rule_id": "COM-1", "employee_id": employee_id}

    def link_login(self, employee_id: str, user_id: str, role: str, actor: str) -> dict:
        return {"employee_id": employee_id, "user_id": user_id}

    def end_employment(self, employee_id: str, left_on: str, reason: str, actor: str, approved_by: str | None = None) -> dict:
        st = self._fm()
        e = self._fm_emp(st, employee_id)
        e["status"] = "left"
        self._fm_save(st)
        self._fm_audit("end_employment", employee_id, {"left_on": left_on}, actor, approved_by)
        return {"employee_id": employee_id, "name": e["name"], "left_on": left_on, "user_id": e.get("user_id", "")}

    def sync_employees_from_users(self, users: list[dict], actor: str) -> int:
        return 0

    # ------------------------------------------------------------------ the month
    def set_attendance(self, period: str, rows: list[dict], actor: str, approved_by: str | None = None) -> dict:
        st = self._fm()
        for r in rows:
            self._fm_emp(st, r.get("employee_id"))
            st["attendance"].setdefault(period, {})[r["employee_id"]] = {k: v for k, v in r.items() if k != "employee_id"}
        self._fm_save(st)
        self._fm_audit("record_attendance", period, {"rows": len(rows)}, actor, approved_by)
        return {"period": period, "rows": rows}

    def attendance(self, period: str) -> dict:
        st = self._fm()
        rows = [{"name": st["employees"][k]["name"], **v} for k, v in st["attendance"].get(period, {}).items()]
        return {"period": period, "rows": rows, "table": A.table("Attendance", [A.col("name", "Name"), A.col("days_worked", "Days", "days")], rows)}

    def add_payroll_adjustment(self, employee_id, period, code, amount, note, ref=None, taxable=True, actor="", approved_by=None) -> dict:
        st = self._fm()
        self._fm_emp(st, employee_id)
        adj = {"adj_id": f"ADJ-{len(st['adjustments']) + 1:03d}", "employee_id": employee_id, "period": period, "code": code, "amount": float(amount)}
        st["adjustments"].append(adj)
        self._fm_save(st)
        self._fm_audit("add_payroll_adjustment", adj["adj_id"], {"code": code}, actor, approved_by)
        return adj

    def void_payroll_adjustment(self, adj_id: str, actor: str, approved_by: str | None = None) -> dict:
        self._fm_audit("void_payroll_adjustment", adj_id, {}, actor, approved_by)
        return {"adj_id": adj_id, "status": "void"}

    def _fm_lines(self, st: dict, period: str, ids=None) -> list[dict]:
        out = []
        for e in st["employees"].values():
            if e["status"] != "active" or (ids and e["employee_id"] not in ids):
                continue
            adj = sum(a["amount"] * (-1 if a["code"] in ("loss_recovery", "fine") else 1) for a in st["adjustments"]
                      if a["employee_id"] == e["employee_id"] and a["period"] == period)
            gross = float(e.get("basic", 0)) + (3500.0 if e["designation"] == "salesman" else 0.0)
            out.append({"employee_id": e["employee_id"], "name": e["name"], "designation": e["designation"], "gross": gross,
                        "commission": 3500.0 if e["designation"] == "salesman" else 0.0, "net_pay": gross + adj, "method": e.get("pay_method", "bank")})
        return out

    def _fm_register_table(self, title: str, lines: list[dict]) -> dict:
        cols = [A.col("name", "Name"), A.col("designation", "Designation"), A.col("gross", "Gross", "money"),
                A.col("commission", "Commission", "money"), A.col("net_pay", "Net pay", "money")]
        tot = {"gross": sum(x["gross"] for x in lines), "net_pay": sum(x["net_pay"] for x in lines), "commission": sum(x["commission"] for x in lines)}
        return A.table(title, cols, lines, totals=tot)

    def preview_payroll(self, period: str, employee_ids: list[str] | None = None) -> dict:
        st = self._fm()
        lines = self._fm_lines(st, period, employee_ids)
        return {"period": period, "lines": lines, "warnings": [], "fingerprint": f"fp-{period}-{len(lines)}-{len(st['adjustments'])}",
                "total_net": sum(x["net_pay"] for x in lines), "table": self._fm_register_table(f"Payroll preview {period}", lines)}

    def approve_payroll(self, period: str, fingerprint: str, actor: str, approved_by: str | None, kind: str = "regular") -> dict:
        st = self._fm()
        if fingerprint != self.preview_payroll(period)["fingerprint"]:
            raise StateError("the payroll changed since the preview: preview it again")
        rid = self._fm_id(st, "PAY")
        st["runs"][rid] = {"run_id": rid, "period": period, "status": "approved", "paid": {}}
        self._fm_save(st)
        self._fm_audit("approve_payroll_run", rid, {"period": period}, actor, approved_by)
        return {"run_id": rid, "period": period, "status": "approved", "total_net": self.preview_payroll(period)["total_net"]}

    def reverse_payroll_run(self, run_id: str, reason: str, actor: str, approved_by: str | None) -> dict:
        self._fm_audit("reverse_payroll_run", run_id, {}, actor, approved_by)
        return {"run_id": run_id, "status": "reversed"}

    def payroll_register(self, run_id: str | None = None, period: str | None = None) -> dict:
        st = self._fm()
        runs = sorted(st["runs"].values(), key=lambda r: r["period"])
        r = st["runs"].get(run_id) if run_id else next((x for x in reversed(runs) if not period or x["period"] == period), None)
        if r is None:
            raise NotFoundError("no payroll run for that month")
        lines = self._fm_lines(st, r["period"])
        return {"run_id": r["run_id"], "period": r["period"], "lines": lines, "table": self._fm_register_table(f"Payroll register {r['period']}", lines)}

    def payslip(self, slip_id: str | None = None, employee_id: str | None = None, period: str | None = None) -> dict:
        st = self._fm()
        e = self._fm_emp(st, employee_id)
        line = next(x for x in self._fm_lines(st, period or _last_period()) if x["employee_id"] == employee_id) if e["status"] == "active" else None
        rows = [{"item": "Basic", "earnings": float(e["basic"])}, {"item": "Net remuneration", "earnings": line["net_pay"] if line else 0, "_em": True}]
        return {"slip_no": "PSL-2026-000009", "employee_id": employee_id, "name": e["name"], "period": period or _last_period(),
                "net": line["net_pay"] if line else 0.0,
                "table": A.table(f"Payslip {e['name']}", [A.col("item", "Item"), A.col("earnings", "Earnings", "money")], rows)}

    def my_payslips(self, user_id: str, limit: int = 12) -> dict:
        st = self._fm()
        e = next((x for x in st["employees"].values() if user_id and x.get("user_id") == user_id), None)
        if e is None:
            return {"slips": [], "employee": None, "table": None}
        slip = self.payslip(employee_id=e["employee_id"])
        return {"employee": {"name": e["name"]}, "slips": [{"slip_no": slip["slip_no"], "period": slip["period"], "net": slip["net"]}],
                "table": A.table("My payslips", [A.col("period", "Month"), A.col("net", "Net pay", "money")], [{"period": slip["period"], "net": slip["net"]}])}

    # ------------------------------------------------------------------ money out
    def _fm_plc(self, method: str) -> None:
        if self.payroll_profile() == A.PROFILE_PLC_2026 and method not in A.CASHLESS_METHODS:
            raise StateError("Punjab Labour Code 2026 limit: pay by bank, JazzCash, Easypaisa or cheque; the owner can switch payroll to the old law in Settings")

    def pay_salaries(self, run_id: str, payments: list[dict], actor: str, approved_by: str | None) -> dict:
        st = self._fm()
        r = st["runs"].get(run_id)
        if r is None:
            raise NotFoundError(f"no such payroll run: {run_id}")
        lines = {x["employee_id"]: x for x in self._fm_lines(st, r["period"])}
        out = []
        for p in payments:
            self._fm_plc(p.get("method") or "cash")
            ln = lines[p["employee_id"]]
            pid = self._fm_id(st, "SPM")
            out.append({"payment_id": pid, "employee_id": p["employee_id"], "name": ln["name"], "amount": float(p.get("amount") or ln["net_pay"]),
                        "method": p.get("method")})
            r["paid"][p["employee_id"]] = pid
        self._fm_save(st)
        self._fm_audit("pay_salaries", run_id, {"count": len(out)}, actor, approved_by)
        return {"run_id": run_id, "payments": out, "total": sum(x["amount"] for x in out)}

    def reverse_salary_payment(self, payment_id: str, reason: str, actor: str, approved_by: str | None) -> dict:
        self._fm_audit("reverse_salary_payment", payment_id, {}, actor, approved_by)
        return {"payment_id": payment_id, "status": "reversed"}

    def give_staff_advance(self, employee_id, amount, method, account_id=None, kind="advance", installment=0.0, start_period=None, note="",
                           actor="", approved_by=None) -> dict:
        st = self._fm()
        e = self._fm_emp(st, employee_id)
        self._fm_plc(method)
        aid = self._fm_id(st, "ADV")
        st["advances"].append({"advance_id": aid, "employee_id": employee_id, "amount": float(amount), "recovered": 0.0, "status": "open"})
        self._fm_save(st)
        self._fm_audit("give_staff_advance", aid, {"amount": float(amount)}, actor, approved_by)
        return {"advance_id": aid, "employee_id": employee_id, "name": e["name"], "amount": float(amount), "method": method}

    def repay_staff_advance(self, employee_id, amount, method, account_id=None, actor="", approved_by=None) -> dict:
        self._fm_audit("repay_staff_advance", employee_id, {}, actor, approved_by)
        return {"advance_id": "ADV-X", "employee_id": employee_id, "amount": float(amount)}

    def reverse_staff_advance(self, advance_id: str, reason: str, actor: str, approved_by: str | None) -> dict:
        self._fm_audit("reverse_staff_advance", advance_id, {}, actor, approved_by)
        return {"advance_id": advance_id, "status": "reversed"}

    def staff_advances_report(self, employee_id: str | None = None, status: str = "open") -> dict:
        st = self._fm()
        rows = [{"adv_no": a["advance_id"], "name": st["employees"][a["employee_id"]]["name"], "amount": a["amount"], "recovered": a["recovered"],
                 "outstanding": a["amount"] - a["recovered"]} for a in st["advances"] if (not employee_id or a["employee_id"] == employee_id)]
        cols = [A.col("adv_no", "Advance"), A.col("name", "Name"), A.col("amount", "Given", "money"), A.col("recovered", "Recovered", "money"),
                A.col("outstanding", "Outstanding", "money")]
        return {"advances": rows, "total_outstanding": sum(r["outstanding"] for r in rows),
                "table": A.table("Staff advances", cols, rows, totals={"outstanding": sum(r["outstanding"] for r in rows)})}

    # ------------------------------------------------------------------ statutory
    def statutory_summary(self, period: str, kind: str) -> dict:
        rows = [{"name": "Rafiq Ahmed", "total": 1850.0}, {"name": "Bilal Hussain", "total": 1850.0}]
        return {"period": period, "kind": kind, "total": 3700.0, "rules_verified_on": "2026-09-20", "source": "EOBI notification",
                "table": A.table(f"{kind.upper()} {period}", [A.col("name", "Name"), A.col("total", "Total", "money")], rows, totals={"total": 3700.0})}

    def record_statutory_payment(self, kind, period, amount, method, account_id, challan_ref, paid_on, actor, approved_by) -> dict:
        self._fm_audit("record_statutory_payment", period, {}, actor, approved_by)
        return {"payment_id": "STY-2026-000001", "kind": kind, "amount": float(amount)}

    def statutory_rate(self, key: str, jurisdiction: str = "pk", on: str | None = None) -> dict:
        return {"value": "37000", "effective_from": "2025-07-01", "source": "Punjab minimum wage notification", "verified_on": "2026-09-20", "grade": "B"}

    def add_statutory_rate(self, key, jurisdiction, value, effective_from, source, source_url, verified_on, grade, note, actor, approved_by) -> dict:
        self._fm_audit("add_statutory_rate", key, {}, actor, approved_by)
        return {"key": key, "value": value}

    def set_payroll_settings(self, changes: dict, actor: str, approved_by: str | None) -> dict:
        self._fm_audit("set_payroll_settings", "payroll", {"keys": sorted(changes)}, actor, approved_by)
        return dict(changes)

    # ------------------------------------------------------------------ finance: accounts
    def list_money_accounts(self, include_inactive: bool = False) -> dict:
        accs = list(self._fm()["accounts"].values())
        cols = [A.col("account", "Account"), A.col("kind", "Kind"), A.col("balance", "Balance", "money")]
        rows = [{"account_id": a["account_id"], "account": a["name"], "kind": a["kind"], "balance": a["balance"]} for a in accs]
        total = sum(a["balance"] for a in accs)
        return {"accounts": rows, "total": total, "table": A.table("Money accounts", cols, rows, totals={"balance": total})}

    def add_money_account(self, kind, name, provider="", number_last4="", opening_balance=0.0, opening_date=None, actor="", approved_by=None) -> dict:
        st = self._fm()
        aid = f"ACC-{len(st['accounts']) + 1:03d}"
        st["accounts"][aid] = {"account_id": aid, "name": name, "kind": kind, "provider": provider, "balance": float(opening_balance)}
        self._fm_save(st)
        self._fm_audit("add_money_account", aid, {}, actor, approved_by)
        return st["accounts"][aid]

    def set_method_route(self, method, account_id, effective_from, actor, approved_by) -> dict:
        self._fm_audit("set_method_route", method, {}, actor, approved_by)
        return {"method": method, "account_id": account_id}

    def account_balance_paisa(self, account_id: str, as_of: str | None = None) -> int:
        a = self._fm()["accounts"].get(account_id)
        if a is None:
            raise NotFoundError(f"no such account: {account_id}")
        return int(round(a["balance"] * 100))

    def account_book(self, account_id: str, start: str, end: str, redact_payroll: bool = False) -> dict:
        a = self._fm()["accounts"].get(account_id)
        if a is None:
            raise NotFoundError(f"no such account: {account_id}")
        rows = [{"date": start, "narration": "Opening", "money_in": 0.0, "money_out": 0.0, "balance": a["balance"] - 5000},
                {"date": end, "narration": A.PAYROLL_REDACTED_LABEL if redact_payroll else "Salary Rafiq Ahmed", "money_in": 0.0, "money_out": 0.0,
                 "balance": a["balance"] - 5000},
                {"date": end, "narration": "Rana Brothers", "money_in": 5000.0, "money_out": 0.0, "balance": a["balance"]}]
        cols = [A.col("date", "Date", "date"), A.col("narration", "Narration"), A.col("money_in", "In", "money"), A.col("money_out", "Out", "money"),
                A.col("balance", "Balance", "money")]
        return {"account_id": account_id, "name": a["name"], "closing": a["balance"], "table": A.table(f"{a['name']} book", cols, rows)}

    def transfer(self, from_account, to_account, amount, on_date=None, ref="", note="", actor="", approved_by=None) -> dict:
        st = self._fm()
        for x in (from_account, to_account):
            if x not in st["accounts"]:
                raise NotFoundError(f"no such account: {x}")
        st["accounts"][from_account]["balance"] -= float(amount)
        st["accounts"][to_account]["balance"] += float(amount)
        tid = self._fm_id(st, "XFR")
        self._fm_save(st)
        self._fm_audit("transfer_between_accounts", tid, {"amount": float(amount)}, actor, approved_by)
        return {"transfer_id": tid, "from_account": from_account, "to_account": to_account, "amount": float(amount)}

    def reverse_transfer(self, transfer_id, reason, actor, approved_by) -> dict:
        self._fm_audit("reverse_account_transfer", transfer_id, {}, actor, approved_by)
        return {"transfer_id": transfer_id, "status": "reversed"}

    def count_cash(self, account_id: str, counted: float, note: str = "", actor: str = "") -> dict:
        st = self._fm()
        book = st["accounts"][account_id]["balance"]
        cid = f"CC-{len(st['counts']) + 1:03d}"
        st["counts"].append({"count_id": cid, "account_id": account_id, "counted": float(counted), "book": book})
        self._fm_save(st)
        self._fm_audit("count_cash", cid, {}, actor, None)
        return {"count_id": cid, "account_id": account_id, "counted": float(counted), "book": book, "difference": float(counted) - book}

    def post_cash_difference(self, count_id, actor, approved_by) -> dict:
        self._fm_audit("post_cash_difference", count_id, {}, actor, approved_by)
        return {"count_id": count_id, "posted": True}

    def mark_cleared(self, account_id, items, cleared_on, cleared, actor) -> dict:
        self._fm_audit("mark_cleared", account_id, {}, actor, None)
        return {"account_id": account_id, "count": len(items)}

    def save_reconciliation(self, account_id, statement_date, statement_balance, actor) -> dict:
        self._fm_audit("save_reconciliation", account_id, {}, actor, None)
        return {"account_id": account_id, "difference": 0.0}

    def reconciliation(self, account_id, statement_date) -> dict:
        rows = [{"line": "Balance per books", "amount": 385000.0}, {"line": "Difference", "amount": 0.0, "_em": True}]
        return {"account_id": account_id, "difference": 0.0, "table": A.table("Reconciliation", [A.col("line", "Line"), A.col("amount", "Amount", "money")], rows)}

    # ------------------------------------------------------------------ journal
    def _fm_je(self, action: str, amount: float, actor: str, approved_by, **extra) -> dict:
        st = self._fm()
        jid = self._fm_id(st, "JV")
        self._fm_save(st)
        self._fm_audit(action, jid, {"amount": float(amount)}, actor, approved_by)
        return {"je_id": jid, "amount": float(amount)} | extra

    def record_capital(self, amount, method, account_id=None, on_date=None, note="", actor="", approved_by=None) -> dict:
        return self._fm_je("record_capital", amount, actor, approved_by, method=method)

    def record_drawing(self, amount, method, account_id=None, on_date=None, note="", actor="", approved_by=None) -> dict:
        return self._fm_je("record_drawing", amount, actor, approved_by, method=method)

    def add_loan(self, lender, kind, amount, method, account_id=None, on_date=None, terms="", actor="", approved_by=None) -> dict:
        return self._fm_je("record_loan", amount, actor, approved_by, loan_id="LN-0001", lender=lender)

    def repay_loan(self, loan_id, principal, interest=0.0, method="bank", account_id=None, on_date=None, actor="", approved_by=None) -> dict:
        return self._fm_je("repay_loan", principal, actor, approved_by, loan_id=loan_id)

    def add_fixed_asset(self, name, category, cost, acquired_on, life_months, salvage=0.0, funded_by="paid", method=None, account_id=None,
                        vehicle_id=None, actor="", approved_by=None) -> dict:
        return self._fm_je("add_fixed_asset", cost, actor, approved_by, asset_id="FA-0001", name=name)

    def dispose_fixed_asset(self, asset_id, on_date, proceeds, method, account_id=None, actor="", approved_by=None) -> dict:
        return self._fm_je("dispose_fixed_asset", proceeds, actor, approved_by, asset_id=asset_id)

    def run_depreciation(self, through_period, actor, approved_by) -> dict:
        return self._fm_je("run_depreciation", 12500.0, actor, approved_by, through_period=through_period)

    def post_journal(self, entry_date, kind, memo, lines, source=None, source_id=None, actor="", approved_by=None) -> dict:
        return self._fm_je("post_journal_entry", sum(float(x.get("debit") or 0) for x in lines), actor, approved_by)

    def reverse_journal(self, je_id, reason, actor, approved_by) -> dict:
        return self._fm_je("reverse_journal_entry", 0.0, actor, approved_by, reversed=je_id)

    def record_opening_balances(self, as_of, money, assets, loans, capital_label="Opening balance equity", actor="", approved_by=None) -> dict:
        return self._fm_je("record_opening_balances", 0.0, actor, approved_by)

    # ------------------------------------------------------------------ periods
    def period_status(self) -> dict:
        return {"closed_through": None, "table": A.table("Periods", [A.col("line", "Line")], [{"line": "Nothing closed yet"}])}

    def close_period(self, through_date, note="", force=False, actor="", approved_by=None) -> dict:
        self._fm_audit("close_period", through_date, {}, actor, approved_by)
        return {"close_id": 1, "through_date": through_date}

    def reopen_period(self, close_id, reason, actor, approved_by) -> dict:
        self._fm_audit("reopen_period", str(close_id), {}, actor, approved_by)
        return {"close_id": close_id, "status": "reopened"}

    # ------------------------------------------------------------------ reports
    def _fm_stmt(self, title: str, rows: list[dict], **raw) -> dict:
        return {"table": A.table(title, [A.col("line", "Line"), A.col("amount", "Amount", "money")], rows)} | raw

    def postings(self, start=None, end=None) -> list:
        return []

    def trial_balance(self, as_of=None) -> dict:
        return self._fm_stmt("Trial balance", [{"line": "Money accounts", "amount": 444000.0}], total_debit=444000.0, total_credit=444000.0)

    def general_journal(self, start, end) -> dict:
        return self._fm_stmt("General journal", [])

    def income_statement(self, start, end, compare=True) -> dict:
        rows = [{"line": "Net revenue", "amount": 1250000.0, "_em": True}, {"line": "Gross profit", "amount": 187500.0, "_em": True},
                {"line": "Staff costs", "amount": 191000.0}, {"line": "Net profit", "amount": -21000.0, "_em": True}]
        return self._fm_stmt("Income statement", rows, start=start, end=end, net_revenue=1250000.0, gross_profit=187500.0, net_profit=-21000.0)

    def balance_sheet(self, as_of=None) -> dict:
        rows = [{"line": "Total assets", "amount": 3120000.0, "_em": True}, {"line": "Total liabilities", "amount": 1450000.0, "_em": True},
                {"line": "Total equity", "amount": 1670000.0, "_em": True}, {"line": "Check", "amount": 0.0}]
        return self._fm_stmt("Balance sheet", rows, as_of=as_of or business_today().isoformat(), total_assets=3120000.0, total_liabilities=1450000.0,
                             total_equity=1670000.0, check=0.0)

    def cash_flow(self, start, end) -> dict:
        rows = [{"line": "Received from customers", "amount": 980000.0}, {"line": "Paid to suppliers", "amount": -720000.0},
                {"line": "Net change", "amount": 260000.0, "_em": True}]
        return self._fm_stmt("Cash flow", rows, start=start, end=end, net_change=260000.0, opening=184000.0, closing=444000.0)

    def owner_kpis(self, as_of=None) -> dict:
        rows = [{"kpi": "DSO", "value": 41.0, "unit": "days"}, {"kpi": "Gross margin", "value": 15.0, "unit": "%"}]
        return {"table": A.table("Owner KPIs", [A.col("kpi", "KPI"), A.col("value", "Value", "qty"), A.col("unit", "Unit")], rows), "dso": 41.0}

    def margins(self, by, start, end) -> dict:
        rows = [{"name": "Multan North", "revenue": 800000.0, "margin": 128000.0, "margin_pct": 16.0},
                {"name": "Vehari", "revenue": 450000.0, "margin": 59500.0, "margin_pct": 13.2}]
        cols = [A.col("name", "Name"), A.col("revenue", "Revenue", "money"), A.col("margin", "Margin", "money"), A.col("margin_pct", "Margin %", "pct")]
        return {"by": by, "rows": rows, "table": A.table(f"Margins by {by}", cols, rows)}

    def fixed_assets_register(self, as_of=None) -> dict:
        return self._fm_stmt("Fixed assets", [{"line": "Shehzore", "amount": 2400000.0}])

    def loans_report(self, as_of=None) -> dict:
        return self._fm_stmt("Loans", [{"line": "Haji sahab", "amount": 200000.0}])

    def verify_books(self, as_of=None) -> dict:
        return {"alarms": []}

    # ------------------------------------------------------------------ attachments (E)
    def store_attachment(self, data, filename, content_type, uploaded_by, actor) -> dict:
        st = self._fm()
        aid = f"ATT-{len(st['attachments']) + 1:03d}"
        st["attachments"][aid] = {"att_id": aid, "content_type": content_type, "filename": filename, "uploaded_by": uploaded_by, "size_bytes": len(data)}
        self._fm_save(st)
        return st["attachments"][aid]

    def get_attachment(self, att_id: str) -> dict:
        st = self._fm()
        a = st["attachments"].get(str(att_id or ""))
        if a is None:
            raise NotFoundError(f"no such attachment: {att_id}")
        return dict(a) | {"links": [x for x in st["links"] if x["att_id"] == att_id]}

    def attachment_bytes(self, att_id: str) -> bytes:
        return b""

    def link_attachment(self, att_id: str, entity: str, entity_id: str, actor: str) -> dict:
        st = self._fm()
        if att_id not in st["attachments"]:
            raise NotFoundError(f"no such attachment: {att_id}")
        link = {"att_id": att_id, "entity": entity, "entity_id": entity_id, "at": now_iso()}
        st["links"].append(link)
        self._fm_save(st)
        self.audit(actor, "attachment_linked", entity, entity_id, {"att_id": att_id})
        return link

    def attachments_for(self, entity: str, entity_id: str) -> list[dict]:
        st = self._fm()
        return [st["attachments"][x["att_id"]] for x in st["links"] if x["entity"] == entity and x["entity_id"] == entity_id]


def install() -> callable:
    """Patch FakeMoney's methods onto MunshiRepository (the class the platform builds); returns the undo."""
    saved = {}
    for name, fn in vars(FakeMoney).items():
        if callable(fn) and not name.startswith("__"):
            saved[name] = MunshiRepository.__dict__.get(name, _MISSING)
            setattr(MunshiRepository, name, fn)

    def undo() -> None:
        for name, old in saved.items():
            if old is _MISSING:
                delattr(MunshiRepository, name)
            else:
                setattr(MunshiRepository, name, old)
    return undo


_MISSING = object()
