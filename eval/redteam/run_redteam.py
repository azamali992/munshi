"""Run the red-team suite and print a report by category.

    python eval/redteam/run_redteam.py                      # offline, hostile fake model, natural + forced modes
    python eval/redteam/run_redteam.py --only a01,b03 -v    # some cases, with every reply
    python eval/redteam/run_redteam.py --category e,g
    python eval/redteam/run_redteam.py --real --pace 5      # the configured real model (LLM_PROVIDER / LLM_MODEL / key in env)
    LLM_MODEL=gemini-3.5-flash-lite python eval/redteam/run_redteam.py --real --env-file .env --category a,e,g,i

With --real the model is whatever LLM_PROVIDER builds (gemini / groq); the attack scripts are ignored (the real model
does what it does) and only the "forced" mode is run by default, since the natural mode rarely reaches the model.
Requests are paced (--pace seconds between messages) for free tiers. API keys are read from the environment and are
never printed. Exit status: 0 when every run is safe, 1 otherwise (--json writes the details)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MUNSHI_NO_AUTOAPP", "1")
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

from eval.redteam.harness import CATEGORY, load_cases, report, run_all  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma-separated case ids")
    ap.add_argument("--category", default="", help="comma-separated categories (a..i)")
    ap.add_argument("--mode", default="", help="natural, forced or both (default: both offline, forced with --real)")
    ap.add_argument("--real", action="store_true", help="run against the configured real model instead of the hostile fake")
    ap.add_argument("--pace", type=float, default=4.0, help="seconds between messages with --real")
    ap.add_argument("--limit", type=int, default=0, help="at most this many cases")
    ap.add_argument("--json", default="", help="write every outcome to this file")
    ap.add_argument("--env-file", default="", help="with --real: read GOOGLE_API_KEY / GROQ_API_KEY from this dotenv file (never printed)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cases = load_cases()
    if a.only:
        want = {x.strip() for x in a.only.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in want]
    if a.category:
        cats = {x.strip() for x in a.category.split(",") if x.strip()}
        cases = [c for c in cases if c["category"] in cats]
    if a.limit:
        cases = cases[:a.limit]
    modes = {"natural": ("natural",), "forced": ("forced",), "both": ("natural", "forced")}.get(a.mode or ("forced" if a.real else "both"))
    if modes is None:
        ap.error("--mode is natural, forced or both")

    factory, pace = None, 0.0
    if a.real:
        if a.env_file:
            for line in Path(a.env_file).read_text(encoding="utf-8").splitlines():
                k, sep, v = line.partition("=")
                if sep and k.strip() in ("GOOGLE_API_KEY", "GROQ_API_KEY") and not os.environ.get(k.strip()):
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")
        if os.environ.get("LLM_PROVIDER", "stub") == "stub":
            os.environ["LLM_PROVIDER"] = "gemini"          # (--real means a real model; gemini unless LLM_PROVIDER says groq)
        from munshi.llm.factory import build_chat_model
        real = build_chat_model()
        factory, pace = (lambda script: real), a.pace
        print(f"real model: {os.environ['LLM_PROVIDER']} {os.environ.get('LLM_MODEL', '(default)')}, pace {pace}s")

    t0 = time.time()

    def progress(o):
        mark = "ok  " if o.safe else "FAIL"
        print(f"{mark} {o.case['id']:<5} {o.mode:<8} calls={sum(o.model_calls):<3}" + ("" if o.safe else "  " + "; ".join(f"[{k}] {v}" for k, v in o.violations)
                                                                                       + (f" [crash] {o.error}" if o.error else "")))
        if a.verbose:
            for r in o.replies:
                print("      > " + r.replace("\n", " | ")[:400])

    outcomes = run_all(cases, modes, factory, pace, progress if (a.verbose or a.real or a.only) else None)
    print()
    print(f"{len(cases)} cases x {len(modes)} mode(s) in {time.time() - t0:.0f}s ({'real model' if a.real else 'hostile fake model'})")
    print(report(outcomes))
    if a.json:
        Path(a.json).write_text(json.dumps([{"id": o.case["id"], "category": o.case["category"], "category_name": CATEGORY.get(o.case["category"]),
                                             "mode": o.mode, "safe": o.safe, "violations": o.violations, "error": o.error,
                                             "model_calls": o.model_calls, "replies": o.replies, "cards": o.cards} for o in outcomes],
                                           ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if all(o.safe for o in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
