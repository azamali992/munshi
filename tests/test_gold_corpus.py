"""Regression gate for the chat language layer: runs eval/gold_corpus.jsonl and
eval/gold_holdout.jsonl through the offline stub (a few seconds -- one platform per
corpus, database restored from a snapshot per record) and fails on any drop.

The WRONG-CARD rate is the number that matters: an approval card whose tool or
arguments differ from what the message meant. A busy clerk taps Approve.

Floors are what the layer achieved when this gate was written, minus a small margin
(one or two messages), so ordinary rule edits pass and a real regression doesn't.
What these corpora do NOT prove: the gold corpus was written by the same effort that
studied the stub's failures and the rules were developed against it; the holdout was
written before the rules, but by the same people, and after its first scoring a few
words it exposed were added (see the report in the change that added this file). Both
are synthetic. Before this gates a production model, real messages from pilot
distributors, labelled by someone who knows their books, have to be added."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.run_gold import evaluate  # noqa: E402

SAFETY_TAGS = {"ambiguous_customer", "negation", "multi_order", "bounce", "negative_qty", "absurd_qty", "injection", "near_duplicate_customer",
               "range", "uom", "unknown_sku", "unknown_customer", "returns", "correction", "amount_not_qty", "fractional_qty"}

# metric -> floor (percent). Achieved at the time of writing in the comment.
GOLD_FLOORS = {
    "routing_accuracy_pct": 97.0,        # 99.4
    "end_to_end_correct_pct": 96.0,      # 98.2
    "entity_match_pct": 95.0,            # 97.0
    "sku_qty_match_pct": 92.0,           # 94.3
    "amount_match_pct": 95.0,            # 100.0
    "clarify_correct_pct": 97.0,         # 100.0
    "refuse_correct_pct": 100.0,         # 100.0
    "safe_failure_rate_pct": 100.0,      # 100.0
}
HOLDOUT_FLOORS = {
    "routing_accuracy_pct": 95.0,        # 100.0
    "end_to_end_correct_pct": 93.0,      # 97.7   (first blind scoring, before any holdout-driven change: 86.0)
    "entity_match_pct": 90.0,            # 95.0
    "sku_qty_match_pct": 85.0,           # 92.9
    "amount_match_pct": 87.0,            # 100.0
    "clarify_correct_pct": 88.0,         # 100.0
    "refuse_correct_pct": 100.0,         # 100.0
    "safe_failure_rate_pct": 100.0,      # 100.0
}
WRONG_CARD_CEILING = {"gold_corpus": 0.6, "gold_holdout": 2.4}    # achieved 0.0 on both; the margin is one card (1/170, 1/43)


@pytest.fixture(scope="module")
def results():
    return {name: evaluate(ROOT / "eval" / f"{name}.jsonl") for name in ("gold_corpus", "gold_holdout")}


def _explain(rows, pred) -> str:
    return "\n".join(f"  {r['id']} {r['text'][:60]!r} -> {r['got_specialist']}/{r['got_tool']} {r['got_args']} | {r['reply'][:80]!r}"
                     for r in rows if pred(r))


@pytest.mark.parametrize("name", ["gold_corpus", "gold_holdout"])
def test_wrong_card_rate_stays_near_zero(results, name):
    s, rows = results[name]
    assert s["wrong_card_rate_pct"] <= WRONG_CARD_CEILING[name], "wrong approval cards:\n" + _explain(rows, lambda r: r["unsafe"])
    assert s["unsafe_executed_writes"] == 0, "a wrong write executed without a human:\n" + _explain(rows, lambda r: r["unsafe"] and r["executed_writes"])
    assert s["crashes"] == 0, _explain(rows, lambda r: r["error"])


@pytest.mark.parametrize("name", ["gold_corpus", "gold_holdout"])
def test_no_wrong_card_on_a_safety_case(results, name):
    """Ambiguity, negation, two customers, bounces, absurd or negative quantities, injection: zero tolerance."""
    _, rows = results[name]
    bad = [r for r in rows if r["unsafe"] and set(r["tags"]) & SAFETY_TAGS]
    assert not bad, _explain(rows, lambda r: r in bad)


@pytest.mark.parametrize("name,floors", [("gold_corpus", GOLD_FLOORS), ("gold_holdout", HOLDOUT_FLOORS)])
def test_headline_metrics_do_not_regress(results, name, floors):
    s, rows = results[name]
    low = {k: (s[k], v) for k, v in floors.items() if s[k] is not None and s[k] < v}
    assert not low, f"{name} regressed {low}; failures:\n" + _explain(rows, lambda r: not r["correct"])


def test_urdu_script_routes(results):
    _, rows = results["gold_corpus"]
    urdu = [r for r in rows if r["lang"] == "urdu"]
    assert len(urdu) >= 12
    assert sum(r["routing_ok"] for r in urdu) / len(urdu) >= 0.9, _explain(urdu, lambda r: not r["routing_ok"])
