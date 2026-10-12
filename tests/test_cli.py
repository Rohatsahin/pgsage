import io
import json
import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from pgsage import cli
from pgsage.ai import ExplanationError
from pgsage.cli import MAX_QUERY_BYTES, app, read_query
from pgsage.cli import make_client as real_make_client
from pgsage.verify import literal
from tests.fakes import FakeClient, use_client

PLAN = [
    {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Filter": "(customer_id = 7)",
            "Startup Cost": 0.0,
            "Total Cost": 10.0,
            "Plan Rows": 2000,
            "Actual Rows": 2000,
            "Actual Loops": 1,
            "Actual Total Time": 20.0,
            "Rows Removed by Filter": 198000,
        },
        "Execution Time": 20.0,
    }
]


REPLY = json.dumps(
    {
        "summary": "Seq scan dominates",
        "root_cause": "Missing index",
        "actions": [
            {
                "description": "Add index",
                "sql": None,
                "index": {"table": "orders", "columns": ["customer_id"]},
            }
        ],
    }
)


runner = CliRunner()


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient(REPLY)
    use_client(monkeypatch, client)
    return client


QUERY = "SELECT *\nFROM orders\nWHERE customer_id = 7"


class FakeStdin(io.TextIOWrapper):
    def __init__(self, data: bytes) -> None:
        super().__init__(io.BytesIO(data), encoding="utf-8")


def set_tty(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    def is_tty() -> bool:
        return value

    monkeypatch.setattr(cli, "_stdin_is_tty", is_tty)


def fake_editor(monkeypatch: pytest.MonkeyPatch, typed: str | None) -> None:
    def edit(text: str, extension: str) -> str | None:
        return None if typed is None else text + typed

    monkeypatch.setattr(cli.click, "edit", edit)


def pipe(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", FakeStdin(data))
    set_tty(monkeypatch, False)


TAIL_MARKER = "end_of_the_long_query_marker"


def long_query() -> str:
    columns = ",\n       ".join(f"id AS column_{n}" for n in range(1, 700))
    return f"SELECT {columns},\n       id AS {TAIL_MARKER}\nFROM orders\nWHERE customer_id = 7"


def tune_args(dsn: str, *extra: str) -> list[str]:
    return ["tune", "--dsn", dsn, *extra]


def test_explain_from_file(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(PLAN))
    result = runner.invoke(cli.app, ["explain", str(path)])
    assert result.exit_code == 0
    assert "Seq scan dominates" in result.output
    assert 'CREATE INDEX ON "orders" ("customer_id")' in result.output


def test_explain_from_stdin() -> None:
    result = runner.invoke(cli.app, ["explain"], input=json.dumps(PLAN))
    assert result.exit_code == 0
    assert "Cause: Missing index" in result.output


def test_explain_invalid_input_exits_2() -> None:
    result = runner.invoke(cli.app, ["explain"], input="not json")
    assert result.exit_code == 2


def test_explain_bad_model_reply_exits_2(fake_llm: FakeClient) -> None:
    fake_llm.reply = "I cannot help"
    result = runner.invoke(cli.app, ["explain"], input=json.dumps(PLAN))
    assert result.exit_code == 2


def test_explain_prints_evidence_confidence_and_ignored_actions(fake_llm: FakeClient) -> None:
    reply: dict[str, Any] = {
        "summary": "Seq scan dominates",
        "root_cause": "Missing index",
        "evidence": ["Seq Scan on orders: 300000 rows read"],
        "actions": [
            {
                "description": "Add index",
                "confidence": "high",
                "expected_effect": "avoids reading 300000 rows",
                "index": {"table": "orders", "columns": ["customer_id"]},
            },
            {"description": "Tune memory", "sql": "SET work_mem = '64MB'"},
            {"description": "bad", "index": {"table": "orders", "columns": []}},
        ],
    }
    fake_llm.reply = json.dumps(reply)
    result = runner.invoke(cli.app, ["explain"], input=json.dumps(PLAN))
    assert result.exit_code == 0
    assert "evidence: Seq Scan on orders: 300000 rows read" in result.output
    assert "[high confidence]" in result.output
    assert "expect: avoids reading 300000 rows" in result.output
    assert "ignored: action 3 rejected" in result.output
    assert "not verified" in result.output and "SET work_mem" in result.output


def test_explain_prints_token_and_cache_usage_when_the_client_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(REPLY)
    client.usage_log = [  # pyright: ignore[reportAttributeAccessIssue]
        {
            "input_tokens": 400,
            "cache_read_input_tokens": 3000,
            "cache_creation_input_tokens": 0,
            "output_tokens": 120,
        },
        {
            "input_tokens": 50,
            "cache_read_input_tokens": 3400,
            "cache_creation_input_tokens": 10,
            "output_tokens": 80,
        },
    ]
    use_client(monkeypatch, client)
    result = runner.invoke(cli.app, ["explain"], input=json.dumps(PLAN))
    assert "450 input, 6400 read from cache, 10 written to cache, 200 output" in result.output


def test_make_client_rejects_an_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        real_make_client("some-model", "bogus")


def test_explain_exits_2_when_the_model_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class Declining:
        def complete(self, system: str, prompt: str) -> str:
            raise ExplanationError("the model declined the request (category: cyber)")

    def make_declining(
        model: str | None,
        provider: str = "anthropic",
        base_url: str | None = None,
        effort: str | None = "high",
    ) -> Declining:
        return Declining()

    monkeypatch.setattr(cli, "make_client", make_declining)
    result = runner.invoke(cli.app, ["explain"], input=json.dumps(PLAN))
    assert result.exit_code == 2
    assert "declined" in result.output


def test_an_unknown_provider_or_effort_is_a_usage_error() -> None:
    bad_provider = runner.invoke(
        cli.app, ["explain", "--provider", "bogus"], input=json.dumps(PLAN)
    )
    bad_effort = runner.invoke(cli.app, ["explain", "--effort", "bogus"], input=json.dumps(PLAN))
    assert bad_provider.exit_code == 2 and bad_effort.exit_code == 2
    assert "bogus" in bad_provider.output + bad_effort.output


def test_a_missing_plan_file_is_an_error_not_a_traceback(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["explain", str(tmp_path / "nope.json")])
    assert result.exit_code == 2
    assert result.output.startswith("error:") and "Traceback" not in result.output


def test_version_flag_prints_the_package_version() -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("pgsage ")


def test_every_command_has_a_description_in_the_help() -> None:
    output = runner.invoke(cli.app, ["--help"]).output
    for command in ("explain", "tune", "verify", "top", "eval"):
        row = next(
            line for line in output.splitlines() if line.lstrip("│ ").startswith(f"{command} ")
        )
        assert len(row.lstrip("│ ")[len(command) :].strip(" │")) > 10, command


def test_the_eval_help_describes_what_it_really_does() -> None:
    output = runner.invoke(cli.app, ["eval", "--help"]).output
    assert "pgsage_eval_" in output and "EMPTY" not in output


def test_explain_labels_its_advice_as_unverified(fake_llm: FakeClient) -> None:
    result = runner.invoke(cli.app, ["explain"], input=json.dumps(PLAN))
    assert result.exit_code == 0
    assert "Unverified: nothing above was measured" in result.output
    assert "pgsage tune" in result.output


def test_explain_warns_when_the_plan_has_no_timings(fake_llm: FakeClient) -> None:
    plain = [{"Plan": {"Node Type": "Seq Scan", "Total Cost": 10.0, "Plan Rows": 5}}]
    result = runner.invoke(cli.app, ["explain"], input=json.dumps(plain))
    assert result.exit_code == 0
    assert "no ANALYZE timings" in result.output
    analyzed = runner.invoke(cli.app, ["explain"], input=json.dumps(PLAN))
    assert "no ANALYZE timings" not in analyzed.output


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


def test_openai_provider_requires_a_model(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "make_client", real_make_client)
    result = runner.invoke(
        cli.app, ["tune", "--dsn", pg_dsn, "--sql", "SELECT 1", "--provider", "openai"]
    )
    assert result.exit_code == 2
    assert "--model is required" in result.output


def test_inline_sql_is_returned_trimmed() -> None:
    assert read_query(sql="  SELECT 1  \n") == "SELECT 1"


def test_a_file_keeps_its_newlines(tmp_path: Path) -> None:
    path = tmp_path / "q.sql"
    path.write_text(QUERY)
    assert read_query(file=path) == QUERY


def test_a_utf8_bom_from_a_windows_editor_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "q.sql"
    path.write_bytes(b"\xef\xbb\xbfSELECT 1")
    assert read_query(file=path) == "SELECT 1"


def test_dash_reads_the_query_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe(monkeypatch, QUERY.encode())
    assert read_query(file=Path("-")) == QUERY


def test_a_piped_query_is_used_when_no_source_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe(monkeypatch, QUERY.encode())
    assert read_query() == QUERY


def test_a_terminal_with_no_source_opens_the_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    set_tty(monkeypatch, True)
    fake_editor(monkeypatch, "SELECT 42\n")
    assert read_query() == "SELECT 42"


def test_the_edit_flag_opens_the_editor_even_when_input_is_piped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipe(monkeypatch, b"ignored")
    fake_editor(monkeypatch, "SELECT 7")
    assert read_query(edit=True) == "SELECT 7"


def test_the_editor_header_never_reaches_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_editor(monkeypatch, "SELECT 1")
    assert "save and close" not in read_query(edit=True)


def test_closing_the_editor_without_saving_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_editor(monkeypatch, None)
    with pytest.raises(ValueError, match="without saving"):
        read_query(edit=True)


@pytest.mark.parametrize("combination", [("sql", "file"), ("sql", "edit"), ("file", "edit")])
def test_two_sources_at_once_is_ambiguous(combination: tuple[str, str], tmp_path: Path) -> None:
    kwargs = {"sql": "SELECT 1", "file": tmp_path / "q.sql", "edit": True}
    chosen = {key: kwargs[key] for key in combination}
    with pytest.raises(ValueError, match="only one of"):
        read_query(**chosen)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("empty", ["", "   ", "\n\n\t\n"])
def test_an_empty_query_is_rejected(empty: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        read_query(sql=empty)


def test_a_binary_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "q.bin"
    path.write_bytes(b"SELECT \x00\x01\x02")
    with pytest.raises(ValueError, match="binary"):
        read_query(file=path)


def test_a_file_that_is_not_utf8_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "q.sql"
    path.write_bytes(b"SELECT '\xff\xfe'")
    with pytest.raises(ValueError, match="UTF-8"):
        read_query(file=path)


def test_an_enormous_file_is_refused_before_it_is_read(tmp_path: Path) -> None:
    path = tmp_path / "big.sql"
    path.write_bytes(b"x" * (MAX_QUERY_BYTES + 1))
    with pytest.raises(ValueError, match="larger than"):
        read_query(file=path)


def test_an_enormous_stdin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe(monkeypatch, b"x" * (MAX_QUERY_BYTES + 1))
    with pytest.raises(ValueError, match="larger than"):
        read_query(file=Path("-"))


def test_a_missing_file_is_an_os_error_the_cli_reports(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        read_query(file=tmp_path / "nope.sql")


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


def test_top_command_prints_placeholder_hint(stats_dsn: str) -> None:
    result = CliRunner().invoke(app, ["top", "--dsn", stats_dsn])
    assert result.exit_code == 0
    assert "$1" in result.output


def test_top_without_extension_explains_how_to_enable(pg_dsn: str) -> None:
    result = CliRunner().invoke(app, ["top", "--dsn", pg_dsn])
    assert result.exit_code == 2
    assert "shared_preload_libraries" in result.output


def test_top_command_honours_limit_and_min_calls(stats_dsn: str) -> None:
    runner = CliRunner()
    one = runner.invoke(app, ["top", "--dsn", stats_dsn, "--limit", "1"])
    assert one.exit_code == 0
    assert one.output.count(" calls, ") == 1
    none = runner.invoke(app, ["top", "--dsn", stats_dsn, "--min-calls", "1000000"])
    assert none.exit_code == 0
    assert " calls, " not in none.output
