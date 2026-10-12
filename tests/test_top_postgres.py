from collections.abc import Iterator

import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer
from typer.testing import CliRunner

from pgsage.cli import app
from pgsage.context import top_queries

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
