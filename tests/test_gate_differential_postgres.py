import random
from collections.abc import Iterator

import psycopg
import pytest

from pgsage.verify import UnsafeStatementError, ensure_read_only, literal, run_single

pytestmark = pytest.mark.docker

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
