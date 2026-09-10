"""A scripted business day, run through the real agents. Later steps reference
IDs earlier steps created (order → plan → stop → OTP), so this is a session,
not a bag of independent prompts."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Step:
    id: str
    role: str
    text: str
    expect_specialist: str | None
    expect_pending: bool
    approve: bool | None = None
    approve_as: str | None = None          # role that resolves; defaults to the requesting role
    expect_contains: list[str] = field(default_factory=list)
    capture: dict[str, str] = field(default_factory=dict)
    check_unchanged: str | None = None     # "stock:WH-MULTAN/UREA-50" or "outstanding:C-005"


STEPS: list[Step] = [
    Step("guest_stock_lookup", "clerk", "urea ka stock kitna hai", "godown", False, expect_contains=["WH-MULTAN"]),
    Step("driver_cannot_order", "driver", "Chaudhry Farms ko 20 urea bhej do", "order", False,
         expect_contains=["office"], check_unchanged="orders"),
    Step("clerk_creates_order", "clerk", "Chaudhry Farms ko 20 urea aur 5 dap bhej do", "order", True, approve=True,
         expect_contains=["ORD-", "draft"], capture={"oid": r"(ORD-[A-Z0-9]{8})"}),
    Step("reject_leaves_no_order", "clerk", "Rana Brothers ko 3 zinc bhej do", "order", True, approve=False,
         expect_contains=["rejected"], check_unchanged="orders_count"),
    Step("confirm_order", "clerk", "confirm {oid}", "order", True, approve=True, expect_contains=["confirmed"]),
    Step("allocate_order", "clerk", "allocate {oid} at WH-MULTAN", "godown", True, approve=True, expect_contains=["allocated"]),
    Step("suggest_dispatch", "clerk", "suggest dispatch for today", "godown", False, expect_contains=["R-MULTAN-N", "V-01"]),
    Step("create_plan", "clerk", "dispatch plan R-MULTAN-N V-01 {oid}", "godown", True, approve=True,
         expect_contains=["DSP-"], capture={"did": r"(DSP-[A-Z0-9]{8})"}),
    Step("approve_plan", "clerk", "approve {did}", "godown", True, approve=True, expect_contains=["approved"]),
    Step("driver_sees_stops", "driver", "stops for {did}", "delivery", False, expect_contains=["pending"],
         capture={"sid": r"(STP-[A-Z0-9]{8})"}),
    Step("driver_wrong_otp", "driver", "close {sid} delivered all cash 50000 otp 0000", "delivery", False,
         expect_contains=["OTP"], check_unchanged="outstanding:C-002"),
    Step("driver_closes_stop", "driver", "close {sid} delivered all cash 50000 otp {otp}", "delivery", False,
         expect_contains=["delivered", "108250"]),
    Step("clerk_records_deposit", "clerk", "{did} driver handed 45000", "hisaab", True, approve=True,
         expect_contains=["-5000", "C-002"]),
    Step("clerk_cannot_credit_note", "clerk", "credit note Rana Brothers 5000 damaged", "hisaab", False,
         expect_contains=["owner"], check_unchanged="outstanding:C-005"),
    Step("owner_credit_note", "owner", "credit note Rana Brothers 5000 damaged bags", "hisaab", True, approve=True,
         expect_contains=["credit_note", "-5000"]),
    Step("owner_credit_note_clerk_blocked", "owner", "credit note Haji Sons 1000 goodwill", "hisaab", True, approve=True,
         approve_as="clerk", expect_contains=["needs owner"], check_unchanged="outstanding:C-009"),
    Step("aging", "clerk", "who owes us and how overdue", "wasooli", False, expect_contains=["Haji Sons", "60+"]),
    Step("draft_reminders", "clerk", "remind everyone over 30 days", "wasooli", True, approve=True,
         expect_contains=["REM-", "final"]),
    Step("log_promise", "clerk", "Haji Sons promise 50000 by 2026-09-20", "wasooli", True, approve=True,
         expect_contains=["PRM-", "2026-09-20"]),
    Step("owner_restock_rejected", "owner", "restock WH-MULTAN 100 urea received", "godown", True, approve=False,
         expect_contains=["rejected"], check_unchanged="stock:WH-MULTAN/UREA-50"),
    # ---- v1.0: purchases, office money, reports, salesman, transfers, cancellations
    Step("clerk_receives_stock", "clerk", "received 100 urea from Fauji at 3600 bill FF-2291", "khareed", True, approve=True,
         expect_contains=["PUR-", "900000"]),
    Step("clerk_cannot_pay_supplier", "clerk", "pay Fauji 100000 by bank", "khareed", False, expect_contains=["owner"]),
    Step("owner_pays_supplier", "owner", "pay Fauji 100000 by bank", "khareed", True, approve=True, expect_contains=["SPY-", "800000"]),
    Step("payables", "clerk", "what do we owe suppliers", "khareed", False, expect_contains=["Fauji"]),
    Step("office_payment", "clerk", "Chaudhry Farms paid 20000 jazzcash", "hisaab", True, approve=True, expect_contains=["RCP-", "jazzcash"]),
    Step("expense", "clerk", "expense diesel 5000 for V-01", "hisaab", True, approve=True, expect_contains=["EXP-", "fuel"]),
    Step("cashbook", "clerk", "aaj ka cashbook", "hisaab", False, expect_contains=["cash_in"]),
    Step("profit_report", "owner", "profit this month", "report", False, expect_contains=["gross_margin", "net"]),
    Step("slow_stock", "owner", "slow stock 30 days", "report", False, expect_contains=["days_without_sale"]),
    Step("salesman_books_draft", "salesman", "Rana Brothers ko 5 dap bhej do", "order", True, approve=True, approve_as="clerk",
         expect_contains=["ORD-", "draft"], capture={"oid2": r"(ORD-[A-Z0-9]{8})"}),
    Step("salesman_cannot_confirm", "salesman", "confirm {oid2}", "order", False, expect_contains=["office"]),
    Step("salesman_cannot_approve", "salesman", "Haji Sons promise 25000 by 2026-09-25", "wasooli", True, approve=True, approve_as="salesman",
         expect_contains=["needs owner"]),
    Step("salesman_no_reports", "salesman", "profit this month", "report", False, expect_contains=["office"]),
    Step("driver_no_purchases", "driver", "received 10 urea from Engro", "khareed", False, expect_contains=["office"]),
    Step("transfer_stock", "clerk", "transfer 20 urea WH-MULTAN WH-VEHARI", "godown", True, approve=True, expect_contains=["TRF-"]),
    Step("cancel_order", "clerk", "cancel {oid2} customer changed mind", "order", True, approve=True, expect_contains=["cancelled"]),
    Step("cancel_unknown_order", "clerk", "cancel ORD-ZZZZZZZZ", "order", True, approve=True, expect_contains=["no such order"], check_unchanged="orders_count"),
    Step("broken_promises", "clerk", "broken promises", "wasooli", False, expect_contains=["Done"]),
    Step("gibberish", "clerk", "asdkj qwoeiru zzz", None, False, expect_contains=["order, the godown"]),
]
