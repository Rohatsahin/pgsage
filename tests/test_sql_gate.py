import pytest

from pgsage.sqlscan import mask, redact_condition
from pgsage.verify import UnsafeStatementError, ensure_read_only


@pytest.mark.parametrize(
    "sql",
    [
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
    ("text", "expected"),
    [
        ("(a = 'x'::text)", "(a = ?::text)"),
        ("(a = $$secret$$)", "(a = ?)"),
        ("(a = $tag$se'cret$tag$)", "(a = ?)"),
        ("(a = E'it\\'s secret')", "(a = ?)"),
        ("(a > 1e5) AND (b < 1.5e-3)", "(a > ?) AND (b < ?)"),
        ("(a = 5) -- it's secret", "(a = ?)  "),
        ("(col1 = 2)", "(col1 = ?)"),
    ],
)
def test_redact_condition(text: str, expected: str) -> None:
    assert redact_condition(text) == expected
