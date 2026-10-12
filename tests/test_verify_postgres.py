import psycopg
import pytest

from pgsage import IndexSuggestion
from pgsage.verify import (
    StatsSuggestion,
    UnsafeStatementError,
    ensure_read_only,
    verify_index,
    verify_rewrite,
    verify_statistics,
)

pytestmark = pytest.mark.docker

QUERY = "SELECT * FROM orders WHERE customer_id = 7"


def test_verified_index_improves_the_plan(pg_dsn: str) -> None:
    result = verify_index(pg_dsn, QUERY, IndexSuggestion(table="orders", columns=["customer_id"]))
    assert result.before.uses_new_index is False
    assert result.after.uses_new_index is True
    assert result.after.total_cost < result.before.total_cost
    assert result.improved


def test_verify_leaves_no_index_behind(pg_dsn: str, pg_conn: psycopg.Connection) -> None:
    verify_index(pg_dsn, QUERY, IndexSuggestion(table="orders", columns=["customer_id"]))
    row = pg_conn.execute(
        "SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'pgsage_verify_idx%'"
    ).fetchone()
    assert row == (0,)


def test_useless_index_is_not_reported_as_improvement(pg_dsn: str) -> None:
    result = verify_index(
        pg_dsn, "SELECT * FROM orders", IndexSuggestion(table="orders", columns=["customer_id"])
    )
    assert result.after.uses_new_index is False
    assert not result.improved


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "UPDATE orders SET id = 1",
        "SELECT 1; DROP TABLE orders",
        "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
    ],
)
def test_rejects_writes(sql: str) -> None:
    with pytest.raises(UnsafeStatementError):
        ensure_read_only(sql)


def test_equivalent_rewrite_is_measured(pg_dsn: str, pg_conn: psycopg.Connection) -> None:
    pg_conn.execute("DROP TABLE IF EXISTS rw")
    pg_conn.execute(
        "CREATE TABLE rw AS SELECT g AS id, timestamp '2024-01-01' + g * interval '1 minute' AS ts "
        "FROM generate_series(1, 300000) g"
    )
    try:
        pg_conn.execute("CREATE INDEX ON rw (ts)")
        pg_conn.execute("ANALYZE rw")
        result = verify_rewrite(
            pg_dsn,
            "SELECT * FROM rw WHERE ts::date = date '2024-03-01'",
            "SELECT * FROM rw WHERE ts >= date '2024-03-01' AND ts < date '2024-03-02'",
        )
    finally:
        pg_conn.execute("DROP TABLE IF EXISTS rw")
    assert result.kind == "rewrite"
    assert result.equivalent is True
    assert result.improved


def test_non_equivalent_rewrite_is_never_proven(pg_dsn: str) -> None:
    result = verify_rewrite(
        pg_dsn,
        "SELECT * FROM orders WHERE customer_id = 7",
        "SELECT * FROM orders WHERE customer_id = 7 AND id < 10",
    )
    assert result.equivalent is False
    assert not result.improved


def test_rewrite_rejects_write_statements(pg_dsn: str) -> None:
    with pytest.raises(UnsafeStatementError):
        verify_rewrite(pg_dsn, "SELECT 1", "DELETE FROM orders")


def test_statistics_verification_runs_and_rolls_back(
    pg_dsn: str, pg_conn: psycopg.Connection
) -> None:
    result = verify_statistics(
        pg_dsn,
        "SELECT * FROM orders WHERE customer_id = 7",
        StatsSuggestion(table="orders", columns=["customer_id", "id"]),
    )
    row = pg_conn.execute(
        "SELECT count(*) FROM pg_statistic_ext WHERE stxname = 'pgsage_verify_stats'"
    ).fetchone()
    assert result.kind == "statistics"
    assert row == (0,)
    assert result.error is None
    assert result.before.execution_ms is not None and result.after.execution_ms is not None


def test_index_ddl_and_names() -> None:
    suggestion = IndexSuggestion(table="orders", columns=["customer_id", "status"], schema_name="s")
    assert suggestion.ddl == 'CREATE INDEX ON "s"."orders" ("customer_id", "status")'
    assert suggestion.suggested_name == "idx_orders_customer_id_status"
    default = StatsSuggestion(table="orders", columns=["a", "b"])
    mcv = StatsSuggestion(table="orders", columns=["a", "b"], kinds=["mcv"])
    assert default.suggested_name == "stats_orders_a_b"
    assert mcv.suggested_name != default.suggested_name
    assert mcv.ddl.endswith('(mcv) ON "a", "b" FROM "orders"')


def test_partial_index_is_proven_when_the_query_matches_its_predicate(pg_dsn: str) -> None:
    result = verify_index(
        pg_dsn,
        "SELECT id FROM orders WHERE customer_id = 7",
        IndexSuggestion(table="orders", columns=["id"], where="customer_id = 7"),
    )
    assert result.statement.endswith("WHERE customer_id = 7")
    assert result.after.uses_new_index
    assert result.improved


def test_covering_index_enables_an_index_only_scan(
    pg_dsn: str, pg_conn: psycopg.Connection
) -> None:
    pg_conn.execute("VACUUM orders")
    result = verify_index(
        pg_dsn,
        "SELECT customer_id, created_at FROM orders WHERE customer_id = 7",
        IndexSuggestion(table="orders", columns=["customer_id"], include=["created_at"]),
    )
    assert result.after.uses_new_index
    assert "Index Only Scan" in result.after.node_types


def test_index_ddl_forms() -> None:
    assert IndexSuggestion(table="t", columns=["a"]).ddl == 'CREATE INDEX ON "t" ("a")'
    assert (
        IndexSuggestion(table="t", columns=["a", "b"], include=["c"], where="d > 0").ddl
        == 'CREATE INDEX ON "t" ("a", "b") INCLUDE ("c") WHERE d > 0'
    )
    assert (
        IndexSuggestion(table="t", columns=["j"], method="gin").create_ddl("n", if_not_exists=True)
        == 'CREATE INDEX IF NOT EXISTS "n" ON "t" USING gin ("j")'
    )


def test_index_names_are_distinct_per_variant() -> None:
    plain = IndexSuggestion(table="t", columns=["a"])
    partial = IndexSuggestion(table="t", columns=["a"], where="b = 1")
    gin = IndexSuggestion(table="t", columns=["a"], method="gin")
    names = {plain.suggested_name, partial.suggested_name, gin.suggested_name}
    assert len(names) == 3
    assert plain.suggested_name == "idx_t_a"


@pytest.mark.parametrize(
    "bad",
    [
        {"include": ["x"], "method": "gin"},
        {"where": "a = 1; DROP TABLE t"},
        {"where": "a IN (SELECT 1)"},
        {"where": "a = 1 -- c"},
        {"where": "pg_sleep(1) IS NULL"},
        {"where": "x" * 501},
        {"method": "gist"},
    ],
)
def test_unsafe_or_unsupported_index_options_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        IndexSuggestion.model_validate({"table": "t", "columns": ["a"], **bad})
