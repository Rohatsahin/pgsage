import json
import sys
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest

from pgsage import IndexSuggestion
from pgsage import context as context_module
from pgsage import verify as verify_module
from pgsage.context import get_plan, table_ancestors, table_info, top_queries
from pgsage.tune import tune
from pgsage.verify import (
    StatsSuggestion,
    literal,
    run_explain,
    run_single,
    verify_index,
    verify_rewrite,
    verify_statistics,
)
from tests.fakes import FakeClient, parse_prompt

pytestmark = pytest.mark.docker

QUERY = "SELECT * FROM orders WHERE customer_id = 7"


def reply(*actions: dict[str, Any]) -> str:
    return json.dumps({"summary": "s", "root_cause": "c", "actions": list(actions)})


def index_action(table: str, *columns: str, **extra: Any) -> dict[str, Any]:
    return {"description": "d", "index": {"table": table, "columns": list(columns), **extra}}


def exists(conn: psycopg.Connection, name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()
    return row == (True,)


@pytest.fixture
def victim(pg_conn: psycopg.Connection) -> Iterator[None]:
    pg_conn.execute("DROP TABLE IF EXISTS victim")
    pg_conn.execute("CREATE TABLE victim AS SELECT 1 AS id")
    yield
    pg_conn.execute("DROP TABLE IF EXISTS victim")


def test_the_database_refuses_a_second_statement_whatever_the_gate_says(
    pg_dsn: str, pg_conn: psycopg.Connection, victim: None
) -> None:
    with psycopg.connect(pg_dsn) as conn:
        with pytest.raises(psycopg.Error, match="multiple commands"):
            run_explain(conn, "SELECT 1; SELECT 2")
        conn.rollback()
        with pytest.raises(psycopg.Error, match="multiple commands"):
            run_single(conn, literal("CREATE INDEX ON orders (id); DROP TABLE victim"))
    assert exists(pg_conn, "victim")


def test_even_with_the_sql_gate_disabled_a_smuggled_statement_cannot_run(
    pg_dsn: str, pg_conn: psycopg.Connection, victim: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def allow_everything(sql: str) -> None:
        return None

    monkeypatch.setattr(verify_module, "ensure_read_only", allow_everything)
    with pytest.raises(psycopg.Error):
        get_plan(pg_dsn, "SELECT 1 /* x */; DROP TABLE victim")
    with pytest.raises(psycopg.Error):
        verify_rewrite(pg_dsn, "SELECT 1 AS a", "SELECT 1 AS a /* x */; DROP TABLE victim")
    assert exists(pg_conn, "victim")


@pytest.fixture
def partitioned(pg_conn: psycopg.Connection) -> Iterator[None]:
    pg_conn.execute("DROP TABLE IF EXISTS pt")
    pg_conn.execute("CREATE TABLE pt (id int, k int) PARTITION BY RANGE (id)")
    pg_conn.execute("CREATE TABLE pt1 PARTITION OF pt FOR VALUES FROM (0) TO (100000)")
    pg_conn.execute("CREATE TABLE pt2 PARTITION OF pt FOR VALUES FROM (100000) TO (200000)")
    pg_conn.execute("INSERT INTO pt SELECT g, g % 500 FROM generate_series(1, 199999) g")
    pg_conn.execute("ANALYZE pt")
    yield
    pg_conn.execute("DROP TABLE IF EXISTS pt")


PARTITIONED_QUERY = "SELECT * FROM pt WHERE id < 90000 AND k = 3"


def test_a_partition_knows_its_parent(pg_dsn: str, partitioned: None) -> None:
    assert table_ancestors(pg_dsn, "pt1") == [("public", "pt")]
    assert table_ancestors(pg_dsn, "pt") == []


def test_an_index_on_a_partitioned_parent_is_recognised_as_used(
    pg_dsn: str, partitioned: None
) -> None:
    result = verify_index(pg_dsn, PARTITIONED_QUERY, IndexSuggestion(table="pt", columns=["k"]))
    assert result.after.uses_new_index
    assert result.improved


def test_tune_accepts_a_proposal_on_the_parent_of_a_pruned_partition(
    pg_dsn: str, partitioned: None
) -> None:
    client = FakeClient(reply(index_action("pt", "k")))
    report = tune(client, pg_dsn, PARTITIONED_QUERY)
    sent = {t["table"] for t in parse_prompt(client.prompts[0])["tables"]}
    assert {"public.pt", "public.pt1"} <= sent
    assert report.verified[0].result.error is None
    assert report.verified[0].result.improved


@pytest.fixture
def matview(pg_conn: psycopg.Connection) -> Iterator[None]:
    pg_conn.execute("DROP MATERIALIZED VIEW IF EXISTS mv")
    pg_conn.execute("CREATE MATERIALIZED VIEW mv AS SELECT id, customer_id FROM orders")
    pg_conn.execute("ANALYZE mv")
    yield
    pg_conn.execute("DROP MATERIALIZED VIEW IF EXISTS mv")


def test_materialized_views_are_visible_to_table_info(pg_dsn: str, matview: None) -> None:
    info = table_info(pg_dsn, "mv")
    assert [c["column_name"] for c in info["columns"]] == ["id", "customer_id"]


def test_tune_works_on_a_materialized_view(pg_dsn: str, matview: None) -> None:
    report = tune(
        FakeClient(reply(index_action("mv", "customer_id"))),
        pg_dsn,
        "SELECT * FROM mv WHERE customer_id = 7",
    )
    assert report.verified[0].result.improved


def test_a_table_the_catalog_cannot_describe_is_skipped_not_fatal(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(dsn: str, table: str, schema: str = "public") -> dict[str, Any]:
        raise ValueError(f"table {schema}.{table} not found")

    monkeypatch.setattr(sys.modules["pgsage.tune"], "table_info", missing)
    report = tune(FakeClient(reply()), pg_dsn, QUERY)
    assert report.verified == []


@pytest.fixture
def other_schema(pg_conn: psycopg.Connection) -> Iterator[None]:
    pg_conn.execute("DROP SCHEMA IF EXISTS app2 CASCADE")
    pg_conn.execute("CREATE SCHEMA app2")
    pg_conn.execute(
        "CREATE TABLE app2.items AS SELECT g AS id, g % 500 AS k FROM generate_series(1, 100000) g"
    )
    pg_conn.execute("ANALYZE app2.items")
    yield
    pg_conn.execute("DROP SCHEMA app2 CASCADE")


def test_a_bare_table_name_resolves_to_its_real_schema(pg_dsn: str, other_schema: None) -> None:
    report = tune(
        FakeClient(reply(index_action("items", "k"))),
        pg_dsn,
        "SELECT * FROM app2.items WHERE k = 3",
    )
    change = report.verified[0]
    assert change.result.error is None
    assert change.index is not None and change.index.schema_name == "app2"
    assert change.result.improved


def test_an_ambiguous_table_name_must_name_its_schema(
    pg_dsn: str, pg_conn: psycopg.Connection, other_schema: None
) -> None:
    pg_conn.execute("DROP TABLE IF EXISTS public.items")
    pg_conn.execute(
        "CREATE TABLE public.items AS SELECT g AS id, g AS k FROM generate_series(1, 10) g"
    )
    try:
        report = tune(
            FakeClient(reply(index_action("items", "k"))),
            pg_dsn,
            "SELECT a.id FROM app2.items a JOIN public.items b ON a.id = b.id",
            1,
        )
    finally:
        pg_conn.execute("DROP TABLE IF EXISTS public.items")
    error = report.verified[0].result.error
    assert error is not None and "several schemas" in error


def test_an_unsafe_rewrite_is_a_failed_proposal_not_a_crash(pg_dsn: str) -> None:
    unsafe = {"description": "bad", "rewrite": "DELETE FROM orders"}
    smuggled = {"description": "bad2", "rewrite": "SELECT 1; DROP TABLE orders"}
    report = tune(
        FakeClient(reply(unsafe, smuggled, index_action("orders", "customer_id"))), pg_dsn, QUERY
    )
    assert len(report.verified) == 3
    assert report.verified[0].result.error is not None
    assert report.verified[1].result.error is not None
    assert report.verified[2].result.improved


def test_queries_with_placeholders_are_explained_clearly(pg_dsn: str) -> None:
    with pytest.raises(ValueError, match="placeholders"):
        get_plan(pg_dsn, "SELECT * FROM orders WHERE id = $1")
    with pytest.raises(ValueError, match="placeholders"):
        verify_rewrite(pg_dsn, "SELECT $1", "SELECT $1 + 0")


def test_a_repeated_proposal_is_flagged_instead_of_measured_twice(pg_dsn: str) -> None:
    unused = reply(index_action("orders", "created_at"))
    report = tune(FakeClient([unused, unused]), pg_dsn, QUERY)
    assert report.rounds == 2
    assert report.verified[0].result.error is None
    second = report.verified[1].result.error
    assert second is not None and "duplicate" in second


def test_schema_errors_keep_the_identifier_but_data_errors_do_not(pg_dsn: str) -> None:
    missing_column = {"description": "d", "rewrite": "SELECT nope FROM orders"}
    bad_value = {
        "description": "d",
        "rewrite": "SELECT * FROM orders WHERE customer_id = 'topsecret'",
    }
    client = FakeClient([reply(missing_column, bad_value), reply()])
    tune(client, pg_dsn, QUERY)
    errors = [a["error"] for a in parse_prompt(client.prompts[1])["previous_attempts"]]
    assert "nope" in errors[0]
    assert "topsecret" not in errors[1]


class Spy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
        self.calls = 0
        original: Callable[..., None] = module.begin_guarded

        def wrapper(conn: Any) -> None:
            self.calls += 1
            original(conn)

        monkeypatch.setattr(module, "begin_guarded", wrapper)


def test_every_proof_transaction_gets_timeouts(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = Spy(monkeypatch, verify_module)
    verify_index(pg_dsn, QUERY, IndexSuggestion(table="orders", columns=["customer_id"]))
    assert spy.calls == 2
    spy.calls = 0
    verify_statistics(pg_dsn, QUERY, StatsSuggestion(table="orders", columns=["customer_id", "id"]))
    assert spy.calls == 2
    spy.calls = 0
    verify_rewrite(pg_dsn, QUERY, QUERY + " AND id > 0")
    assert spy.calls == 1


def test_every_lookup_gets_timeouts_too(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(monkeypatch, context_module)
    get_plan(pg_dsn, QUERY)
    table_info(pg_dsn, "orders")
    table_ancestors(pg_dsn, "orders")
    assert spy.calls == 3


def test_rewrite_verification_runs_in_a_read_only_transaction_with_timeouts(pg_dsn: str) -> None:
    result = verify_rewrite(pg_dsn, QUERY, QUERY + " AND id > 0")
    assert result.equivalent is True
    assert result.kind == "rewrite"


def test_an_identical_rewrite_is_not_a_proof(pg_dsn: str) -> None:
    result = verify_rewrite(pg_dsn, QUERY, "  SELECT *   FROM orders\nWHERE customer_id = 7 ; ")
    assert result.error is not None and "identical" in result.error
    assert not result.improved


def test_top_queries_validates_its_limit(pg_dsn: str) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        top_queries(pg_dsn, limit=0)
