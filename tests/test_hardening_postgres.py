import json
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from pgsage import IndexSuggestion
from pgsage.context import get_plan, table_info, tables_in_plan
from pgsage.evals import CASES, OracleClient, run_evals
from pgsage.tune import tune
from pgsage.verify import (
    UnsafeStatementError,
    begin_guarded,
    verify_index,
    verify_rewrite,
)
from tests.fakes import FakeClient, parse_prompt

pytestmark = pytest.mark.docker

QUERY = "SELECT * FROM orders WHERE customer_id = 7"
INJECTION = 'id"); COMMIT; DROP TABLE victim; --'


@pytest.fixture
def victim(pg_conn: psycopg.Connection) -> Iterator[None]:
    pg_conn.execute("DROP TABLE IF EXISTS victim")
    pg_conn.execute("CREATE TABLE victim AS SELECT 1 AS id")
    yield
    pg_conn.execute("DROP TABLE IF EXISTS victim")


def exists(conn: psycopg.Connection, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()
    return row == (True,)


def leaked_verify_objects(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT count(*) FROM pg_class WHERE relname LIKE 'pgsage_verify%%'"
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_column_name_injection_cannot_commit_or_drop(
    pg_dsn: str, pg_conn: psycopg.Connection, victim: None
) -> None:
    with pytest.raises(psycopg.Error):
        verify_index(
            pg_dsn,
            "SELECT * FROM orders WHERE id = 5",
            IndexSuggestion(table="orders", columns=[INJECTION]),
        )
    assert exists(pg_conn, "victim")
    assert leaked_verify_objects(pg_conn) == 0


def test_table_name_injection_cannot_commit_or_drop(
    pg_dsn: str, pg_conn: psycopg.Connection, victim: None
) -> None:
    with pytest.raises(psycopg.Error):
        verify_index(
            pg_dsn,
            "SELECT * FROM orders WHERE id = 5",
            IndexSuggestion(table='orders"; COMMIT; DROP TABLE victim; --', columns=["id"]),
        )
    assert exists(pg_conn, "victim")


def test_schema_name_injection_cannot_commit_or_drop(
    pg_dsn: str, pg_conn: psycopg.Connection, victim: None
) -> None:
    with pytest.raises(psycopg.Error):
        verify_index(
            pg_dsn,
            "SELECT * FROM orders WHERE id = 5",
            IndexSuggestion(
                table="orders", columns=["id"], schema_name='x"; DROP TABLE victim; --'
            ),
        )
    assert exists(pg_conn, "victim")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; COMMIT; GRANT ALL ON victim TO PUBLIC",
        "SELECT 1; COMMIT; DO $$ BEGIN EXECUTE 'create table pwned()'; END $$",
    ],
)
def test_get_plan_rejects_commit_escape_attempts(
    pg_dsn: str, pg_conn: psycopg.Connection, victim: None, sql: str
) -> None:
    with pytest.raises(UnsafeStatementError):
        get_plan(pg_dsn, sql)
    assert not exists(pg_conn, "pwned")


@pytest.fixture
def side_effect_function(pg_conn: psycopg.Connection) -> Iterator[None]:
    pg_conn.execute("DROP TABLE IF EXISTS counter")
    pg_conn.execute("CREATE TABLE counter (n int)")
    pg_conn.execute(
        "CREATE OR REPLACE FUNCTION bump() RETURNS int LANGUAGE plpgsql AS "
        "$$ BEGIN INSERT INTO counter VALUES (1); RETURN 1; END $$"
    )
    yield
    pg_conn.execute("DROP FUNCTION IF EXISTS bump()")
    pg_conn.execute("DROP TABLE IF EXISTS counter")


def counter_rows(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM counter").fetchone()
    assert row is not None
    return int(row[0])


def test_user_defined_side_effects_are_blocked_by_the_read_only_transaction(
    pg_dsn: str, pg_conn: psycopg.Connection, side_effect_function: None
) -> None:
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        get_plan(pg_dsn, "SELECT bump()")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        verify_rewrite(pg_dsn, "SELECT bump() AS bump", "SELECT bump() + 0 AS bump")
    assert counter_rows(pg_conn) == 0


def test_proof_phases_are_read_only_even_for_the_after_measurement(
    pg_dsn: str, pg_conn: psycopg.Connection, side_effect_function: None
) -> None:
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        verify_index(
            pg_dsn,
            "SELECT bump() FROM orders WHERE customer_id = 7 LIMIT 1",
            IndexSuggestion(table="orders", columns=["customer_id"]),
        )
    assert counter_rows(pg_conn) == 0


def test_every_transaction_carries_a_statement_timeout(pg_dsn: str) -> None:
    with psycopg.connect(pg_dsn) as conn:
        begin_guarded(conn)
        row = conn.execute("SHOW statement_timeout").fetchone()
        assert row == ("1min",)
        row = conn.execute("SHOW lock_timeout").fetchone()
        assert row == ("5s",)


@pytest.fixture
def app_schema(pg_conn: psycopg.Connection) -> Iterator[None]:
    pg_conn.execute("DROP SCHEMA IF EXISTS app CASCADE")
    pg_conn.execute("CREATE SCHEMA app")
    pg_conn.execute(
        "CREATE TABLE app.items AS SELECT g AS id, g % 500 AS k FROM generate_series(1, 100000) g"
    )
    pg_conn.execute("ANALYZE app.items")
    yield
    pg_conn.execute("DROP SCHEMA app CASCADE")


def test_non_public_schema_is_resolved_from_the_plan(pg_dsn: str, app_schema: None) -> None:
    plan = get_plan(pg_dsn, "SELECT * FROM app.items WHERE k = 3")
    assert tables_in_plan(plan) == [("app", "items")]
    assert table_info(pg_dsn, "items", "app")["table"] == "app.items"


def test_tune_works_for_tables_outside_public(pg_dsn: str, app_schema: None) -> None:
    reply = json.dumps(
        {
            "summary": "s",
            "root_cause": "c",
            "actions": [
                {
                    "description": "index k",
                    "index": {"table": "items", "columns": ["k"], "schema_name": "app"},
                }
            ],
        }
    )
    report = tune(FakeClient(reply), pg_dsn, "SELECT * FROM app.items WHERE k = 3")
    assert report.verified[0].result.improved


def reply_with(**action: object) -> str:
    return json.dumps(
        {"summary": "s", "root_cause": "c", "actions": [{"description": "d", **action}]}
    )


@pytest.mark.parametrize(
    "action",
    [
        {"index": {"table": "orders", "columns": ["no_such_column"]}},
        {"index": {"table": "public.orders", "columns": ["customer_id"]}},
        {"index": {"table": "no_such_table", "columns": ["id"]}},
        {"statistics": {"table": "orders", "columns": ["nope", "customer_id"]}},
        {"rewrite": "SELECT nope FROM orders"},
        {"rewrite": "SELECT * FROM orders WHERE customer_id = 7 -- trailing comment"},
    ],
)
def test_a_bad_proposal_is_reported_as_unproven_not_a_crash(
    pg_dsn: str, action: dict[str, object]
) -> None:
    report = tune(FakeClient(reply_with(**action)), pg_dsn, QUERY, max_rounds=1)
    result = report.verified[0].result
    assert not result.improved
    assert result.error is not None or result.equivalent is not True


def test_an_invalid_action_is_rejected_without_losing_the_response(pg_dsn: str) -> None:
    good: dict[str, Any] = {
        "description": "ok",
        "rewrite": "SELECT * FROM orders WHERE customer_id = 7",
    }
    bad: dict[str, Any] = {"description": "bad", "index": {"table": "orders", "columns": []}}
    reply = json.dumps({"summary": "s", "root_cause": "c", "actions": [bad, good]})
    report = tune(FakeClient(reply), pg_dsn, QUERY, max_rounds=1)
    assert len(report.verified) == 1
    assert report.verified[0].result.kind == "rewrite"
    assert any("action 1 rejected" in line for line in report.explanation.rejected)


def test_round_one_results_survive_a_bad_round_two_proposal(pg_dsn: str) -> None:
    client = FakeClient(
        [
            reply_with(index={"table": "orders", "columns": ["created_at"]}),
            reply_with(index={"table": "orders", "columns": ["no_such_column"]}),
        ]
    )
    report = tune(client, pg_dsn, QUERY)
    assert report.rounds == 2
    assert len(report.verified) == 2
    assert report.verified[1].result.error is not None


def test_error_text_fed_back_to_the_model_hides_quoted_values(pg_dsn: str) -> None:
    client = FakeClient(
        [
            reply_with(rewrite="SELECT * FROM orders WHERE customer_id = 'topsecret'"),
            reply_with(),
        ]
    )
    tune(client, pg_dsn, QUERY)
    feedback = parse_prompt(client.prompts[1])["previous_attempts"][0]
    assert "topsecret" not in json.dumps(feedback)
    assert feedback["error"] is not None


def test_round_two_prompt_never_contains_literals_from_the_rewrite(pg_dsn: str) -> None:
    secret_query = "SELECT * FROM orders WHERE customer_id = 7 AND created_at > '2000-01-01'"
    client = FakeClient(
        [
            reply_with(
                rewrite="SELECT * FROM orders WHERE customer_id = __L1__ AND created_at > __L2__ AND id < 0"
            ),
            reply_with(),
        ]
    )
    report = tune(client, pg_dsn, secret_query)
    assert report.rounds == 2
    for prompt in client.prompts:
        assert "2000-01-01" not in prompt
    attempt = parse_prompt(client.prompts[1])["previous_attempts"][0]
    assert "__L1__" in attempt["statement"] and "__L2__" in attempt["statement"]


def test_table_info_redacts_literals_in_partial_index_definitions(
    pg_dsn: str, pg_conn: psycopg.Connection
) -> None:
    pg_conn.execute("CREATE INDEX hardening_partial ON orders (id) WHERE customer_id = 424242")
    try:
        info = table_info(pg_dsn, "orders")
    finally:
        pg_conn.execute("DROP INDEX hardening_partial")
    definitions = " ".join(i["indexdef"] for i in info["indexes"])
    assert "hardening_partial" in definitions
    assert "424242" not in definitions


def test_eval_never_touches_real_tables_with_the_same_names(
    pg_dsn: str, pg_conn: psycopg.Connection
) -> None:
    pg_conn.execute("DROP TABLE IF EXISTS ev_orders")
    pg_conn.execute("CREATE TABLE ev_orders AS SELECT 1 AS precious")
    try:
        outcomes = run_evals(OracleClient, pg_dsn, CASES[:1])
        row = pg_conn.execute("SELECT precious FROM ev_orders").fetchone()
    finally:
        pg_conn.execute("DROP TABLE IF EXISTS ev_orders")
    assert outcomes[0].passed
    assert row == (1,)
    left = pg_conn.execute(
        "SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'pgsage_eval_%'"
    ).fetchone()
    assert left == (0,)


def test_eval_setup_failure_is_reported_not_raised(pg_dsn: str) -> None:
    broken = CASES[0].model_copy(update={"setup": ("CREATE TABLE nope (",)})
    outcomes = run_evals(OracleClient, pg_dsn, [broken])
    assert not outcomes[0].passed
    assert outcomes[0].detail.startswith("error:")
