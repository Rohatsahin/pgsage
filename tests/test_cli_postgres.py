from pathlib import Path

import pytest
from typer.testing import CliRunner

from pgsage import cli
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


def test_tune_apply_dir_writes_only_proven_migrations(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
            "--apply-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    files = list(tmp_path.glob("V*__idx_orders_customer_id.sql"))
    assert len(files) == 1
    assert files[0].read_text().startswith("CREATE INDEX IF NOT EXISTS")


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
