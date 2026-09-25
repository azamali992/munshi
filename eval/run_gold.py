#!/usr/bin/env python3
"""Scores a labelled message corpus (eval/gold_corpus.jsonl, eval/gold_holdout.jsonl)
through MunshiPlatform -- the offline stub by default, any LLM_PROVIDER works --
and reports what matters for a chat that proposes approval cards:

  routing            the manager picked an acceptable munshi (or none / the help desk for chatter)
  entity             right customer / supplier on act messages that name one
  sku_qty            right SKUs and quantities on act messages that carry items or a sku
  amount             right rupee amount on act messages that carry one
  clarify_correct    on an ambiguous / incomplete / out-of-catalogue message: no entity committed, no write proposed
  refuse_correct     the role lacks the capability: nothing proposed or executed
  safe_failure       of the messages it got wrong, the share that did NOT produce a wrong card or write
  WRONG-CARD rate    approval cards (or executed writes) whose tool or arguments differ from gold, over ALL messages.
                     A busy clerk taps Approve; this is the number that has to stay near zero.

Every record runs on a fresh in-memory platform (so records are independent) with
fixtures: two extra customers (C-011 Chaudhry Traders, C-012 Malik Seeds) so 'Chaudhry'
and 'Malik' are genuinely ambiguous, a draft order, a confirmed order, a loaded plan
with one stop (and its OTP), and a planned-but-unloaded plan. Placeholders in the
corpus ({ORD_DRAFT} {ORD_CONF} {DSP} {DSP_PLANNED} {STP} {OTP} {LAST_ORD}
{NEXT_FRIDAY}) are filled from those fixtures.

Corpus schema (one JSON object per line): id, role, text, lang, tags[], context[]
(prior turns on the same thread: {role, text, approve}), expect{specialist
(str | list | null), action (act | clarify | refuse), tool (str | list | null),
args{subset that must match}, reply_has[] / reply_lacks[] (optional: case-insensitive
substrings the visible reply must / must not contain -- the visible reply is the text
before the machine-readable "Done -- {...}" details block)}, note.

Context turns are replayed on the same thread, in order, so a record whose context ends in
the munshi's own question ("How much was it?") scores the ANSWER to that question: the
open-question memory is exercised exactly as a user would.

Nothing is written inside the repo: the report goes to --out (default: the system
temp dir). Usage:
    python eval/run_gold.py                       # both corpora
    python eval/run_gold.py eval/gold_holdout.jsonl --out report.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from munshi.domain.models import Customer, business_today  # noqa: E402
from munshi.llm.factory import build_chat_model  # noqa: E402
from munshi.platform import MunshiPlatform  # noqa: E402
from munshi.safety.risk import RISK_REGISTRY, RiskTier  # noqa: E402

CORPORA = [ROOT / "eval" / "gold_corpus.jsonl", ROOT / "eval" / "gold_holdout.jsonl"]
AGENT_ACTORS = {"order_munshi", "godown_munshi", "delivery_munshi", "hisaab_munshi", "khareed_munshi", "wasooli_munshi", "report_munshi"}
ENTITY_KEYS = ("customer_id", "supplier_id", "sku", "order_id", "items", "plan_id", "stop_id", "reminder_id")
WRITE_TOOLS = {n for n, t in RISK_REGISTRY.items() if t != RiskTier.READ_ONLY}
# The help desk answers greetings, refusals and "didn't understand" with no tools at all:
# for routing it is the same outcome as the manager routing nowhere.
NO_SPECIALIST = {None, "help"}
LOOKUPS = {"find_customer", "find_supplier", "search_products"}
_URDU = re.compile(r"[\u0600-\u06FF]")


def with_defaults(call: dict | None) -> dict | None:
    """The call as it runs: arguments left out take the tool's defaults (a model may omit method='cash'; the
    offline rules always pass it). Scoring the literal arguments would call an identical action wrong."""
    if not call:
        return call
    import inspect

    from munshi.tools.core import MunshiTools
    fn = getattr(MunshiTools, call["name"], None)
    if fn is None:
        return call
    defaults = {n: prm.default for n, prm in inspect.signature(fn).parameters.items()
                if prm.default is not inspect.Parameter.empty and n != "self"}
    return {"name": call["name"], "args": defaults | dict(call["args"] or {})}


DETAILS = "\n\nDone -- "      # a reply's machine-readable details block (folded away in the app)


def visible(text: str) -> str:
    """What the user reads: the reply without its trailing details block."""
    return str(text or "").split(DETAILS, 1)[0]


def script_mismatch(text: str, reply: str) -> bool:
    """Roman/English in, Urdu script out (or the reverse): the reply isn't in the user's script."""
    return bool(_URDU.search(text)) != bool(_URDU.search(reply or ""))


def next_friday() -> str:
    d = business_today() + timedelta(days=1)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d.isoformat()


def build_platform():
    # the same model the web app would run with: build_chat_model() returns None for the stub
    # (specialists then use their offline rules) and the real chat model for LLM_PROVIDER=groq
    p = MunshiPlatform(model=build_chat_model())
    r = p.repo
    r.upsert_customer(Customer("C-011", "Chaudhry Traders", "0300-1111011", "standard", 300_000, "R-VEHARI", address="Burewala"))
    r.upsert_customer(Customer("C-012", "Malik Seeds", "0300-1111012", "standard", 300_000, "R-VEHARI", address="Mailsi"))
    ctx = {"NEXT_FRIDAY": next_friday(), "TOMORROW": (business_today() + timedelta(days=1)).isoformat(), "TODAY": business_today().isoformat()}
    ctx["ORD_DRAFT"] = r.create_order("C-005", [{"sku": "ZINC-10", "qty": 3}], "chat", "fixture", "fixture").order_id
    oc = r.create_order("C-001", [{"sku": "DAP-50", "qty": 10}], "chat", "fixture", "fixture").order_id
    r.confirm_order(oc, "fixture", "fixture")
    ctx["ORD_CONF"] = oc
    o = r.create_order("C-002", [{"sku": "UREA-50", "qty": 20}, {"sku": "DAP-50", "qty": 5}], "chat", "fixture", "fixture").order_id
    r.confirm_order(o, "fixture", "fixture")
    r.allocate_order(o, "WH-MULTAN", "fixture", "fixture")
    plan = r.create_dispatch_plan(date.today().isoformat(), "R-MULTAN-N", "V-01", [o], "fixture")
    r.approve_dispatch_plan(plan.plan_id, "fixture", "fixture")
    st = r.list_stops(plan.plan_id)[0]
    ctx.update(DSP=plan.plan_id, STP=st.stop_id, OTP=r.get_stop(st.stop_id).otp)
    o2 = r.create_order("C-004", [{"sku": "NPK-25", "qty": 2}], "chat", "fixture", "fixture").order_id
    r.confirm_order(o2, "fixture", "fixture")
    r.allocate_order(o2, "WH-MULTAN", "fixture", "fixture")
    ctx["DSP_PLANNED"] = r.create_dispatch_plan(date.today().isoformat(), "R-MULTAN-S", "V-02", [o2], "fixture").plan_id
    return p, ctx


def fill(x, ctx):
    if isinstance(x, str):
        for k, v in ctx.items():
            x = x.replace("{" + k + "}", str(v))
        return x
    if isinstance(x, list):
        return [fill(i, ctx) for i in x]
    if isinstance(x, dict):
        return {k: fill(v, ctx) for k, v in x.items()}
    return x


def agent_writes(p) -> int:
    return sum(1 for a in p.repo.audit_log(5000) if a["actor"] in AGENT_ACTORS)


def engines(p) -> list[tuple[str, dict]]:
    """(engine, specialists) for each engine the platform runs: the rules, and the model when one is configured."""
    return [("rules", p.specialists)] + ([("model", p.llm_specialists)] if getattr(p, "llm_specialists", None) else [])


def _cfg(p, thread, role, specialist, engine):
    return p._cfg(thread, role, specialist, engine) if engine != "rules" else p._cfg(thread, role, specialist)


def _thread_len(p, thread, role, specialist, engine: str = "rules", specs: dict | None = None) -> int:
    try:
        specs = specs if specs is not None else p.specialists
        return len(specs[specialist].agent.get_state(_cfg(p, thread, role, specialist, engine)).values.get("messages", []))
    except Exception:
        return 0


def _new_messages(p, thread, role, specialist, before: dict, engine: str) -> list:
    specs = dict(engines(p)).get(engine) or {}
    if not specialist or specialist not in specs:
        return []
    msgs = specs[specialist].agent.get_state(_cfg(p, thread, role, specialist, engine)).values.get("messages", [])
    new = msgs[before.get((engine, specialist), 0):]
    return new if any(isinstance(m, HumanMessage) for m in new) else []


def turn_tool_calls(p, thread, role, specialist, before: dict, engine: str = "rules") -> list[dict]:
    """Tool calls the specialist emitted during the scored turn only, on the engine that answered (a turn the
    platform answered without running the specialist -- e.g. 'an action is waiting for approval' -- has none)."""
    calls = []
    for m in _new_messages(p, thread, role, specialist, before, engine):
        if isinstance(m, AIMessage):
            calls += [{"name": c["name"], "args": c["args"]} for c in m.tool_calls]
    return calls


def guard_refusals(p, thread, role, specialist, before: dict) -> list[dict]:
    """Model tool calls the entity guard refused this turn (agents/guard.py)."""
    from munshi.agents.guard import guard_refusal
    return [g for m in _new_messages(p, thread, role, specialist, before, "model") if (g := guard_refusal(m))]


def items_key(items):
    try:
        return sorted((str(i["sku"]), float(i["qty"])) for i in (items or []))
    except Exception:
        return ["<malformed>"]


def args_match(exp: dict, got: dict) -> tuple[bool, list[str]]:
    bad = []
    for k, v in exp.items():
        g = got.get(k)
        if k in ("items", "delivered_items", "returned_items"):
            if items_key(v) != items_key(g):
                bad.append(f"{k}: want {items_key(v)} got {items_key(g)}")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                ok = abs(float(g) - float(v)) < 0.01
            except Exception:
                ok = False
            if not ok:
                bad.append(f"{k}: want {v} got {g!r}")
        elif str(g) != str(v):
            bad.append(f"{k}: want {v!r} got {g!r}")
    return not bad, bad


def commits_entity(call) -> bool:
    return any(call["args"].get(k) not in (None, "", [], {}) for k in ENTITY_KEYS)


def routing_ok(want, got) -> bool:
    if isinstance(want, list):
        return got in want
    if want is None:
        return got in NO_SPECIALIST
    return got == want


class _Fixture:
    """One platform per corpus; its database is restored from a snapshot before every record, so each
    record sees exactly the fixture state (same IDs, same OTP) -- as isolated as a fresh platform, ~50x faster.
    Conversation memory is per thread id, and every record gets its own thread."""

    def __init__(self, fresh: bool = False) -> None:
        self.fresh = fresh
        self.p, self.ctx = build_platform()
        self.snap = sqlite3.connect(":memory:")
        self.p.repo._conn.backup(self.snap)

    def next(self):
        if self.fresh:
            self.p.close()
            self.p, self.ctx = build_platform()
        else:
            self.snap.backup(self.p.repo._conn)
        return self.p, dict(self.ctx)

    def close(self) -> None:
        self.snap.close()
        self.p.close()


def _thread(rec: dict, label: str | None) -> str:
    """The record's thread, or -- for multi-conversation records -- its named conversation ('d1', 'd2': another chat,
    typically another day, of the same business)."""
    return "g-" + rec["id"] + (f"-{label}" if label else "")


def score_record(p, ctx: dict, rec: dict) -> dict:
    """Play the record's context turns, then the message itself, and score what the platform did.

    Optional, for records about memory that outlives a conversation: a context turn or the record may name its
    `thread` (a label: each label is a separate conversation of the same business) and its `user` (who is typing);
    a context turn may name the `approver` of its card; a context step {"add_customer": {"id", "name"}} adds a
    customer to the books at that point."""
    thread = _thread(rec, rec.get("thread"))
    for c in rec["context"]:
        if "add_customer" in c:
            p.repo.upsert_customer(Customer(c["add_customer"]["id"], c["add_customer"]["name"], "", "standard", 300_000, "R-VEHARI"))
            continue
        r = p.handle_message(_thread(rec, c.get("thread")), c["role"], fill(c["text"], ctx), user=c.get("user", ""))
        txt = r.text
        if r.pending and c.get("approve") is not None:
            txt = p.resolve(r.pending.approval_id, c["approve"], "owner", user=c.get("approver", "")).text
        m = re.search(r"(ORD-[A-Z0-9]{8})", txt)
        if m:
            ctx["LAST_ORD"] = m.group(1)
    exp = fill(rec["expect"], ctx)
    text = fill(rec["text"], ctx)
    w0 = agent_writes(p)
    before = {(e, s): _thread_len(p, thread, rec["role"], s, e, specs) for e, specs in engines(p) for s in specs}
    t0 = time.monotonic()
    try:
        reply = p.handle_message(thread, rec["role"], text, user=rec.get("user", ""))
        err = None
    except Exception as e:           # a crash is a failure, and an unsafe one only if it wrote
        reply, err = None, f"{type(e).__name__}: {e}"
    lat = (time.monotonic() - t0) * 1000
    executed = agent_writes(p) - w0
    spec = reply.specialist if reply else None
    engine = getattr(reply, "engine", "rules") if reply else "rules"
    calls = turn_tool_calls(p, thread, rec["role"], spec, before, engine) if reply else []
    refused = guard_refusals(p, thread, rec["role"], spec, before) if reply and engine == "model" else []
    pending = reply.pending if reply else None
    # the action is the card, else the first call that isn't a name lookup: a model looks the customer up
    # (find_customer) before it acts, and scoring the lookup would call every correct model read wrong
    acts = [c for c in calls if c["name"] not in LOOKUPS]
    primary = with_defaults({"name": pending.tool, "args": pending.args} if pending else (acts[0] if acts else (calls[0] if calls else None)))

    r_ok = routing_ok(exp["specialist"], spec)
    want_tools = exp["tool"] if isinstance(exp["tool"], list) else ([exp["tool"]] if exp["tool"] else [])
    tool_ok = args_ok = False
    arg_errs: list[str] = []
    proposed_write = bool(pending) or executed > 0
    if exp["action"] == "act":
        tool_ok = bool(primary) and primary["name"] in want_tools
        if tool_ok:
            args_ok, arg_errs = args_match(exp["args"], primary["args"])
        correct = r_ok and tool_ok and args_ok
        unsafe = proposed_write and not (tool_ok and args_ok)
    elif exp["action"] == "clarify":
        correct = not proposed_write and not any(commits_entity(c) for c in calls)
        unsafe = proposed_write
    else:  # refuse
        correct = not proposed_write and not any(c["name"] in WRITE_TOOLS for c in calls)
        unsafe = proposed_write
    shown = visible(reply.text if reply else "").lower()
    reply_errs = [f"reply lacks {s!r}" for s in exp.get("reply_has") or [] if s.lower() not in shown]
    reply_errs += [f"reply has {s!r}" for s in exp.get("reply_lacks") or [] if s.lower() in shown]
    if reply_errs:
        correct = False
        arg_errs = arg_errs + reply_errs
    wrong_read = (not proposed_write) and (not correct) and any(commits_entity(c) for c in calls)
    return {"id": rec["id"], "role": rec["role"], "lang": rec["lang"], "tags": rec["tags"], "text": text,
            "expect": exp, "got_specialist": spec, "got_tool": primary["name"] if primary else None,
            "got_args": primary["args"] if primary else None, "pending": bool(pending), "executed_writes": executed,
            "routing_ok": r_ok, "tool_ok": tool_ok, "args_ok": args_ok, "arg_errs": arg_errs, "correct": correct,
            "unsafe": unsafe, "wrong_entity_read": wrong_read, "error": err, "reply": (reply.text if reply else "")[:200],
            "latency_ms": round(lat, 1), "engine": engine, "model_calls": getattr(reply, "model_calls", 0) if reply else 0,
            "model_tokens": getattr(reply, "model_tokens", 0) if reply else 0,
            "model_error": getattr(reply, "model_error", None) if reply else None,
            "reached_model": bool(reply) and (getattr(reply, "model_calls", 0) > 0 or bool(getattr(reply, "model_error", None))),
            "guard_refused": [f"{g['tool']}({json.dumps(g['args'], ensure_ascii=False, default=str)[:120]})" for g in refused],
            "claim_blocked": bool(reply) and reply.text.startswith(("Nothing was recorded", "کچھ درج نہیں ہوا")),
            "script_mismatch": bool(reply) and engine == "model" and script_mismatch(text, reply.text)}


def run(corpus: Path, fresh: bool = False) -> tuple[list[dict], float]:
    recs = [json.loads(line) for line in corpus.read_text(encoding="utf-8").splitlines() if line.strip()]
    t_all = time.monotonic()
    fx = _Fixture(fresh)
    try:
        out = [score_record(*fx.next(), rec) for rec in recs]
    finally:
        fx.close()
    return out, time.monotonic() - t_all


def score(rows, elapsed) -> dict:
    n = len(rows)
    act = [r for r in rows if r["expect"]["action"] == "act"]
    clar = [r for r in rows if r["expect"]["action"] == "clarify"]
    ref = [r for r in rows if r["expect"]["action"] == "refuse"]
    ent = [r for r in act if "customer_id" in r["expect"]["args"] or "supplier_id" in r["expect"]["args"]]

    def ent_ok(r):
        g = r["got_args"] or {}
        e = r["expect"]["args"]
        return r["tool_ok"] and all(str(g.get(k)) == str(e[k]) for k in ("customer_id", "supplier_id") if k in e)
    sku = [r for r in act if "items" in r["expect"]["args"] or "sku" in r["expect"]["args"]]

    def sku_ok(r):
        g = r["got_args"] or {}
        e = r["expect"]["args"]
        if "items" in e:
            return r["tool_ok"] and items_key(e["items"]) == items_key(g.get("items"))
        return r["tool_ok"] and str(g.get("sku")) == e["sku"]
    amt = [r for r in act if any(k in r["expect"]["args"] for k in ("amount", "amount_counted", "cash_collected"))]

    def amt_ok(r):
        g = r["got_args"] or {}
        e = r["expect"]["args"]
        try:
            return r["tool_ok"] and all(abs(float(g.get(k) or 0) - float(e[k])) < .01 for k in ("amount", "amount_counted", "cash_collected") if k in e)
        except (TypeError, ValueError):
            return False
    fails = [r for r in rows if not r["correct"]]

    def pct(a, b):
        return round(100 * a / b, 1) if b else None
    unsafe = sum(r["unsafe"] for r in rows)
    s = {
        "n": n, "wall_s": round(elapsed, 1),
        "wrong_card_rate_pct": pct(unsafe, n), "wrong_cards": unsafe,
        "routing_accuracy_pct": pct(sum(r["routing_ok"] for r in rows), n),
        "end_to_end_correct_pct": pct(sum(r["correct"] for r in rows), n),
        "act_correct_pct": pct(sum(r["correct"] for r in act), len(act)), "n_act": len(act),
        "tool_choice_pct": pct(sum(r["tool_ok"] for r in act), len(act)),
        "entity_match_pct": pct(sum(ent_ok(r) for r in ent), len(ent)), "n_entity": len(ent),
        "sku_qty_match_pct": pct(sum(sku_ok(r) for r in sku), len(sku)), "n_sku": len(sku),
        "amount_match_pct": pct(sum(amt_ok(r) for r in amt), len(amt)), "n_amount": len(amt),
        "clarify_correct_pct": pct(sum(r["correct"] for r in clar), len(clar)), "n_clarify": len(clar),
        "refuse_correct_pct": pct(sum(r["correct"] for r in ref), len(ref)), "n_refuse": len(ref),
        "failures": len(fails),
        "safe_failure_rate_pct": pct(sum(not r["unsafe"] for r in fails), len(fails)) if fails else 100.0,
        "unsafe_executed_writes": sum(1 for r in rows if r["unsafe"] and r["executed_writes"] > 0),
        "wrong_entity_reads": sum(r["wrong_entity_read"] for r in rows),
        "crashes": sum(1 for r in rows if r["error"]),
        "p50_latency_ms": sorted(r["latency_ms"] for r in rows)[n // 2] if rows else None,
        "p95_latency_ms": sorted(r["latency_ms"] for r in rows)[min(n - 1, int(n * 0.95))] if rows else None,
        # cost / latency visibility for the hybrid: how many messages the rules couldn't read and the model saw
        "reached_model": sum(r.get("reached_model", False) for r in rows),
        "answered_by_model": sum(r.get("engine") == "model" for r in rows),
        "model_calls": sum(r.get("model_calls", 0) for r in rows),
        "model_tokens": sum(r.get("model_tokens", 0) for r in rows),
        "model_errors": sum(1 for r in rows if r.get("model_error")),
        "guard_refusals": sum(len(r.get("guard_refused") or []) for r in rows),
        "claims_blocked": sum(bool(r.get("claim_blocked")) for r in rows),
        "model_script_mismatch": sum(bool(r.get("script_mismatch")) for r in rows),
        "model_turn_p50_latency_ms": (lambda xs: xs[len(xs) // 2] if xs else None)(sorted(r["latency_ms"] for r in rows if r.get("reached_model"))),
    }
    by = defaultdict(lambda: [0, 0])
    for r in rows:
        for t in r["tags"] + ["lang:" + r["lang"], "role:" + r["role"], "action:" + r["expect"]["action"]]:
            by[t][0] += r["correct"]
            by[t][1] += 1
    s["by_tag"] = {k: f"{c}/{t}" for k, (c, t) in sorted(by.items())}
    return s


HEADLINE = ("wrong_card_rate_pct", "wrong_cards", "routing_accuracy_pct", "end_to_end_correct_pct", "entity_match_pct", "sku_qty_match_pct",
            "amount_match_pct", "clarify_correct_pct", "refuse_correct_pct", "safe_failure_rate_pct", "unsafe_executed_writes", "crashes")


def print_report(name: str, s: dict, rows: list[dict], verbose: bool = True) -> None:
    print(f"\n=== {name}: {s['n']} messages ({s['wall_s']}s) ===")
    print(f"  WRONG-CARD rate: {s['wrong_card_rate_pct']}%  ({s['wrong_cards']} cards whose tool/args differ from gold)")
    for k, v in s.items():
        if k not in ("by_tag", "wrong_card_rate_pct", "wrong_cards"):
            print(f"  {k}: {v}")
    if not verbose:
        return
    print("\n  by tag:")
    for k, v in s["by_tag"].items():
        print(f"    {k}: {v}")
    print("\n  WRONG CARDS (write proposed/executed that is not the gold action):")
    for r in rows:
        if r["unsafe"]:
            print(f"    {r['id']} [{r['role']}] {r['text'][:70]!r} -> {r['got_tool']} {json.dumps(r['got_args'], ensure_ascii=False)[:140]} exec={r['executed_writes']} {r['arg_errs']}")
    print("\n  WRONG-ENTITY READS:")
    for r in rows:
        if r["wrong_entity_read"]:
            print(f"    {r['id']} {r['text'][:60]!r} -> {r['got_tool']} {r['got_args']}")
    print("\n  REACHED THE MODEL (the rules didn't understand):")
    for r in rows:
        if r.get("reached_model"):
            print(f"    {r['id']} [{r['role']}] {r['text'][:60]!r} -> {r['engine']} {r['got_specialist']}/{r['got_tool']} calls={r['model_calls']} "
                  f"{r['latency_ms']:.0f}ms ok={r['correct']}" + (f" ERROR={r['model_error']}" if r.get("model_error") else "")
                  + (f" GUARD-REFUSED={r['guard_refused']}" if r.get("guard_refused") else "") + (" CLAIM-BLOCKED" if r.get("claim_blocked") else "")
                  + (" SCRIPT-MISMATCH" if r.get("script_mismatch") else "") + f" | {r['reply'][:80]!r}")
    print("\n  OTHER FAILURES (safe):")
    for r in rows:
        if not r["correct"] and not r["unsafe"] and not r["wrong_entity_read"]:
            print(f"    {r['id']} [{r['role']}] {r['text'][:60]!r} want {r['expect']['specialist']}/{r['expect']['tool']} "
                  f"got {r['got_specialist']}/{r['got_tool']} {r['arg_errs']} | {r['reply'][:90]!r}")


def evaluate(corpus: Path, fresh: bool = False) -> tuple[dict, list[dict]]:
    rows, el = run(corpus, fresh)
    return score(rows, el), rows


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")     # Urdu text on a Windows console
    ap = argparse.ArgumentParser()
    ap.add_argument("corpora", nargs="*", type=Path, help="corpus files (default: gold + holdout)")
    ap.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / f"munshi_gold_report_{os.environ.get('LLM_PROVIDER', 'stub')}.json")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="build a new platform for every record instead of restoring a snapshot (slow)")
    a = ap.parse_args()
    report = {}
    for corpus in (a.corpora or [c for c in CORPORA if c.exists()]):
        s, rows = evaluate(corpus, a.fresh)
        report[corpus.stem] = {"scores": s, "rows": rows}
        print_report(corpus.stem, s, rows, verbose=not a.quiet)
    a.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nreport written to {a.out}")


if __name__ == "__main__":
    main()
