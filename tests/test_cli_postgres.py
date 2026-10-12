import json
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from pgsage import cli
from pgsage.verify import literal
from tests.fakes import FakeClient, use_client
from tests.test_cli import REPLY

pytestmark = pytest.mark.docker

runner = CliRunner()


def test_tune_prints_proven_index(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    use_client(monkeypatch, FakeClient(REPLY))
    result = runner.invoke(
        cli.app,
        ["tune", "--dsn", pg_dsn, "--sql", "SELECT * FROM orders WHERE customer_id = 7"],
    )
    assert result.exit_code == 0
    assert "[PROVEN]" in result.output


def test_verify_rejects_writes(pg_dsn: str) -> None:
    result = runner.invoke(
        cli.app,
        [
            "verify",
            "--dsn",
            pg_dsn,
            "--sql",
            "DELETE FROM orders",
            "--table",
            "orders",
            "--columns",
            "id",
        ],
    )
    assert result.exit_code == 2


def test_tune_report_dir_writes_a_report_json_and_a_runnable_script(
    pg_dsn: str, pg_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_client(monkeypatch, FakeClient(REPLY))
    result = runner.invoke(
        cli.app,
        [
            "tune",
            "--dsn",
            pg_dsn,
            "--sql",
            "SELECT * FROM orders WHERE customer_id = 7",
            "--report-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    assert result.output.count("wrote ") == 3
    markdown = next(tmp_path.glob("*.md")).read_text()
    assert "## Proven changes" in markdown and "Baseline execution time" in markdown
    data = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert data["changes"][0]["proven"] is True
    script = next(tmp_path.glob("*.sql")).read_text()
    statements = [
        part.strip()
        for part in "\n".join(
            line for line in script.splitlines() if not line.startswith("--")
        ).split(";")
        if part.strip()
    ]
    try:
        for statement in statements:
            pg_conn.execute(literal(statement))
        row = pg_conn.execute(
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'idx_orders_customer_id'"
        ).fetchone()
        assert row == (1,)
        for statement in statements:
            pg_conn.execute(literal(statement))
    finally:
        pg_conn.execute("DROP INDEX IF EXISTS idx_orders_customer_id")


def test_tune_report_dir_can_avoid_concurrently(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_client(monkeypatch, FakeClient(REPLY))
    runner.invoke(
        cli.app,
        [
            "tune",
            "--dsn",
            pg_dsn,
            "--sql",
            "SELECT * FROM orders WHERE customer_id = 7",
            "--report-dir",
            str(tmp_path),
            "--no-concurrently",
        ],
    )
    assert "CONCURRENTLY" not in next(tmp_path.glob("*.sql")).read_text()


def test_eval_oracle_command_passes(pg_dsn: str) -> None:
    result = runner.invoke(cli.app, ["eval", "--dsn", pg_dsn, "--oracle"])
    assert result.exit_code == 0
    assert "5/5 passed" in result.output


def test_openai_provider_requires_a_model(pg_dsn: str) -> None:
    result = runner.invoke(
        cli.app, ["tune", "--dsn", pg_dsn, "--sql", "SELECT 1", "--provider", "openai"]
    )
    assert result.exit_code == 2
    assert "--model is required" in result.output
