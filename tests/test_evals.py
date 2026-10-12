import psycopg
from typer.testing import CliRunner

from pgsage import cli
from pgsage.evals import CASES, EvalCase, OracleClient, run_evals

runner = CliRunner()


class DoNothingClient:
    def complete(self, system: str, prompt: str) -> str:
        return '{"summary": "ok", "root_cause": "none", "actions": []}'


def do_nothing(case: EvalCase) -> DoNothingClient:
    return DoNothingClient()


def test_eval_oracle_command_passes(pg_dsn: str) -> None:
    result = runner.invoke(cli.app, ["eval", "--dsn", pg_dsn, "--oracle"])
    assert result.exit_code == 0
    assert "5/5 passed" in result.output


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


def test_eval_never_touches_real_tables_with_the_same_names(
    pg_dsn: str, pg_conn: psycopg.Connection
) -> None:
    pg_conn.execute("DROP TABLE IF EXISTS ev_orders")
    pg_conn.execute("CREATE TABLE ev_orders AS SELECT 1 AS precious")
    try:
        outcomes = run_evals(OracleClient, pg_dsn, CASES[:1])
        row = pg_conn.execute("SELECT precious FROM ev_orders").fetchone()
    finally:
        pg_conn.execute("DROP TABLE IF EXISTS ev_orders")
    assert outcomes[0].passed
    assert row == (1,)
    left = pg_conn.execute(
        "SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'pgsage_eval_%'"
    ).fetchone()
    assert left == (0,)


def test_eval_setup_failure_is_reported_not_raised(pg_dsn: str) -> None:
    broken = CASES[0].model_copy(update={"setup": ("CREATE TABLE nope (",)})
    outcomes = run_evals(OracleClient, pg_dsn, [broken])
    assert not outcomes[0].passed
    assert outcomes[0].detail.startswith("error:")
