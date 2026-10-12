import json

import pytest

from pgsage import parse_explain
from pgsage.ai import (
    ExplanationError,
    build_prompt,
    explain_plan,
    parse_explanation,
    redact_condition,
)
from tests.fakes import FakeClient, parse_prompt

PLAN = [
    {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Filter": "((email = 'jane@example.com'::text) AND (customer_id = 7) AND (x > 1.5))",
            "Startup Cost": 0.0,
            "Total Cost": 10.0,
            "Plan Rows": 2000,
            "Actual Rows": 2000,
            "Actual Loops": 1,
            "Actual Total Time": 20.0,
            "Rows Removed by Filter": 198000,
            "Plans": [
                {
                    "Node Type": "Index Scan",
                    "Index Cond": "(id = 42)",
                    "Startup Cost": 0.0,
                    "Total Cost": 1.0,
                    "Plan Rows": 1,
                }
            ],
        },
        "Execution Time": 20.0,
    }
]

VALID = {
    "summary": "A sequential scan dominates.",
    "root_cause": "No index on customer_id.",
    "actions": [
        {
            "description": "Add an index",
            "sql": "CREATE INDEX ON orders (customer_id)",
            "index": {"table": "orders", "columns": ["customer_id"]},
        }
    ],
}


def test_redacts_string_and_numeric_literals_but_keeps_columns_and_casts() -> None:
    redacted = redact_condition("((email = 'jane@example.com'::text) AND (customer_id = 7))")
    assert redacted == "((email = ?::text) AND (customer_id = ?))"


def test_redacts_escaped_quotes_and_decimals() -> None:
    assert redact_condition("(name = 'O''Brien') AND (x > -1.5)") == "(name = ?) AND (x > ?)"


def test_prompt_never_contains_literal_values() -> None:
    result = parse_explain(PLAN)
    prompt = build_prompt(result, "SELECT * FROM orders WHERE email = 'jane@example.com'")
    assert "jane@example.com" not in prompt
    assert '"digest"' in prompt
    assert "42" not in parse_prompt(prompt)["plan"]["Plan"]["Plans"][0]["Index Cond"]
    assert "customer_id" in prompt


def test_explain_plan_parses_json_reply() -> None:
    result = parse_explain(PLAN)
    client = FakeClient(json.dumps(VALID))
    explanation = explain_plan(client, result)
    assert explanation.actions[0].sql == "CREATE INDEX ON orders (customer_id)"
    assert explanation.actions[0].index is not None
    assert explanation.actions[0].index.columns == ["customer_id"]
    assert len(client.prompts) == 1


def test_parses_reply_wrapped_in_code_fence() -> None:
    fenced = f"```json\n{json.dumps(VALID)}\n```"
    assert parse_explanation(fenced).summary == VALID["summary"]


@pytest.mark.parametrize("reply", ["not json", '{"summary": "x"}', "[]"])
def test_invalid_reply_raises(reply: str) -> None:
    with pytest.raises(ExplanationError):
        parse_explanation(reply)
