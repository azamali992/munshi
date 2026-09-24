"""MunshiPlatform: one business's agents, one entry point for chat, approvals
and the daily close.

handle_message()  routes a message to a specialist and runs one turn; if the
                  specialist pauses on a gated tool, a PendingApproval is
                  persisted and returned instead of the action running.
resolve()         a human with the right role -- and, unless they are the owner,
                  not the person who asked for it -- approves or rejects it;
                  the specialist resumes from its checkpoint (SQLite when the
                  platform has a checkpoint_path, so a restart loses nothing).
                  If the resumed turn asks for another gated action, that
                  becomes a new card rather than a silent "Done.".

One gated call per step: see safety.middleware.OneGatedCallPerStep. A card is
never raised for a call whose record reference is blank.
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
from munshi.safety.middleware import DEFERRED_KEY
from munshi.safety.risk import approval_refusal, approver_for, risk_of, stricter_role
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
        # Every write this turn makes -- including any tool the agent runs on a worker thread -- is `user`'s.
        with self._lock, self.repo.acting_as(user):
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
                cfg = self._cfg(thread_id, role, specialist)
                self._clear_orphaned_interrupt(bundle, cfg)
                result = bundle.agent.invoke({"messages": [HumanMessage(text)], "role": role}, config=cfg)
                reply = self._settle(bundle, specialist, thread_id, role, user, result, tr)
                self.repo.add_chat(thread_id, "munshi", reply.text, {"specialist": specialist} | ({"approval_id": reply.pending.approval_id} if reply.pending else {}))
                return reply

    # ------------------------------------------------------------------ pausing on a gated call
    _MAX_DECLINES = 3

    def _settle(self, bundle, specialist: str, thread_id: str, role: str, user: str, result: dict, tr, lead: str = "") -> Reply:
        """Turn a specialist run into a Reply. If the run paused on a gated call,
        persist exactly one approval card for it -- unless the call references
        something that doesn't exist (or the pause holds more than one action,
        which OneGatedCallPerStep prevents), in which case the call is declined
        back to the agent so the thread is never left hanging on an unanswered
        tool call, and the human is told plainly that it was not done.
        `lead` is set when settling a resumed turn ("Approved. "): whatever
        follows is a follow-up to an action that has already been decided."""
        problems: list[str] = []
        for _ in range(self._MAX_DECLINES + 1):
            interrupts = result.get("__interrupt__")
            if not interrupts:
                break
            value = interrupts[0].value
            reqs = value.get("action_requests", [])
            problem = "only one action needing approval can be asked for at a time" if len(reqs) != 1 else self._unresolvable(reqs[0]["name"], reqs[0]["args"])
            if problem is None:
                return self._open_card(bundle, specialist, thread_id, role, user, reqs[0], value.get(DEFERRED_KEY, []), problems, tr, lead)
            log.warning("declined gated call on %s/%s: %s", thread_id, specialist, problem)
            problems.append(problem)
            if len(problems) > self._MAX_DECLINES:
                break
            result = bundle.agent.invoke(Command(resume={"decisions": [{"type": "reject", "message": f"Not asked for: {problem}."}] * max(len(reqs), 1)}),
                                         config=self._cfg(thread_id, role, specialist))
        if problems and lead:
            txt = f"{lead}A follow-up action couldn't be asked for — {problems[0]}. That follow-up was not done."
        elif problems:
            txt = f"Couldn't ask for approval — {problems[0]}. Nothing was done."
        else:
            txt = self._final_text(result)
        tr.response_text = txt
        return Reply(txt, specialist, None, thread_id)

    def _open_card(self, bundle, specialist: str, thread_id: str, role: str, user: str, req: dict, deferred: list[dict], problems: list[str], tr,
                   lead: str = "") -> Reply:
        tool = req["name"]
        pa = PendingApproval(uuid.uuid4().hex[:10].upper(), thread_id, specialist, tool, req["args"],
                             risk_of(tool).value, self._needs_role(tool, req["args"]), role, user)
        self.repo.save_approval(asdict(pa))
        later = ""
        if deferred:
            later = (" Only one action needing approval can be asked for at a time — not asked for yet: "
                     + "; ".join(self._summary(d.get("name", "?"), d.get("args") or {}) for d in deferred)
                     + ". Ask again for it once this one is decided.")
        self.repo.notify(pa.needs_role, "approval", f"{bundle.title} wants to: {pa.describe()}" + (f" (asked by {user})" if user else ""), pa.approval_id)
        tr.required_approval = True; tr.tool_called = tool
        skipped = f"(Skipped: {problems[0]}.) " if problems else ""
        txt = f"{lead}{skipped}{bundle.title} {'next ' if lead else ''}wants to: {pa.describe()}. Needs {pa.needs_role} approval.{later}"
        return Reply(txt, specialist, pa, thread_id)

    @staticmethod
    def _summary(tool: str, args: dict) -> str:
        try:
            return PendingApproval("", "", "", tool, args, "", "clerk", "").describe()
        except Exception:
            return f"{tool}({args})"

    # Required arguments that name a record. Every gated tool that takes one of these requires it.
    _REFS = {"order_id": "order", "customer_id": "customer", "plan_id": "dispatch plan", "supplier_id": "supplier", "reminder_id": "reminder"}

    def _unresolvable(self, tool: str, args: dict) -> str | None:
        """Why no card should be raised for this call, or None. A card must never
        read "Confirm order ." -- a required reference that is blank means the
        model didn't find one, so there is nothing a human could approve.
        (A well-formed id that doesn't exist still gets a card and fails safely
        on approve with "no such ..."; eval step cancel_unknown_order pins that.)"""
        refs = [(k, v) for k, v in args.items() if k in self._REFS] + [("order_id", v) for v in (args.get("order_ids") or [])]
        for key, raw in refs:
            if not str(raw or "").strip():
                return f"no {self._REFS[key]} was named"
        return None

    def _clear_orphaned_interrupt(self, bundle, cfg: dict) -> None:
        """A paused graph with no card behind it (e.g. a thread wedged before this
        fix, or a resume that crashed) would carry an unanswered tool call into
        the next model request, which provider APIs refuse. Decline it first."""
        try:
            state = bundle.agent.get_state(cfg)
        except Exception:
            return
        for _ in range(self._MAX_DECLINES):
            if not state.interrupts:
                return
            n = max(len(state.interrupts[0].value.get("action_requests", [])), 1)
            log.warning("declining orphaned interrupt on %s", cfg["configurable"]["thread_id"])
            try:
                bundle.agent.invoke(Command(resume={"decisions": [{"type": "reject", "message": "Superseded: the user sent a new message instead."}] * n}), config=cfg)
                state = bundle.agent.get_state(cfg)
            except Exception:
                log.exception("couldn't clear orphaned interrupt")
                return

    def resolve(self, approval_id: str, approve: bool, role: str, note: str = "", user: str = "") -> Reply:
        # The decision is the approver's act; the action it releases is the requester's. So the approval
        # record is written as `user`, and the resumed turn runs as `pa.requested_by` with the approver
        # alongside (audit approved_by) -- never as the approver, or the requester would vanish from
        # created_by / the audit trail and could later clear their own request on the direct routes.
        with self._lock, self.repo.acting_as(user):
            pa = self.pending.get(approval_id)
            if not pa:
                raise KeyError(f"no pending approval {approval_id}")
            if approve:
                # re-check at decision time: the requirement can only get stricter (e.g. the order went over limit meanwhile)
                need = stricter_role(pa.needs_role, self._needs_role(pa.tool, pa.args))
                why = approval_refusal(role, pa.tool, need, approver=user, requester=pa.requested_by)
                if why:
                    raise PermissionError(why)
            bundle = self.specialists[pa.specialist]
            decision = {"type": "approve"} if approve else {"type": "reject", "message": note or f"rejected by {role}"}
            with self._trace(f"{pa.specialist}.resume", role, f"{'approve' if approve else 'reject'} {pa.tool}") as tr:
                tr.approval_decision = "approve" if approve else "reject"
                # the approval is on record BEFORE the tool runs: the audit trail never shows a write ahead of its approval
                self.repo.resolve_approval(approval_id, approve, user or role, note)
                self.repo.audit(role, "approval_" + ("granted" if approve else "rejected"), "approval", approval_id,
                                {"tool": pa.tool, "args": pa.args, "specialist": pa.specialist, "note": note}, approved_by=role)
                signature = (f"{role}:{user}" if user.strip() else role) if approve else ""
                try:
                    with self.repo.acting_as(pa.requested_by, approved_by=signature):
                        result = bundle.agent.invoke(Command(resume={"decisions": [decision]}), config=self._cfg(pa.thread_id, pa.requested_by_role, pa.specialist))
                except Exception as e:      # the graph state is gone (e.g. checkpoints wiped); the approval stays resolved but unexecuted
                    log.exception("resume failed for %s", approval_id)
                    txt = f"Couldn't resume that action ({type(e).__name__}); please ask the munshi again."
                    self.repo.add_chat(pa.thread_id, "munshi", txt, {"specialist": pa.specialist, "resolved": approval_id, "approved": approve, "error": True})
                    return Reply(txt, pa.specialist, None, pa.thread_id)
                # The resumed turn may go on to ask for another gated action ("confirm A, then B"): that gets its
                # own card, on the requester's behalf, instead of a bare "Done." over a paused graph. Anything the
                # agent does while settling is the requester's too, but no longer covered by this approval.
                with self.repo.acting_as(pa.requested_by):
                    reply = self._settle(bundle, pa.specialist, pa.thread_id, pa.requested_by_role, pa.requested_by, result, tr,
                                         lead="Approved. " if approve else "Rejected. ")
                meta = {"specialist": pa.specialist, "resolved": approval_id, "approved": approve}
                self.repo.add_chat(pa.thread_id, "munshi", reply.text, meta | ({"approval_id": reply.pending.approval_id} if reply.pending else {}))
                if approve: self.deliver_messages()
                return reply

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
