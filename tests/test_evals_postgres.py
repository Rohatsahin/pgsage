import psycopg
import pytest

from pgsage.evals import CASES, EvalCase, OracleClient, run_evals

pytestmark = pytest.mark.docker


class DoNothingClient:
    def complete(self, system: str, prompt: str) -> str:
        return '{"summary": "ok", "root_cause": "none", "actions": []}'


def do_nothing(case: EvalCase) -> DoNothingClient:
    return DoNothingClient()


def test_oracle_answers_pass_every_case(pg_dsn: str) -> None:
    outcomes = run_evals(OracleClient, pg_dsn)
    failed = [o for o in outcomes if not o.passed]
    assert failed == [], failed
    assert len(outcomes) == len(CASES)


def test_a_model_that_never_helps_fails_only_the_fix_cases(pg_dsn: str) -> None:
    outcomes = {o.name: o for o in run_evals(do_nothing, pg_dsn)}
    for case in CASES:
        assert outcomes[case.name].passed == (case.expectation == "no_change")


def test_eval_leaves_no_tables_behind(pg_dsn: str, pg_conn: psycopg.Connection) -> None:
    run_evals(OracleClient, pg_dsn)
    row = pg_conn.execute("SELECT count(*) FROM pg_tables WHERE tablename LIKE 'ev\\_%'").fetchone()
    assert row == (0,)
