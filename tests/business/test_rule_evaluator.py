import json
from pathlib import Path

import pytest

from packages.rules.evaluator import RuleDefinitionError, evaluate_bundle, evaluate_rule

CASES = json.loads(
    (
        Path(__file__).resolve().parents[2] / "contracts/examples/synthetic-rule-cases.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES["cases"])
def test_synthetic_cases_preserve_three_valued_truth(case):
    rule = next(rule for rule in CASES["bundle"]["rules"] if rule["rule_id"] == case["rule_id"])
    actual, _ = evaluate_rule(rule, case["facts"], case["reference_date"])
    assert actual is case["expected"]


def test_bundle_evaluation_is_priority_then_rule_id():
    result = evaluate_bundle(CASES["bundle"], CASES["cases"][0]["facts"], "2026-09-27")
    assert [item["rule_id"] for item in result] == ["SYNTHETIC-DATE", "SYNTHETIC-PAIR"]


def test_duplicate_rule_ids_are_rejected():
    bundle = dict(CASES["bundle"], rules=CASES["bundle"]["rules"] * 2)
    with pytest.raises(RuleDefinitionError):
        evaluate_bundle(bundle, {}, "2026-09-27")
