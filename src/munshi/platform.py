"""MunshiPlatform: one business's agents, one entry point for chat, approvals
and the daily close.

handle_message()  routes a message to a specialist and runs one turn; if the
                  specialist pauses on a gated tool, a PendingApproval is
                  persisted and returned instead of the action running.
resolve()         a human with the right role approves or rejects it; the
                  specialist resumes from its checkpoint (SQLite when the
                  platform has a checkpoint_path, so a restart loses nothing).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from munshi.agents.manager import CLARIFY, build_manager, classify
from munshi.agents.specialists import BUILDERS
from munshi.channels import build_channel, deliver_outbox
from munshi.domain.repository import MunshiRepository
from munshi.domain.seed import seeded_repository
from munshi.observability.tracing import TurnTrace, configure_tracking, trace_turn
from munshi.safety.risk import approver_for, risk_of, role_may_approve
from munshi.tools.core import MunshiTools

log = logging.getLogger("munshi.platform")


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
    requested_by: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def describe(self) -> str:
        a = self.args
        t = self.tool
        money = lambda v: f"Rs {float(v or 0):,.0f}"   # noqa: E731
        if t == "create_order":
            lines = ", ".join(f"{i['qty']} × {i['sku']}" for i in a.get("items", []))
            return f"Create order for {a.get('customer_id')}: {lines}"
        if t == "confirm_order": return f"Confirm order {a.get('order_id')}" + (" — over credit limit" if self.needs_role == "owner" else "")
        if t == "cancel_order": return f"Cancel order {a.get('order_id')} — {a.get('reason', '')[:40]}"
        if t == "allocate_order": return f"Allocate {a.get('order_id')} at {a.get('warehouse_id') or 'default godown'}"
        if t == "create_dispatch_plan": return f"Plan {a.get('route_id')} on {a.get('vehicle_id')} with {len(a.get('order_ids', []))} order(s)"
        if t == "approve_dispatch_plan": return f"Load and dispatch plan {a.get('plan_id')} — stock leaves the godown"
        if t == "adjust_stock": return f"Adjust {a.get('sku')} at {a.get('warehouse_id')} by {int(a.get('delta') or 0):+} ({a.get('reason', '')[:40]})"
        if t == "transfer_stock": return f"Move {a.get('qty')} × {a.get('sku')} from {a.get('from_warehouse')} to {a.get('to_warehouse')}"
        if t == "record_deposit": return f"Record deposit of {money(a.get('amount_counted'))} for {a.get('plan_id')}"
        if t == "record_payment": return f"Record {money(a.get('amount'))} received from {a.get('customer_id')} by {a.get('method', 'cash')}"
        if t == "record_expense": return f"Record expense {money(a.get('amount'))} — {a.get('category')} ({a.get('note', '')[:30]})"
        if t == "credit_note": return f"Credit note {money(a.get('amount'))} to {a.get('customer_id')} — {a.get('reason', '')[:40]}"
        if t == "record_purchase":
            lines = ", ".join(f"{i['qty']} × {i['sku']}" for i in a.get("items", []))
            return f"Receive from {a.get('supplier_id')}: {lines} into {a.get('warehouse_id') or 'default godown'}"
        if t == "pay_supplier": return f"Pay supplier {a.get('supplier_id')} {money(a.get('amount'))} by {a.get('method', 'cash')}"
        if t == "draft_reminder": return f"Draft {a.get('tier') or 'auto'} reminder for {a.get('customer_id')}"
        if t == "draft_due_reminders": return f"Draft reminders for everyone ≥{a.get('min_days_overdue')} days overdue"
        if t == "send_reminder": return f"Send reminder {a.get('reminder_id')} to the customer"
        if t == "log_promise": return f"Log promise: {a.get('customer_id')} pays {money(a.get('amount'))} by {a.get('promised_date')}"
        return f"{t}({a})"


@dataclass
class Reply:
    text: str
    specialist: str | None = None
    pending: PendingApproval | None = None
    thread_id: str | None = None


class MunshiPlatform:
    def __init__(self, repo: MunshiRepository | None = None, model: BaseChatModel | None = None, enable_tracing: bool = False,
                 checkpoint_path: str | None = None) -> None:
        self.repo = repo or seeded_repository()
        self.ops = MunshiTools(self.repo)
        self.enable_tracing = enable_tracing
        self.channel = build_channel()
        self._lock = threading.RLock()
        if enable_tracing:
            configure_tracking()
        self._ckpt_conn = None
        checkpointer = None
        if checkpoint_path:
            from langgraph.checkpoint.sqlite import SqliteSaver
            self._ckpt_conn = sqlite3.connect(checkpoint_path, check_same_thread=False)
            checkpointer = SqliteSaver(self._ckpt_conn)
            checkpointer.setup()
        self.specialists = {name: build(self.ops, self.repo, model, checkpointer) for name, build in BUILDERS.items()}
        self.manager = build_manager(model)

    def close(self) -> None:
        if self._ckpt_conn is not None:
            self._ckpt_conn.close()
        self.repo.close()

    # ------------------------------------------------------------------
    @property
    def pending(self) -> dict[str, PendingApproval]:
        return {a["approval_id"]: self._pa(a) for a in self.repo.pending_approvals()}

    @staticmethod
    def _pa(a: dict) -> PendingApproval:
        return PendingApproval(a["approval_id"], a["thread_id"], a["specialist"], a["tool"], a["args"], a["tier"], a["needs_role"],
                               a["requested_by_role"], a.get("requested_by") or "", a["created_at"])

    def _needs_role(self, tool: str, args: dict) -> str:
        """The registry's approver, escalated to the owner for an order above the business's big-order limit."""
        need = approver_for(tool)
        if tool == "confirm_order" and need == "clerk":
            try:
                if self.repo.over_credit(self.repo.get_order(args.get("order_id", ""))):
                    return "owner"
            except Exception:
                pass
        if tool == "create_order" and need == "clerk":
            try:
                limit = float(self.repo.setting("big_order_limit") or 0)
                total = sum(int(i["qty"]) * self.repo.get_product(i["sku"]).unit_price for i in args.get("items", []))
                if limit and total > limit:
                    return "owner"
            except Exception:
                pass
        return need

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

    def handle_message(self, thread_id: str, role: str, text: str, user: str = "") -> Reply:
        with self._lock:
            self.repo.set_current_user(user)
            self.repo.add_chat(thread_id, role, text, {"user": user})
            with self._trace("manager", role, text) as tr:
                specialist = classify(self.manager, text, role)
                tr.specialist = specialist
                if specialist is None:
                    self.repo.add_chat(thread_id, "munshi", CLARIFY, {"specialist": None})
                    return Reply(CLARIFY, None, None, thread_id)

                # A thread can only hold one pending action per specialist per role; a new message
                # while one waits is answered without touching the paused graph.
                if any(p["specialist"] == specialist and p["requested_by_role"] == role for p in self.repo.pending_approvals(thread_id)):
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
                                         risk_of(tool).value, self._needs_role(tool, req["args"]), role, user)
                    self.repo.save_approval(asdict(pa))
                    self.repo.notify(pa.needs_role, "approval", f"{bundle.title} wants to: {pa.describe()}" + (f" (asked by {user})" if user else ""), pa.approval_id)
                    tr.required_approval = True; tr.tool_called = tool
                    txt = f"{bundle.title} wants to: {pa.describe()}. Needs {pa.needs_role} approval."
                    self.repo.add_chat(thread_id, "munshi", txt, {"specialist": specialist, "approval_id": pa.approval_id})
                    return Reply(txt, specialist, pa, thread_id)

                txt = self._final_text(result)
                tr.response_text = txt
                self.repo.add_chat(thread_id, "munshi", txt, {"specialist": specialist})
                return Reply(txt, specialist, None, thread_id)

    def resolve(self, approval_id: str, approve: bool, role: str, note: str = "", user: str = "") -> Reply:
        with self._lock:
            self.repo.set_current_user(user)
            pa = self.pending.get(approval_id)
            if not pa:
                raise KeyError(f"no pending approval {approval_id}")
            if approve and not (role_may_approve(role, pa.tool) and (role == "owner" or pa.needs_role == "clerk")):
                raise PermissionError(f"{pa.tool} needs {pa.needs_role} approval; you are {role}")
            bundle = self.specialists[pa.specialist]
            decision = {"type": "approve"} if approve else {"type": "reject", "message": note or f"rejected by {role}"}
            with self._trace(f"{pa.specialist}.resume", role, f"{'approve' if approve else 'reject'} {pa.tool}") as tr:
                tr.approval_decision = "approve" if approve else "reject"
                # the approval is on record BEFORE the tool runs: the audit trail never shows a write ahead of its approval
                self.repo.resolve_approval(approval_id, approve, user or role, note)
                self.repo.audit(role, "approval_" + ("granted" if approve else "rejected"), "approval", approval_id,
                                {"tool": pa.tool, "args": pa.args, "specialist": pa.specialist, "note": note}, approved_by=role)
                try:
                    result = bundle.agent.invoke(Command(resume={"decisions": [decision]}), config=self._cfg(pa.thread_id, pa.requested_by_role, pa.specialist))
                except Exception as e:      # the graph state is gone (e.g. checkpoints wiped); the approval stays resolved but unexecuted
                    log.exception("resume failed for %s", approval_id)
                    txt = f"Couldn't resume that action ({type(e).__name__}); please ask the munshi again."
                    self.repo.add_chat(pa.thread_id, "munshi", txt, {"specialist": pa.specialist, "resolved": approval_id, "approved": approve, "error": True})
                    return Reply(txt, pa.specialist, None, pa.thread_id)
                txt = self._final_text(result)
                self.repo.add_chat(pa.thread_id, "munshi", txt, {"specialist": pa.specialist, "resolved": approval_id, "approved": approve})
                if approve: self.deliver_messages()
                return Reply(txt, pa.specialist, None, pa.thread_id)

    def list_pending(self, thread_id: str | None = None) -> list[dict]:
        items = [self._pa(a) for a in self.repo.pending_approvals(thread_id)]
        return [asdict(p) | {"summary": p.describe()} for p in sorted(items, key=lambda p: p.created_at)]

    def deliver_messages(self) -> dict:
        """Push the outbox through the configured channel (no-op without one)."""
        try:
            return deliver_outbox(self.repo, self.channel)
        except Exception as e:
            log.warning("outbox delivery failed: %s", e)
            return {"sent": 0, "failed": 0, "error": str(e)}
