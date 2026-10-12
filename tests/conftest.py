import json
import shutil
import subprocess
from collections.abc import Iterator
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg.sql import SQL
from testcontainers.community.postgres import PostgresContainer


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    needs_docker = [
        item
        for item in items
        if "pg_dsn" in getattr(item, "fixturenames", ()) or item.get_closest_marker("docker")
    ]
    if not needs_docker or _docker_available():
        return
    skip = pytest.mark.skip(
        reason="Docker is not available; run with a Docker daemon or -m 'not docker'"
    )
    for item in needs_docker:
        item.add_marker(skip)


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


def explain_json(conn: psycopg.Connection, sql: str, analyze: bool = True) -> str:
    options = "ANALYZE, BUFFERS, FORMAT JSON" if analyze else "FORMAT JSON"
    statement = SQL("EXPLAIN ({}) {}").format(
        SQL(cast("LiteralString", options)), SQL(cast("LiteralString", sql))
    )
    row = conn.execute(statement).fetchone()
    assert row is not None
    return json.dumps(row[0])
