#!/usr/bin/env python3
"""A narrated business day through the Munshi agent team, fully offline.

    PYTHONPATH=src python3 demo/run_demo.py
"""
from __future__ import annotations
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from munshi.platform import MunshiPlatform

p = MunshiPlatform()
W = 78

def say(role, text, approve=None, as_role=None):
    print(f"\n[{role}] {text}")
    r = p.handle_message("demo", role, text)
    print(f"  → {r.specialist or 'manager'}: {r.text[:200]}")
    if r.pending and approve is not None:
        try:
            a = p.resolve(r.pending.approval_id, approve, as_role or role)
            print(f"  ✓ {'approved' if approve else 'rejected'} by {as_role or role}: {a.text[:200]}")
            return a.text
        except PermissionError as e:
            print(f"  ✗ blocked: {e}")
    return r.text

print("=" * W); print("MUNSHI — a day at Sultan Traders, run by five munshis and a manager"); print("=" * W)
print("\n— Morning: orders come in over WhatsApp, the clerk relays them —")
t = say("clerk", "Chaudhry Farms ko 20 urea aur 5 dap bhej do", approve=True)
oid = re.search(r"ORD-[A-Z0-9]+", t).group(0)
say("driver", "Rana Brothers ko 3 zinc bhej do")                       # drivers can't order
say("clerk", f"confirm {oid}", approve=True)
say("clerk", f"allocate {oid} at WH-MULTAN", approve=True)

print("\n— Godown plans the run —")
say("clerk", "suggest dispatch for today")
t = say("clerk", f"dispatch plan R-MULTAN-N V-01 {oid}", approve=True)
did = re.search(r"DSP-[A-Z0-9]+", t).group(0)
say("clerk", f"approve {did}", approve=True)

print("\n— On the road: the driver closes the stop with the customer's OTP —")
stop = p.repo.list_stops(did)[0]
say("driver", f"close {stop.stop_id} delivered all cash 50000 otp 0000")   # wrong OTP
say("driver", f"close {stop.stop_id} delivered all cash 50000 otp {stop.otp}")

print("\n— Evening: cash comes back short, Hisaab says where to look —")
say("clerk", f"{did} driver handed 45000", approve=True)

print("\n— Collections: Wasooli drafts templated reminders, nothing free-text —")
say("clerk", "who owes us and how overdue")
say("clerk", "remind everyone over 30 days", approve=True)
say("clerk", "Haji Sons promise 50000 by 2026-09-20", approve=True)

print("\n— Money and stock need the owner —")
say("clerk", "credit note Rana Brothers 5000 damaged bags")            # clerk can't even ask
say("owner", "credit note Rana Brothers 5000 damaged bags", approve=True, as_role="clerk")   # clerk can't approve
say("owner", "restock WH-MULTAN 100 urea received", approve=False)      # owner rejects

d = p.repo.digest()
print("\n" + "=" * W)
print(f"Digest {d['date']}: orders {d['orders']['count']} (Rs {d['orders']['value']:,.0f}) · delivered {d['dispatch']['delivered']}/{d['dispatch']['stops']}"
      f" · cash collected Rs {d['cash']['collected']:,.0f} / deposited Rs {d['cash']['deposited']:,.0f}"
      f" · receivables Rs {d['receivables']['total']:,.0f} · reminders drafted {d['pending_reminders']}")
print(f"Audit rows: {len(p.repo.audit_log(1000))} — every write names its actor and, where gated, its approver.")
print("=" * W)
