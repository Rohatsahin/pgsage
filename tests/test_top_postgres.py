from collections.abc import Iterator

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from testcontainers.community.postgres import PostgresContainer
from typer.testing import CliRunner

from pgsage.cli import app
from pgsage.context import table_ancestors, table_info, top_queries

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def stats_dsn() -> Iterator[str]:
    container = PostgresContainer("postgres:17", driver=None).with_command(
        "postgres -c shared_preload_libraries=pg_stat_statements"
    )
    with container as pg:
        dsn = pg.get_connection_url()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION pg_stat_statements")
            conn.execute("CREATE TABLE t AS SELECT g AS id FROM generate_series(1, 50000) g")
            for value in (1, 2, 3):
                conn.execute("SELECT count(*) FROM t WHERE id > %s", (value,))
            conn.execute("UPDATE t SET id = id WHERE id = 1")
            conn.execute("CREATE DATABASE otherdb")
        with psycopg.connect(make_conninfo(dsn, dbname="otherdb"), autocommit=True) as other:
            other.execute("CREATE EXTENSION pg_stat_statements")
            other.execute(
                "SELECT count(*) FROM generate_series(1, 300000) WHERE 1 = 1 OFFSET 0 -- marker_other_db"
            )
            other.execute("SELECT 424242 AS marker_other_db_value")
        yield dsn


def test_top_queries_lists_only_selects_ranked_by_cost(stats_dsn: str) -> None:
    rows = top_queries(stats_dsn, limit=5)
    assert rows
    assert all(row["query"].lstrip().lower().startswith("select") for row in rows)
    assert "t WHERE id > $1" in rows[0]["query"]
    assert rows[0]["calls"] == 3


def test_top_command_prints_placeholder_hint(stats_dsn: str) -> None:
    result = CliRunner().invoke(app, ["top", "--dsn", stats_dsn])
    assert result.exit_code == 0
    assert "$1" in result.output


def test_top_without_extension_explains_how_to_enable(pg_dsn: str) -> None:
    result = CliRunner().invoke(app, ["top", "--dsn", pg_dsn])
    assert result.exit_code == 2
    assert "shared_preload_libraries" in result.output


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


def test_top_command_honours_limit_and_min_calls(stats_dsn: str) -> None:
    runner = CliRunner()
    one = runner.invoke(app, ["top", "--dsn", stats_dsn, "--limit", "1"])
    assert one.exit_code == 0
    assert one.output.count(" calls, ") == 1
    none = runner.invoke(app, ["top", "--dsn", stats_dsn, "--min-calls", "1000000"])
    assert none.exit_code == 0
    assert " calls, " not in none.output
