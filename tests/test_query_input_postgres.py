from pathlib import Path

import pytest
from typer.testing import CliRunner

from pgsage import cli
from tests.fakes import FakeClient, use_client
from tests.test_cli import REPLY

pytestmark = pytest.mark.docker

runner = CliRunner()
TAIL_MARKER = "end_of_the_long_query_marker"


def long_query() -> str:
    columns = ",\n       ".join(f"id AS column_{n}" for n in range(1, 700))
    return f"SELECT {columns},\n       id AS {TAIL_MARKER}\nFROM orders\nWHERE customer_id = 7"


def tune_args(dsn: str, *extra: str) -> list[str]:
    return ["tune", "--dsn", dsn, *extra]


def test_a_long_multiline_query_from_a_file_reaches_the_prompt_untruncated(
    pg_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(REPLY)
    use_client(monkeypatch, client)
    path = tmp_path / "long.sql"
    path.write_text(long_query())
    assert len(long_query()) > 15_000
    result = runner.invoke(cli.app, tune_args(pg_dsn, "--file", str(path), "--show-prompt"))
    assert result.exit_code == 0, result.output
    assert TAIL_MARKER in result.output
    assert client.prompts == []


def test_dry_run_reports_sizes_and_calls_no_model(
    pg_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(REPLY)
    use_client(monkeypatch, client)
    path = tmp_path / "q.sql"
    path.write_text("SELECT * FROM orders WHERE customer_id = 7")
    result = runner.invoke(cli.app, tune_args(pg_dsn, "-f", str(path), "--dry-run"))
    assert result.exit_code == 0
    assert "nothing was sent to a model" in result.output.lower()
    assert "system prompt:" in result.output and "tokens in total" in result.output
    assert "1 nodes" in result.output or "nodes, sent in full" in result.output
    assert "UNTRUSTED INPUT BEGINS" not in result.output
    assert client.prompts == []


def test_show_prompt_prints_exactly_what_would_be_sent_with_literals_redacted(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_client(monkeypatch, FakeClient(REPLY))
    query = "SELECT * FROM orders WHERE customer_id = 7 AND created_at > '2001-02-03'"
    result = runner.invoke(cli.app, tune_args(pg_dsn, "--sql", query, "--show-prompt"))
    assert result.exit_code == 0
    assert "UNTRUSTED INPUT BEGINS" in result.output
    assert "2001-02-03" not in result.output
    assert "__L1__" in result.output


def test_a_piped_query_works(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    use_client(monkeypatch, FakeClient(REPLY))
    result = runner.invoke(
        cli.app,
        tune_args(pg_dsn, "--dry-run"),
        input="SELECT * FROM orders\nWHERE customer_id = 7\n",
    )
    assert result.exit_code == 0, result.output
    assert "Prompt built" in result.output


def test_two_query_sources_exit_2(pg_dsn: str, tmp_path: Path) -> None:
    path = tmp_path / "q.sql"
    path.write_text("SELECT 1")
    result = runner.invoke(
        cli.app, tune_args(pg_dsn, "--sql", "SELECT 1", "--file", str(path), "--dry-run")
    )
    assert result.exit_code == 2
    assert "only one of" in result.output


def test_a_missing_query_file_is_a_clean_error(pg_dsn: str, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, tune_args(pg_dsn, "--file", str(tmp_path / "nope.sql")))
    assert result.exit_code == 2
    assert result.output.startswith("error:") and "Traceback" not in result.output


def test_an_oversized_query_gets_a_clear_message_not_silent_truncation(
    pg_dsn: str, tmp_path: Path
) -> None:
    path = tmp_path / "huge.sql"
    path.write_text("SELECT 1 /* " + "x" * 120_000 + " */ FROM orders WHERE customer_id = 7")
    result = runner.invoke(cli.app, tune_args(pg_dsn, "--file", str(path), "--dry-run"))
    assert result.exit_code == 2
    assert "characters; the limit is" in result.output


def test_verify_reads_its_query_from_a_file_too(pg_dsn: str, tmp_path: Path) -> None:
    path = tmp_path / "q.sql"
    path.write_text("SELECT *\nFROM orders\nWHERE customer_id = 7\n")
    result = runner.invoke(
        cli.app,
        [
            "verify",
            "--dsn",
            pg_dsn,
            "--file",
            str(path),
            "--table",
            "orders",
            "--columns",
            "customer_id",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "[PROVEN]" in result.output
