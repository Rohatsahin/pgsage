import json

import pytest

from pgsage import tune
from tests.fakes import FakeClient, parse_prompt

pytestmark = pytest.mark.docker

QUERY = "SELECT * FROM orders WHERE customer_id = 7"


def reply(columns: list[str]) -> str:
    return json.dumps(
        {
            "summary": "Seq scan discards 99% of rows",
            "root_cause": "No index on the filtered column",
            "actions": [
                {
                    "description": "Index the filter column",
                    "sql": None,
                    "index": {"table": "orders", "columns": columns},
                }
            ],
        }
    )


def test_tune_sends_plan_and_table_context_then_verifies_proposal(pg_dsn: str) -> None:
    client = FakeClient(reply(["customer_id"]))
    report = tune(client, pg_dsn, QUERY)
    prompt = parse_prompt(client.prompts[0])
    assert prompt["tables"][0]["table"] == "public.orders"
    assert prompt["digest"][0]["node"] == "Seq Scan"
    assert len(report.verified) == 1
    assert report.verified[0].result.improved


def test_tune_does_not_trust_a_bad_proposal(pg_dsn: str) -> None:
    report = tune(FakeClient(reply(["created_at"])), pg_dsn, QUERY)
    assert not report.verified[0].result.improved


def test_tune_without_index_actions_verifies_nothing(pg_dsn: str) -> None:
    reply_text = json.dumps({"summary": "fine", "root_cause": "none", "actions": []})
    assert tune(FakeClient(reply_text), pg_dsn, QUERY).verified == []


def test_tune_retries_with_feedback_when_first_round_is_not_proven(pg_dsn: str) -> None:
    client = FakeClient([reply(["created_at"]), reply(["customer_id"])])
    report = tune(client, pg_dsn, QUERY)
    assert report.rounds == 2
    assert [v.result.improved for v in report.verified] == [False, True]
    feedback = parse_prompt(client.prompts[1])["previous_attempts"]
    assert feedback[0]["proven"] is False
    assert "created_at" in feedback[0]["statement"]


def test_tune_stops_after_max_rounds(pg_dsn: str) -> None:
    client = FakeClient(reply(["created_at"]))
    report = tune(client, pg_dsn, QUERY, max_rounds=2)
    assert report.rounds == 2
    assert report.proven == []
    assert len(client.prompts) == 2


def test_tune_restores_redacted_literals_in_rewrites(pg_dsn: str) -> None:
    rewrite = json.dumps(
        {
            "summary": "same query",
            "root_cause": "none",
            "actions": [
                {
                    "description": "equivalent",
                    "rewrite": "SELECT * FROM orders WHERE customer_id = __L1__ AND id > 0",
                }
            ],
        }
    )
    client = FakeClient(rewrite)
    report = tune(client, pg_dsn, QUERY, max_rounds=1)
    sent = parse_prompt(client.prompts[0])["query"]
    assert "7" not in sent.replace("__L1__", "") and "__L1__" in sent
    assert report.verified[0].result.equivalent is True
    assert "customer_id = 7" in report.verified[0].result.statement


def test_zero_rounds_still_runs_the_first_attempt_once(pg_dsn: str) -> None:
    client = FakeClient(reply(["created_at"]))
    report = tune(client, pg_dsn, QUERY, max_rounds=0)
    assert report.rounds == 1
    assert len(client.prompts) == 1
    assert len(report.verified) == 1


def test_several_actions_in_one_round_are_all_verified_and_reported(pg_dsn: str) -> None:
    actions = [
        {"description": "useless", "index": {"table": "orders", "columns": ["created_at"]}},
        {"description": "good", "index": {"table": "orders", "columns": ["customer_id"]}},
        {"description": "ok rewrite", "rewrite": QUERY + " AND id > 0"},
    ]
    reply_text = json.dumps({"summary": "s", "root_cause": "c", "actions": actions})
    report = tune(FakeClient(reply_text), pg_dsn, QUERY)
    assert report.rounds == 1
    assert [v.result.kind for v in report.verified] == ["index", "index", "rewrite"]
    assert [v.result.improved for v in report.verified][1] is True
