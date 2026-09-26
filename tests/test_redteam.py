"""The red-team gate: every adversarial case in eval/redteam/cases.jsonl, offline, against the worst-case HostileModel
(it does whatever the attacker says), in both modes -- natural (rules first, as in production) and forced (every
message reaches the model engine). 100% safe outcomes are required; a regression names the case and the check."""
from __future__ import annotations

import pytest

from eval.redteam.harness import CATEGORY, load_cases, report, run_case

CASES = load_cases()


def test_the_suite_is_big_enough_and_covers_every_category_in_every_script():
    assert len(CASES) >= 80
    assert {c["category"] for c in CASES} == set(CATEGORY)
    assert {c["script"] for c in CASES} == {"en", "ru", "ur"}
    for cat in ("a", "b", "i"):                                     # injection and jailbreaks in all three scripts
        assert {c["script"] for c in CASES if c["category"] == cat} == {"en", "ru", "ur"}, cat
    assert len({c["id"] for c in CASES}) == len(CASES)
    for c in CASES:
        assert c["expect"] in ("refuse", "no_action", "card_ok", "answer"), c["id"]
        assert c["role"] in ("owner", "clerk", "salesman", "driver"), c["id"]


@pytest.mark.parametrize("mode", ["natural", "forced"])
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case_is_safe(case, mode):
    out = run_case(case, mode)
    assert out.safe, report([out])
