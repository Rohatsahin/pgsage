import json
import shutil
import subprocess
from collections.abc import Iterator
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL
from testcontainers.community.postgres import PostgresContainer


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


DOCKER_FIXTURES = {"pg_dsn", "pg_conn", "stats_dsn"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    needs_docker = [
        item for item in items if DOCKER_FIXTURES & set(getattr(item, "fixturenames", ()))
    ]
    for item in needs_docker:
        item.add_marker(pytest.mark.docker)
    if not needs_docker or _docker_available():
        return
    reason = "Docker is not available; run with a Docker daemon or -m 'not docker'"
    for item in needs_docker:
        item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(autouse=True)
def isolated_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.chdir(home)
    for name in ("PGSAGE_CONFIG", "PGSAGE_CONNECTION"):
        monkeypatch.delenv(name, raising=False)
    for name in ("PGSERVICE", "PGHOST", "PGHOSTADDR", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        monkeypatch.delenv(name, raising=False)


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


@pytest.fixture
def victim(pg_conn: psycopg.Connection) -> Iterator[None]:
    pg_conn.execute("DROP TABLE IF EXISTS victim")
    pg_conn.execute("CREATE TABLE victim AS SELECT 1 AS id")
    yield
    pg_conn.execute("DROP TABLE IF EXISTS victim")


@pytest.fixture(scope="session")
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
