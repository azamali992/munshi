"""MunshiPlatform: one entry point for chat, approvals and the daily close.

handle_message()  routes a message to a specialist and runs one turn; if the
                  specialist pauses on a gated tool, a PendingApproval is
                  returned and stored instead of the action running.
resolve()         a human with the right role approves or rejects it; the
                  specialist resumes from its checkpoint.
"""
from __future__ import annotations

import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from munshi.agents.manager import build_manager, classify
from munshi.agents.specialists import (build_delivery_munshi, build_godown_munshi, build_hisaab_munshi,
                                       build_order_munshi, build_wasooli_munshi)
from munshi.domain.repository import MunshiRepository
from munshi.domain.seed import seeded_repository
from munshi.observability.tracing import TurnTrace, configure_tracking, trace_turn
from munshi.safety.risk import approver_for, risk_of, role_may_approve
from munshi.tools.core import MunshiTools

CLARIFY = "Is this about an order, the godown, a delivery, cash/khata, or collections?"


@dataclass
class PendingApproval:
    approval_id: str
    thread_id: str
    specialist: str
    tool: str
    args: dict
    tier: str
    needs_role: str
    requested_by_role: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def describe(self) -> str:
        a = self.args
        t = self.tool
        if t == "create_order":
            lines = ", ".join(f"{i['qty']} × {i['sku']}" for i in a.get("items", []))
            return f"Create order for {a.get('customer_id')}: {lines}"
        if t == "confirm_order": return f"Confirm order {a.get('order_id')}"
        if t == "allocate_order": return f"Allocate {a.get('order_id')} at {a.get('warehouse_id')}"
        if t == "create_dispatch_plan": return f"Plan {a.get('route_id')} on {a.get('vehicle_id')} with {len(a.get('order_ids', []))} order(s)"
        if t == "approve_dispatch_plan": return f"Load and dispatch plan {a.get('plan_id')} — stock leaves the godown"
        if t == "adjust_stock": return f"Adjust {a.get('sku')} at {a.get('warehouse_id')} by {a.get('delta'):+} ({a.get('reason','')[:40]})"
        if t == "record_deposit": return f"Record deposit of Rs {a.get('amount_counted'):,.0f} for {a.get('plan_id')}"
        if t == "credit_note": return f"Credit note Rs {a.get('amount'):,.0f} to {a.get('customer_id')} — {a.get('reason','')[:40]}"
        if t == "draft_reminder": return f"Draft {a.get('tier') or 'auto'} reminder for {a.get('customer_id')}"
        if t == "draft_due_reminders": return f"Draft reminders for everyone ≥{a.get('min_days_overdue')} days overdue"
        if t == "send_reminder": return f"Send reminder {a.get('reminder_id')} to the customer"
        if t == "log_promise": return f"Log promise: {a.get('customer_id')} pays Rs {a.get('amount'):,.0f} by {a.get('promised_date')}"
        return f"{t}({a})"


@dataclass
class Reply:
    text: str
    specialist: Optional[str] = None
    pending: Optional[PendingApproval] = None
    thread_id: Optional[str] = None


class MunshiPlatform:
    def __init__(self, repo: MunshiRepository | None = None, model: BaseChatModel | None = None, enable_tracing: bool = False) -> None:
        self.repo = repo or seeded_repository()
        self.ops = MunshiTools(self.repo)
        self.enable_tracing = enable_tracing
        if enable_tracing:
            configure_tracking()
        self.specialists = {
            "order": build_order_munshi(self.ops, self.repo, model),
            "godown": build_godown_munshi(self.ops, self.repo, model),
            "delivery": build_delivery_munshi(self.ops, self.repo, model),
            "hisaab": build_hisaab_munshi(self.ops, self.repo, model),
            "wasooli": build_wasooli_munshi(self.ops, self.repo, model),
        }
        self.manager = build_manager(model)
        self.pending: dict[str, PendingApproval] = {}

    # ------------------------------------------------------------------
    def _cfg(self, thread_id: str, role: str, specialist: str) -> dict:
        # Role is part of the checkpoint key: a driver and a clerk on the same
        # conversation never share a specialist's memory or its pending action.
        return {"configurable": {"thread_id": f"{thread_id}:{role}:{specialist}"}}

    def _trace(self, agent: str, role: str, text: str):
        return trace_turn(agent, role, text) if self.enable_tracing else nullcontext(TurnTrace(agent_name=agent, role=role, user_text=text))

    def _final_text(self, result: dict) -> str:
        msg = result["messages"][-1]
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        return content or "Done."

    def handle_message(self, thread_id: str, role: str, text: str) -> Reply:
        self.repo.add_chat(thread_id, role, text)
        with self._trace("manager", role, text) as tr:
            specialist = classify(self.manager, text, role)
            tr.specialist = specialist
            if specialist is None:
                self.repo.add_chat(thread_id, "munshi", CLARIFY, {"specialist": None})
                return Reply(CLARIFY, None, None, thread_id)

            # A thread can only hold one pending action per specialist; a new message
            # while one waits is answered without touching the paused graph.
            if any(p.thread_id == thread_id and p.specialist == specialist and p.requested_by_role == role for p in self.pending.values()):
                txt = "There's an action from this munshi waiting for approval — approve or reject it first."
                self.repo.add_chat(thread_id, "munshi", txt, {"specialist": specialist})
                return Reply(txt, specialist, None, thread_id)

            bundle = self.specialists[specialist]
            result = bundle.agent.invoke({"messages": [HumanMessage(text)], "role": role}, config=self._cfg(thread_id, role, specialist))
            interrupts = result.get("__interrupt__")
            if interrupts:
                req = interrupts[0].value["action_requests"][0]
                tool = req["name"]
                pa = PendingApproval(uuid.uuid4().hex[:10].upper(), thread_id, specialist, tool, req["args"],
                                     risk_of(tool).value, approver_for(tool), role)
                self.pending[pa.approval_id] = pa
                tr.required_approval = True; tr.tool_called = tool
                txt = f"{bundle.title} wants to: {pa.describe()}. Needs {pa.needs_role} approval."
                self.repo.add_chat(thread_id, "munshi", txt, {"specialist": specialist, "approval_id": pa.approval_id})
                return Reply(txt, specialist, pa, thread_id)

            txt = self._final_text(result)
            tr.response_text = txt
            self.repo.add_chat(thread_id, "munshi", txt, {"specialist": specialist})
            return Reply(txt, specialist, None, thread_id)

    def resolve(self, approval_id: str, approve: bool, role: str, note: str = "") -> Reply:
        pa = self.pending.get(approval_id)
        if not pa:
            raise KeyError(f"no pending approval {approval_id}")
        if approve and not role_may_approve(role, pa.tool):
            raise PermissionError(f"{pa.tool} needs {pa.needs_role} approval; you are {role}")
        bundle = self.specialists[pa.specialist]
        decision = {"type": "approve"} if approve else {"type": "reject", "message": note or f"rejected by {role}"}
        with self._trace(f"{pa.specialist}.resume", role, f"{'approve' if approve else 'reject'} {pa.tool}") as tr:
            tr.approval_decision = "approve" if approve else "reject"
            result = bundle.agent.invoke(Command(resume={"decisions": [decision]}), config=self._cfg(pa.thread_id, pa.requested_by_role, pa.specialist))
            del self.pending[approval_id]
            txt = self._final_text(result)
            self.repo.audit(role, "approval_" + ("granted" if approve else "rejected"), "approval", approval_id,
                            {"tool": pa.tool, "args": pa.args, "specialist": pa.specialist}, approved_by=role)
            self.repo.add_chat(pa.thread_id, "munshi", txt, {"specialist": pa.specialist, "resolved": approval_id, "approved": approve})
            return Reply(txt, pa.specialist, None, pa.thread_id)

    def list_pending(self, thread_id: str | None = None) -> list[dict]:
        items = [p for p in self.pending.values() if thread_id is None or p.thread_id == thread_id]
        return [asdict(p) | {"summary": p.describe()} for p in sorted(items, key=lambda p: p.created_at)]
