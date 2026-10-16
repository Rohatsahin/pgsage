import random
from collections.abc import Iterator

import psycopg
import pytest

from pgsage import verify as verify_module
from pgsage.context import get_plan
from pgsage.sqlscan import is_ambiguous, mask
from pgsage.verify import (
    UnsafeStatementError,
    ensure_read_only,
    ensure_safe_predicate,
    literal,
    run_explain,
    run_single,
    verify_rewrite,
)

TOKENS = [
    "SELECT 1",
    "SELECT",
    ";",
    "'",
    "''",
    '"',
    "--",
    "/*",
    "*/",
    "$$",
    "$a$",
    "$1$",
    "\r",
    "\n",
    " ",
    "x",
    "e'",
    "E'",
    "\\",
    "(",
    ")",
    "DROP TABLE t",
    "1",
    "U&'",
    'U&"',
]


SAMPLES = 4000


def samples(seed: int) -> Iterator[str]:
    rng = random.Random(seed)
    for _ in range(SAMPLES):
        yield "SELECT 1 " + "".join(rng.choice(TOKENS) for _ in range(rng.randint(1, 10)))


def exists(conn: psycopg.Connection, name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()
    return row == (True,)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_nothing_the_gate_accepts_is_ever_more_than_one_statement_to_postgres(
    pg_dsn: str, seed: int
) -> None:
    accepted = 0
    with psycopg.connect(pg_dsn) as conn:
        conn.read_only = True
        for sql in samples(seed):
            try:
                ensure_read_only(sql)
            except UnsafeStatementError:
                continue
            accepted += 1
            try:
                run_single(conn, literal(sql))
            except psycopg.Error as error:
                assert "multiple commands" not in str(error), f"lexer disagreement on {sql!r}"
            finally:
                conn.rollback()
    assert accepted > 50


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
    result = verify_rewrite(pg_dsn, "SELECT 1 AS a", "SELECT 1 AS a /* x */; DROP TABLE victim")
    assert result.error and not result.improved
    assert exists(pg_conn, "victim")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t",
        "UPDATE t SET id = 1",
        "SELECT 1; COMMIT; GRANT ALL ON victim TO PUBLIC",
        "SELECT 1; COMMIT; DO $$ BEGIN EXECUTE 'cr'||'eate table pwned()'; END $$",
        "SELECT 1; SELECT 2",
        "COMMIT",
        "DO $$ BEGIN NULL; END $$",
        "GRANT ALL ON t TO PUBLIC",
        "CALL proc()",
        "COPY t TO PROGRAM 'id'",
        "SELECT * FROM t FOR UPDATE",
        "SELECT * FROM t FOR NO KEY UPDATE",
        "SELECT * FROM t FOR KEY SHARE",
        "SELECT * INTO newtable FROM t",
        "SELECT set_config('x', 'y', false)",
        "SELECT pg_sleep(100)",
        "SELECT nextval('s')",
        "SELECT setval('s', 1)",
        "SELECT pg_terminate_backend(1)",
        "SELECT lo_import('/etc/passwd')",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT * FROM dblink('x', 'y') AS t(a int)",
        "SELECT pg_advisory_lock(1)",
        "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
        "WITH d AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM d",
        "SET ROLE postgres",
        "VACUUM t",
        "EXPLAIN ANALYZE DELETE FROM t",
        "SELECT 1 /* hidden */ ; /* x */ DROP TABLE t",
        "select 1;\ndrop table t",
        "",
    ],
)
def test_rejects_unsafe_input(sql: str) -> None:
    with pytest.raises(UnsafeStatementError):
        ensure_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT 1;",
        "  select * from t where id = 1  ;  ",
        "(SELECT 1)",
        "/* leading comment */ SELECT 1",
        "-- leading comment\nSELECT 1",
        "TABLE t",
        "VALUES (1), (2)",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SELECT * FROM t WHERE status = 'update'",
        "SELECT * FROM t WHERE note = 'a; DROP TABLE t'",
        "SELECT 1 -- update later",
        'SELECT "update" FROM t',
        "SELECT $$delete; commit$$",
        "SELECT E'it\\'s; DROP TABLE t'",
    ],
)
def test_accepts_single_read_only_statements(sql: str) -> None:
    ensure_read_only(sql)


def test_mask_hides_literals_and_comments_but_not_structure() -> None:
    assert mask("SELECT 'a;b', \"c;d\" FROM t -- x;y\nWHERE n = 5") == (
        "SELECT '', \"\" FROM t  \nWHERE n = 5"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'unterminated",
        'SELECT "unterminated',
        "SELECT 1 /* never closed",
        "SELECT 1 /* a /* nested */ b */",
        "SELECT 1 */",
        "SELECT 1 -- note\r; DROP TABLE t",
        "SELECT 1 -- note\r\n; DROP TABLE t",
    ],
)
def test_ambiguous_lexing_fails_closed(sql: str) -> None:
    with pytest.raises(UnsafeStatementError):
        ensure_read_only(sql)


@pytest.mark.parametrize(
    ("sql", "ambiguous"),
    [
        ("SELECT 'a'", False),
        ("SELECT 'it''s'", False),
        ('SELECT "a""b"', False),
        ("SELECT $$x$$, $tag$y$tag$", False),
        ("SELECT 1 /* ok */", False),
        ("SELECT 'a", True),
        ("SELECT 1 /*", True),
        ("SELECT 1 */ 2", True),
    ],
)
def test_is_ambiguous(sql: str, ambiguous: bool) -> None:
    assert is_ambiguous(sql) is ambiguous


def test_a_carriage_return_ends_a_line_comment_exactly_like_postgres() -> None:
    assert "; X" in mask("SELECT 1 -- hidden?\r; X")
    assert "; X" in mask("SELECT 1 -- hidden?\n; X")
    assert "; X" not in mask("SELECT 1 -- all hidden; X")


def test_a_dollar_tag_cannot_start_with_a_digit() -> None:
    assert mask("SELECT $1$ ; SELECT $1$") == "SELECT $1$ ; SELECT $1$"
    assert mask("SELECT $a1$ ; DROP $a1$ FROM t") == "SELECT '' FROM t"


def test_an_e_prefix_only_counts_at_a_token_start() -> None:
    assert mask("SELECT name'x'") == "SELECT name''"
    assert mask("SELECT e'a\\'b'") == "SELECT ''"
    assert mask("SELECT 'a\\' ; DROP") != "SELECT ''"


@pytest.mark.parametrize(
    "predicate",
    ["a = 'x", "a = 1 /* open", "a = 1 */", 'a = "x', "a = 1 -- c\r"],
)
def test_the_predicate_validator_fails_closed_too(predicate: str) -> None:
    with pytest.raises(ValueError):
        ensure_safe_predicate(predicate)
