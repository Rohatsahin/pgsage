import json
from collections.abc import Iterator
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg.sql import SQL
from testcontainers.community.postgres import PostgresContainer

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:17", driver=None) as pg:
        dsn = pg.get_connection_url()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "CREATE TABLE orders AS SELECT g AS id, g % 100 AS customer_id, "
                "now() - (g || ' seconds')::interval AS created_at "
                "FROM generate_series(1, 200000) g"
            )
            conn.execute("ANALYZE orders")
        yield dsn


@pytest.fixture(scope="session")
def pg_conn(pg_dsn: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def load_fixture():
    def _load(name: str) -> str:
        return (FIXTURES / name).read_text()

    return _load


def explain_json(conn: psycopg.Connection, sql: str, analyze: bool = True) -> str:
    options = "ANALYZE, BUFFERS, FORMAT JSON" if analyze else "FORMAT JSON"
    statement = SQL("EXPLAIN ({}) {}").format(
        SQL(cast("LiteralString", options)), SQL(cast("LiteralString", sql))
    )
    row = conn.execute(statement).fetchone()
    assert row is not None
    return json.dumps(row[0])
