import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from pgsage import cli
from pgsage.ai import ExplanationError
from pgsage.cli import make_client as real_make_client
from tests.fakes import FakeClient, use_client

PLAN = [
    {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Filter": "(customer_id = 7)",
            "Startup Cost": 0.0,
            "Total Cost": 10.0,
            "Plan Rows": 2000,
            "Actual Rows": 2000,
            "Actual Loops": 1,
            "Actual Total Time": 20.0,
            "Rows Removed by Filter": 198000,
        },
        "Execution Time": 20.0,
    }
]

REPLY = json.dumps(
    {
        "summary": "Seq scan dominates",
        "root_cause": "Missing index",
        "actions": [
            {
                "description": "Add index",
                "sql": None,
                "index": {"table": "orders", "columns": ["customer_id"]},
            }
        ],
    }
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient(REPLY)
    use_client(monkeypatch, client)
    return client


def test_explain_from_file(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(PLAN))
    result = runner.invoke(cli.app, ["explain", str(path)])
    assert result.exit_code == 0
    assert "Seq scan dominates" in result.output
    assert 'CREATE INDEX ON "orders" ("customer_id")' in result.output


def test_explain_from_stdin() -> None:
    result = runner.invoke(cli.app, ["explain"], input=json.dumps(PLAN))
    assert result.exit_code == 0
    assert "Cause: Missing index" in result.output


def test_explain_invalid_input_exits_2() -> None:
    result = runner.invoke(cli.app, ["explain"], input="not json")
    assert result.exit_code == 2


def test_explain_bad_model_reply_exits_2(fake_llm: FakeClient) -> None:
    fake_llm.reply = "I cannot help"
    result = runner.invoke(cli.app, ["explain"], input=json.dumps(PLAN))
    assert result.exit_code == 2


def test_explain_prints_evidence_confidence_and_ignored_actions(fake_llm: FakeClient) -> None:
    reply: dict[str, Any] = {
        "summary": "Seq scan dominates",
        "root_cause": "Missing index",
        "evidence": ["Seq Scan on orders: 300000 rows read"],
        "actions": [
            {
                "description": "Add index",
                "confidence": "high",
                "expected_effect": "avoids reading 300000 rows",
                "index": {"table": "orders", "columns": ["customer_id"]},
            },
            {"description": "Tune memory", "sql": "SET work_mem = '64MB'"},
            {"description": "bad", "index": {"table": "orders", "columns": []}},
        ],
    }
    fake_llm.reply = json.dumps(reply)
    result = runner.invoke(cli.app, ["explain"], input=json.dumps(PLAN))
    assert result.exit_code == 0
    assert "evidence: Seq Scan on orders: 300000 rows read" in result.output
    assert "[high confidence]" in result.output
    assert "expect: avoids reading 300000 rows" in result.output
    assert "ignored: action 3 rejected" in result.output
    assert "not verified" in result.output and "SET work_mem" in result.output


def test_explain_prints_token_and_cache_usage_when_the_client_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(REPLY)
    client.usage_log = [  # pyright: ignore[reportAttributeAccessIssue]
        {
            "input_tokens": 400,
            "cache_read_input_tokens": 3000,
            "cache_creation_input_tokens": 0,
            "output_tokens": 120,
        },
        {
            "input_tokens": 50,
            "cache_read_input_tokens": 3400,
            "cache_creation_input_tokens": 10,
            "output_tokens": 80,
        },
    ]
    use_client(monkeypatch, client)
    result = runner.invoke(cli.app, ["explain"], input=json.dumps(PLAN))
    assert "450 input, 6400 read from cache, 10 written to cache, 200 output" in result.output


def test_make_client_rejects_an_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        real_make_client("some-model", "bogus")


def test_explain_exits_2_when_the_model_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class Declining:
        def complete(self, system: str, prompt: str) -> str:
            raise ExplanationError("the model declined the request (category: cyber)")

    def make_declining(
        model: str | None,
        provider: str = "anthropic",
        base_url: str | None = None,
        effort: str | None = "high",
    ) -> Declining:
        return Declining()

    monkeypatch.setattr(cli, "make_client", make_declining)
    result = runner.invoke(cli.app, ["explain"], input=json.dumps(PLAN))
    assert result.exit_code == 2
    assert "declined" in result.output
