from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from pgsage import context as context_module
from pgsage import plan_digest
from pgsage.context import get_plan, table_ancestors, table_info, tables_in_plan, top_queries


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


QUERY = "SELECT * FROM orders WHERE customer_id = 7"


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


@pytest.fixture
def matview(pg_conn: psycopg.Connection) -> Iterator[None]:
    pg_conn.execute("DROP MATERIALIZED VIEW IF EXISTS mv")
    pg_conn.execute("CREATE MATERIALIZED VIEW mv AS SELECT id, customer_id FROM orders")
    pg_conn.execute("ANALYZE mv")
    yield
    pg_conn.execute("DROP MATERIALIZED VIEW IF EXISTS mv")


class Spy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
        self.calls = 0
        original: Callable[..., None] = module.begin_guarded

        def wrapper(conn: Any) -> None:
            self.calls += 1
            original(conn)

        monkeypatch.setattr(module, "begin_guarded", wrapper)


def test_get_plan_returns_analyzed_plan(pg_dsn: str) -> None:
    result = get_plan(pg_dsn, "SELECT * FROM orders WHERE customer_id = 7")
    assert result.analyzed
    assert tables_in_plan(result) == [("public", "orders")]
    digest = plan_digest(result)
    assert digest[0]["relation"] == "orders"
    assert digest[0]["rows_removed_by_filter"] == 198000


def test_table_info_reports_columns_size_and_stats(pg_dsn: str) -> None:
    info = table_info(pg_dsn, "orders")
    assert [c["column_name"] for c in info["columns"]] == ["id", "customer_id", "created_at"]
    assert info["size"]["estimated_rows"] == 200000
    stats = {s["column_name"]: s for s in info["column_stats"]}
    assert stats["customer_id"]["n_distinct"] == 100


def test_table_info_unknown_table(pg_dsn: str) -> None:
    with pytest.raises(ValueError, match="not found"):
        table_info(pg_dsn, "nope")


def test_non_public_schema_is_resolved_from_the_plan(pg_dsn: str, app_schema: None) -> None:
    plan = get_plan(pg_dsn, "SELECT * FROM app.items WHERE k = 3")
    assert tables_in_plan(plan) == [("app", "items")]
    assert table_info(pg_dsn, "items", "app")["table"] == "app.items"


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


def test_a_partition_knows_its_parent(pg_dsn: str, partitioned: None) -> None:
    assert table_ancestors(pg_dsn, "pt1") == [("public", "pt")]
    assert table_ancestors(pg_dsn, "pt") == []


def test_materialized_views_are_visible_to_table_info(pg_dsn: str, matview: None) -> None:
    info = table_info(pg_dsn, "mv")
    assert [c["column_name"] for c in info["columns"]] == ["id", "customer_id"]


def test_every_lookup_gets_timeouts_too(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(monkeypatch, context_module)
    get_plan(pg_dsn, QUERY)
    table_info(pg_dsn, "orders")
    table_ancestors(pg_dsn, "orders")
    assert spy.calls == 3


def test_top_queries_lists_only_selects_ranked_by_cost(stats_dsn: str) -> None:
    rows = top_queries(stats_dsn, limit=5)
    assert rows
    assert all(row["query"].lstrip().lower().startswith("select") for row in rows)
    assert "t WHERE id > $1" in rows[0]["query"]
    assert rows[0]["calls"] == 3


def test_queries_from_other_databases_are_not_listed(stats_dsn: str) -> None:
    rows = top_queries(stats_dsn, limit=50)
    assert not any("marker_other_db" in row["query"] for row in rows)
    other = top_queries(make_conninfo(stats_dsn, dbname="otherdb"), limit=50)
    assert any("marker_other_db" in row["query"] for row in other)


def test_pgsages_own_catalog_lookups_do_not_pollute_the_ranking(stats_dsn: str) -> None:
    table_info(stats_dsn, "t")
    table_ancestors(stats_dsn, "t")
    rows = top_queries(stats_dsn, limit=100)
    noise = (
        "pg_attribute",
        "pg_inherits",
        "pg_indexes",
        "pg_stats",
        "pg_class",
        "information_schema",
    )
    assert not [row["query"] for row in rows if any(word in row["query"] for word in noise)]


def test_top_limit_is_capped_and_validated(stats_dsn: str) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        top_queries(stats_dsn, limit=-1)
    assert len(top_queries(stats_dsn, limit=10_000)) <= 100


def test_min_calls_filters_rarely_run_queries(stats_dsn: str) -> None:
    assert top_queries(stats_dsn, limit=10, min_calls=1_000_000) == []
