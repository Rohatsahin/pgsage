from collections.abc import Iterator

import psycopg
import pytest

from pgsage.verify import verify_rewrite

pytestmark = pytest.mark.docker


@pytest.fixture
def eq(pg_conn: psycopg.Connection) -> Iterator[None]:
    pg_conn.execute("DROP TABLE IF EXISTS eq")
    pg_conn.execute(
        "CREATE TABLE eq AS SELECT g AS id, g % 10 AS grp, ('{\"a\": ' || g || '}')::json AS doc "
        "FROM generate_series(1, 2000) g"
    )
    pg_conn.execute("ANALYZE eq")
    yield
    pg_conn.execute("DROP TABLE IF EXISTS eq")


def check(dsn: str, original: str, rewritten: str) -> tuple[bool | None, str | None]:
    result = verify_rewrite(dsn, original, rewritten)
    assert not result.improved or result.equivalent
    return result.equivalent, result.note


def test_identical_semantics_are_equivalent_even_for_json_columns(pg_dsn: str, eq: None) -> None:
    equivalent, _ = check(
        pg_dsn, "SELECT id, doc FROM eq WHERE grp = 3", "SELECT id, doc FROM eq WHERE 3 = grp"
    )
    assert equivalent is True


@pytest.mark.parametrize(
    ("original", "rewritten", "reason"),
    [
        ("SELECT id AS a FROM eq", "SELECT id AS b FROM eq", "columns"),
        ("SELECT id FROM eq", "SELECT id::numeric AS id FROM eq", "columns"),
        ("SELECT id, grp FROM eq", "SELECT grp, id FROM eq", "columns"),
        ("SELECT id FROM eq ORDER BY id DESC", "SELECT id FROM eq", "ordered"),
        ("SELECT id FROM eq WHERE grp = 1", "SELECT id FROM eq WHERE grp = 2", "rows"),
        ("SELECT id FROM eq LIMIT 5", "SELECT id FROM eq ORDER BY id LIMIT 5", "arbitrary"),
        ("SELECT random() FROM eq", "SELECT random() FROM eq", "volatile"),
    ],
)
def test_not_equivalent_or_unprovable(
    pg_dsn: str, eq: None, original: str, rewritten: str, reason: str
) -> None:
    equivalent, note = check(pg_dsn, original, rewritten)
    assert equivalent is False
    assert note is not None


def test_ordered_queries_are_compared_in_order(pg_dsn: str, eq: None) -> None:
    equivalent, _ = check(
        pg_dsn,
        "SELECT id FROM eq WHERE grp = 1 ORDER BY id",
        "SELECT id FROM eq WHERE grp = 1 ORDER BY id ASC",
    )
    assert equivalent is True
