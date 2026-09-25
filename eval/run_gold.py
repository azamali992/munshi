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
args{subset that must match}}, note.

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
from munshi.platform import MunshiPlatform  # noqa: E402
from munshi.safety.risk import RISK_REGISTRY, RiskTier  # noqa: E402

CORPORA = [ROOT / "eval" / "gold_corpus.jsonl", ROOT / "eval" / "gold_holdout.jsonl"]
AGENT_ACTORS = {"order_munshi", "godown_munshi", "delivery_munshi", "hisaab_munshi", "khareed_munshi", "wasooli_munshi", "report_munshi"}
ENTITY_KEYS = ("customer_id", "supplier_id", "sku", "order_id", "items", "plan_id", "stop_id", "reminder_id")
WRITE_TOOLS = {n for n, t in RISK_REGISTRY.items() if t != RiskTier.READ_ONLY}
# The help desk answers greetings, refusals and "didn't understand" with no tools at all:
# for routing it is the same outcome as the manager routing nowhere.
NO_SPECIALIST = {None, "help"}


def next_friday() -> str:
    d = business_today() + timedelta(days=1)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d.isoformat()


def build_platform():
    p = MunshiPlatform()
    r = p.repo
    r.upsert_customer(Customer("C-011", "Chaudhry Traders", "0300-1111011", "standard", 300_000, "R-VEHARI", address="Burewala"))
    r.upsert_customer(Customer("C-012", "Malik Seeds", "0300-1111012", "standard", 300_000, "R-VEHARI", address="Mailsi"))
    ctx = {"NEXT_FRIDAY": next_friday()}
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


def _thread_len(p, thread, role, specialist) -> int:
    try:
        return len(p.specialists[specialist].agent.get_state(p._cfg(thread, role, specialist)).values.get("messages", []))
    except Exception:
        return 0


def turn_tool_calls(p, thread, role, specialist, before: dict[str, int]) -> list[dict]:
    """Tool calls the specialist emitted during the scored turn only (a turn the platform answered without
    running the specialist -- e.g. 'an action is waiting for approval' -- has none)."""
    if not specialist or specialist not in p.specialists:
        return []
    msgs = p.specialists[specialist].agent.get_state(p._cfg(thread, role, specialist)).values.get("messages", [])
    new = msgs[before.get(specialist, 0):]
    if not any(isinstance(m, HumanMessage) for m in new):
        return []
    calls = []
    for m in new:
        if isinstance(m, AIMessage):
            calls += [{"name": c["name"], "args": c["args"]} for c in m.tool_calls]
    return calls


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


def score_record(p, ctx: dict, rec: dict) -> dict:
    """Play the record's context turns, then the message itself, and score what the platform did."""
    thread = "g-" + rec["id"]
    for c in rec["context"]:
        r = p.handle_message(thread, c["role"], fill(c["text"], ctx))
        txt = r.text
        if r.pending and c.get("approve") is not None:
            txt = p.resolve(r.pending.approval_id, c["approve"], "owner").text
        m = re.search(r"(ORD-[A-Z0-9]{8})", txt)
        if m:
            ctx["LAST_ORD"] = m.group(1)
    exp = fill(rec["expect"], ctx)
    text = fill(rec["text"], ctx)
    w0 = agent_writes(p)
    before = {s: _thread_len(p, thread, rec["role"], s) for s in p.specialists}
    t0 = time.monotonic()
    try:
        reply = p.handle_message(thread, rec["role"], text)
        err = None
    except Exception as e:           # a crash is a failure, and an unsafe one only if it wrote
        reply, err = None, f"{type(e).__name__}: {e}"
    lat = (time.monotonic() - t0) * 1000
    executed = agent_writes(p) - w0
    spec = reply.specialist if reply else None
    calls = turn_tool_calls(p, thread, rec["role"], spec, before) if reply else []
    pending = reply.pending if reply else None
    primary = {"name": pending.tool, "args": pending.args} if pending else (calls[0] if calls else None)

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
    wrong_read = (not proposed_write) and (not correct) and any(commits_entity(c) for c in calls)
    return {"id": rec["id"], "role": rec["role"], "lang": rec["lang"], "tags": rec["tags"], "text": text,
            "expect": exp, "got_specialist": spec, "got_tool": primary["name"] if primary else None,
            "got_args": primary["args"] if primary else None, "pending": bool(pending), "executed_writes": executed,
            "routing_ok": r_ok, "tool_ok": tool_ok, "args_ok": args_ok, "arg_errs": arg_errs, "correct": correct,
            "unsafe": unsafe, "wrong_entity_read": wrong_read, "error": err, "reply": (reply.text if reply else "")[:200],
            "latency_ms": round(lat, 1)}


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
