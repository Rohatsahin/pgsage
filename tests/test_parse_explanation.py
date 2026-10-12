import json
from typing import Any

import pytest

from pgsage.ai import ExplanationError, parse_explanation


def answer(actions: list[Any]) -> dict[str, Any]:
    return {"summary": "s", "root_cause": "c", "evidence": ["e"], "actions": actions}


GOOD: dict[str, Any] = {"description": "d", "rewrite": "SELECT 1"}


def test_plain_json() -> None:
    assert parse_explanation(json.dumps(answer([GOOD]))).actions[0].rewrite == "SELECT 1"


def test_json_wrapped_in_fences_and_prose() -> None:
    wrapped = (
        f"Sure! Here is the analysis:\n```json\n{json.dumps(answer([GOOD]))}\n```\nHope it helps."
    )
    assert parse_explanation(wrapped).summary == "s"


def test_json_surrounded_by_prose_without_fences() -> None:
    assert parse_explanation(f"Analysis: {json.dumps(answer([]))} Done.").actions == []


@pytest.mark.parametrize(
    "text", ["", "I cannot help with that.", "[]", '"just a string"', "{broken"]
)
def test_garbage_is_an_explanation_error(text: str) -> None:
    with pytest.raises(ExplanationError):
        parse_explanation(text)


def test_missing_actions_is_an_error() -> None:
    with pytest.raises(ExplanationError):
        parse_explanation(json.dumps({"summary": "s", "root_cause": "c"}))


def test_missing_summary_is_an_error() -> None:
    with pytest.raises(ExplanationError):
        parse_explanation(json.dumps({"root_cause": "c", "actions": []}))


def test_one_bad_action_does_not_discard_the_good_ones() -> None:
    bad_kind: dict[str, Any] = {"description": "x", "index": {"table": "t", "columns": []}}
    explanation = parse_explanation(json.dumps(answer([bad_kind, GOOD])))
    assert [a.rewrite for a in explanation.actions] == ["SELECT 1"]
    assert len(explanation.rejected) == 1
    assert "action 1 rejected" in explanation.rejected[0]


def test_an_action_with_two_change_kinds_is_rejected() -> None:
    both = {
        "description": "x",
        "rewrite": "SELECT 1",
        "index": {"table": "t", "columns": ["a"]},
    }
    explanation = parse_explanation(json.dumps(answer([both])))
    assert explanation.actions == []
    assert "at most one" in explanation.rejected[0]


def test_more_than_three_actions_are_truncated_and_reported() -> None:
    explanation = parse_explanation(json.dumps(answer([GOOD] * 5)))
    assert len(explanation.actions) == 3
    assert any("more than 3" in line for line in explanation.rejected)


def test_explicit_nulls_as_required_by_the_schema_are_accepted() -> None:
    action = {
        "description": "d",
        "confidence": "high",
        "expected_effect": None,
        "index": {
            "table": "t",
            "columns": ["a"],
            "schema_name": None,
            "method": "btree",
            "include": [],
            "where": None,
        },
        "statistics": None,
        "rewrite": None,
        "sql": None,
    }
    explanation = parse_explanation(json.dumps(answer([action])))
    assert explanation.rejected == []
    assert explanation.actions[0].index is not None
    assert explanation.actions[0].confidence == "high"


def test_unknown_confidence_values_are_rejected_per_action() -> None:
    explanation = parse_explanation(json.dumps(answer([{**GOOD, "confidence": "certain"}])))
    assert explanation.actions == []
    assert explanation.rejected
