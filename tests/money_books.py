"""A business's payroll and money books set up through the REAL repository (Streams A, B and E), for the chat tests and
the money gold corpus: six staff with pay terms (two with app logins), bank and wallet accounts with their method
routes, last month's payroll approved (unpaid), an open staff advance, one open cash shortage on a delivery run, and
payment proofs uploaded by the owner and by the clerk. Everything goes through the repository's own writes -- nothing
here writes SQL.

setup(repo) returns the ids the corpus's placeholders name: EMP_RAFIQ ... EMP_NADEEM, ACC_HBL, ACC_JAZZ, RUN_ID,
ATT_OWNER1, ATT_OWNER2, ATT_CLERK1, SHORTAGE."""
from __future__ import annotations

import io
from datetime import timedelta

from munshi.domain.models import business_today, today_iso

# who is typing -> the registry user id their session would carry (the chat route passes principal.user_id)
USER_IDS = {"Sultan Ahmed": "U-OWNER", "Rafiq Ahmed": "U-RAFIQ", "Bilal Hussain": "U-BILAL", "Imran Khan": "U-IMRAN"}
STAFF = (  # name, role, phone, monthly basic, paid by, login
    ("Rafiq Ahmed", "driver", "0301-2000001", 42000, "jazzcash", "U-RAFIQ"),
    ("Bilal Hussain", "clerk", "0301-2000002", 45000, "bank", "U-BILAL"),
    ("Imran Khan", "salesman", "0301-2000003", 41000, "bank", "U-IMRAN"),
    ("Shafiq Masih", "loader", "0301-2000004", 40000, "easypaisa", ""),
    ("Kashif Iqbal", "loader", "0301-2000005", 40000, "easypaisa", ""),
    ("Nadeem Abbas", "helper", "0301-2000006", 40000, "jazzcash", ""),
)


def last_period() -> str:
    return (business_today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def _png(rgb: tuple[int, int, int]) -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (48, 32), rgb).save(b, "PNG")
    return b.getvalue()


def _shortage(repo, ctx: dict) -> str:
    """A delivery run whose driver handed in Rs 1,000 less than he collected: the cash-shortage entry a loss recovery names."""
    o = repo.create_order("C-003", [{"sku": "UREA-50", "qty": 2}], "chat", "fixture", "fixture")
    repo.confirm_order(o.order_id, "fixture", "fixture")
    repo.allocate_order(o.order_id, "WH-MULTAN", "fixture", "fixture")
    plan = repo.create_dispatch_plan(today_iso(), "R-MULTAN-N", "V-02", [o.order_id], "fixture")
    repo.approve_dispatch_plan(plan.plan_id, "fixture", "fixture")
    st = repo.list_stops(plan.plan_id)[0]
    total = repo.get_order(o.order_id).total
    repo.close_stop(st.stop_id, [{"sku": "UREA-50", "qty": 2}], [], total, repo.get_stop(st.stop_id).otp, "fixture")
    repo.record_deposit(plan.plan_id, total - 1000, "cashier", "fixture")
    rows = repo._all("SELECT expense_id FROM expenses WHERE category='cash_shortage' ORDER BY created_at DESC LIMIT 1")
    return rows[0]["expense_id"] if rows else ""


def setup(repo) -> dict:
    ctx: dict = {}
    today = business_today().isoformat()
    hbl = repo.add_money_account("bank", "HBL current", "HBL", "1234", 385000, None, actor="fixture", approved_by="owner")
    jz = repo.add_money_account("wallet", "JazzCash", "jazzcash", "", 12500, None, actor="fixture", approved_by="owner")
    ep = repo.add_money_account("wallet", "Easypaisa", "easypaisa", "", 8000, None, actor="fixture", approved_by="owner")
    for method, acc in (("bank", hbl), ("jazzcash", jz), ("easypaisa", ep)):
        repo.set_method_route(method, acc["account_id"], today, "fixture", "owner")
    ctx.update(ACC_HBL=hbl["account_id"], ACC_JAZZ=jz["account_id"], ACC_EASY=ep["account_id"])
    for name, role, phone, basic, method, login in STAFF:
        e = repo.add_employee({"name": name, "designation": role, "role_hint": role, "phone": phone, "pay_method": method,
                               "joined_on": "2026-01-01"}, "fixture", "owner")
        repo.set_pay_structure(e["employee_id"], "2026-01-01", "monthly", basic, actor="fixture", approved_by="owner")
        if login:
            repo.link_login(e["employee_id"], login, role, "fixture")
        ctx["EMP_" + name.split()[0].upper()] = e["employee_id"]
    prev = last_period()
    pv = repo.preview_payroll(prev)
    ctx["RUN_ID"] = repo.approve_payroll(prev, pv["fingerprint"], "fixture", "owner")["run"]["run_id"]
    repo.give_staff_advance(ctx["EMP_IMRAN"], 6000, "bank", installment=1000, actor="fixture", approved_by="owner")
    ctx["SHORTAGE"] = _shortage(repo, ctx)
    ctx["ATT_OWNER1"] = repo.store_attachment(_png((200, 10, 10)), "hbl.png", "image/png", "U-OWNER", "fixture")["att_id"]
    ctx["ATT_OWNER2"] = repo.store_attachment(_png((10, 200, 10)), "fauji.png", "image/png", "U-OWNER", "fixture")["att_id"]
    ctx["ATT_CLERK1"] = repo.store_attachment(_png((10, 10, 200)), "diesel.png", "image/png", "U-BILAL", "fixture")["att_id"]
    ctx["ATT_UNKNOWN"] = "ATT-0BADF00D"        # well-formed, in no business's books: what a spoofed or foreign id looks like here
    return ctx
