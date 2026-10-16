import io
import json
import stat
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from psycopg.conninfo import conninfo_to_dict
from typer.testing import CliRunner

from pgsage.cli import MAX_QUERY_BYTES, app, read_query
from pgsage.config import user_config_path

QUERY = "SELECT *\nFROM orders\nWHERE customer_id = 7"

runner = CliRunner()


class FakeStdin(io.TextIOWrapper):
    def __init__(self, data: bytes, tty: bool = False) -> None:
        super().__init__(io.BytesIO(data), encoding="utf-8")
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def pipe(monkeypatch: pytest.MonkeyPatch, data: bytes, tty: bool = False) -> None:
    monkeypatch.setattr(sys, "stdin", FakeStdin(data, tty))


def write_project(body: str) -> None:
    Path("pgsage.toml").write_text(body)


def write_user_config(body: str) -> None:
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def run(*args: str, input: str | None = None) -> Any:
    return runner.invoke(app, list(args), input=input)


def payload(result: Any) -> Any:
    assert result.exit_code in (0, 1), result.stderr
    return json.loads(result.stdout)


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


def test_a_terminal_with_no_source_is_an_error_not_an_editor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipe(monkeypatch, b"", tty=True)
    with pytest.raises(ValueError, match="--sql or --file"):
        read_query()


def test_two_sources_at_once_is_ambiguous(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only one of"):
        read_query(sql="SELECT 1", file=tmp_path / "q.sql")


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


def test_a_missing_file_is_an_os_error(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        read_query(file=tmp_path / "nope.sql")


def test_version_flag_prints_the_package_version() -> None:
    result = run("--version")
    assert result.exit_code == 0
    assert result.stdout.startswith("pgsage ")


def test_the_help_lists_exactly_the_agent_commands() -> None:
    output = run("--help").stdout
    for command in ("inspect", "verify", "top", "connections", "init", "install-skill"):
        assert command in output


@pytest.mark.parametrize("gone", ["tune", "eval", "explain"])
def test_the_model_driven_commands_no_longer_exist(gone: str) -> None:
    result = run(gone)
    assert result.exit_code == 2
    assert "No such command" in result.output


def test_verify_without_a_subcommand_prints_its_help() -> None:
    result = run("verify")
    assert "index" in result.output and "statistics" in result.output and "rewrite" in result.output


def test_inspect_prints_digest_and_tables_as_json(pg_dsn: str) -> None:
    found = payload(run("inspect", "--dsn", pg_dsn, "--sql", QUERY))
    assert found["digest"][0]["self_ms"] is not None
    assert found["baseline_ms"] is not None
    assert found["tables"][0]["name"] == "orders"
    assert "plan" not in found


def test_inspect_can_include_the_raw_plan(pg_dsn: str) -> None:
    found = payload(run("inspect", "--dsn", pg_dsn, "--sql", QUERY, "--raw-plan"))
    assert "Plan" in found["plan"]


def test_inspect_reads_a_multiline_query_from_stdin(pg_dsn: str) -> None:
    found = payload(run("inspect", "--dsn", pg_dsn, input=QUERY))
    assert found["tables"][0]["name"] == "orders"
    assert "query" not in found


def test_inspect_reads_a_query_file(pg_dsn: str, tmp_path: Path) -> None:
    path = tmp_path / "q.sql"
    path.write_text(QUERY)
    found = payload(run("inspect", "--dsn", pg_dsn, "--file", str(path)))
    assert found["tables"][0]["name"] == "orders"


def test_inspect_rejects_writes_with_a_friendly_error(pg_dsn: str) -> None:
    result = run("inspect", "--dsn", pg_dsn, "--sql", "DELETE FROM orders")
    assert result.exit_code == 2
    assert result.stderr.startswith("error:")
    assert result.stdout == ""


def test_a_missing_query_file_is_a_clean_error(pg_dsn: str, tmp_path: Path) -> None:
    result = run("inspect", "--dsn", pg_dsn, "--file", str(tmp_path / "nope.sql"))
    assert result.exit_code == 2
    assert result.stderr.startswith("error:")


def test_two_query_sources_exit_2(pg_dsn: str, tmp_path: Path) -> None:
    path = tmp_path / "q.sql"
    path.write_text(QUERY)
    result = run("inspect", "--dsn", pg_dsn, "--sql", QUERY, "--file", str(path))
    assert result.exit_code == 2
    assert "only one of" in result.stderr


def test_verify_index_proves_and_offers_the_apply_sql(pg_dsn: str) -> None:
    result = run(
        *("verify", "index", "--dsn", pg_dsn, "--table", "orders", "--columns", "customer_id"),
        *("--sql", QUERY),
    )
    verdict = payload(result)
    assert result.exit_code == 0
    assert verdict["improved"] is True
    assert verdict["speedup"] > 1
    assert verdict["after"]["uses_new_index"] is True
    assert verdict["apply"][0].startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS")


def test_verify_index_reads_its_query_from_a_file(pg_dsn: str, tmp_path: Path) -> None:
    path = tmp_path / "q.sql"
    path.write_text(QUERY)
    verdict = payload(
        run(
            *("verify", "index", "--dsn", pg_dsn, "--table", "orders", "--columns", "customer_id"),
            *("--file", str(path)),
        )
    )
    assert verdict["improved"] is True


def test_verify_index_exits_1_and_offers_nothing_when_not_proven(pg_dsn: str) -> None:
    result = run(
        *("verify", "index", "--dsn", pg_dsn, "--table", "orders", "--columns", "id"),
        *("--sql", QUERY),
    )
    verdict = payload(result)
    assert result.exit_code == 1
    assert verdict["improved"] is False
    assert verdict["apply"] == []


def test_verify_index_passes_include_and_where_through(pg_dsn: str) -> None:
    verdict = payload(
        run(
            *("verify", "index", "--dsn", pg_dsn, "--table", "orders", "--columns", "customer_id"),
            *("--include", "created_at", "--where", "customer_id < 50", "--sql", QUERY),
        )
    )
    assert "INCLUDE" in verdict["statement"] and "WHERE" in verdict["statement"]


def test_verify_index_rejects_an_unknown_method(pg_dsn: str) -> None:
    result = run(
        *("verify", "index", "--dsn", pg_dsn, "--table", "orders", "--columns", "id"),
        *("--method", "gist", "--sql", QUERY),
    )
    assert result.exit_code == 2


def test_verify_rejects_writes(pg_dsn: str) -> None:
    result = run(
        *("verify", "index", "--dsn", pg_dsn, "--table", "orders", "--columns", "id"),
        *("--sql", "DELETE FROM orders"),
    )
    assert result.exit_code == 2
    assert result.stderr.startswith("error:")


def test_a_bad_column_is_an_unproven_result_not_a_traceback(pg_dsn: str) -> None:
    result = run(
        *("verify", "index", "--dsn", pg_dsn, "--table", "orders", "--columns", "nope"),
        *("--sql", QUERY),
    )
    assert result.exit_code == 1
    verdict = json.loads(result.stdout)
    assert verdict["improved"] is False
    assert "nope" in verdict["error"] and verdict["apply"] == []


def test_verify_statistics_reports_a_statistics_proof(pg_dsn: str) -> None:
    result = run(
        *("verify", "statistics", "--dsn", pg_dsn, "--table", "orders"),
        *("--columns", "id,customer_id", "--kinds", "dependencies"),
        *("--sql", "SELECT * FROM orders WHERE id = 5 AND customer_id = 5"),
    )
    verdict = payload(result)
    assert verdict["kind"] == "statistics"
    assert "dependencies" in verdict["statement"]
    assert verdict["error"] is None


def test_verify_statistics_needs_two_columns(pg_dsn: str) -> None:
    result = run(
        *("verify", "statistics", "--dsn", pg_dsn, "--table", "orders", "--columns", "id"),
        *("--sql", QUERY),
    )
    assert result.exit_code == 2
    assert result.stderr.startswith("error:")


def test_verify_statistics_rejects_an_unknown_kind(pg_dsn: str) -> None:
    result = run(
        *("verify", "statistics", "--dsn", pg_dsn, "--table", "orders"),
        *("--columns", "id,customer_id", "--kinds", "bogus", "--sql", QUERY),
    )
    assert result.exit_code == 2
    assert result.stderr.startswith("error:")


def test_verify_rewrite_exits_1_for_an_identical_query(pg_dsn: str) -> None:
    result = run("verify", "rewrite", "--dsn", pg_dsn, "--sql", QUERY, "--rewrite", QUERY)
    verdict = payload(result)
    assert result.exit_code == 1
    assert "identical" in verdict["error"]


def test_verify_rewrite_reports_equivalence_for_a_different_query(pg_dsn: str) -> None:
    result = run(
        *("verify", "rewrite", "--dsn", pg_dsn, "--sql", QUERY),
        *("--rewrite", "SELECT * FROM orders WHERE customer_id = 7 AND id > 0"),
    )
    verdict = payload(result)
    assert verdict["kind"] == "rewrite"
    assert verdict["equivalent"] is True
    assert verdict["apply"] == []


def test_verify_rewrite_flags_a_rewrite_that_changes_the_result(pg_dsn: str) -> None:
    result = run(
        *("verify", "rewrite", "--dsn", pg_dsn, "--sql", QUERY),
        *("--rewrite", "SELECT * FROM orders WHERE customer_id = 8"),
    )
    verdict = payload(result)
    assert result.exit_code == 1
    assert verdict["equivalent"] is False


def test_verify_rewrite_reads_the_new_query_from_a_file(pg_dsn: str, tmp_path: Path) -> None:
    path = tmp_path / "new.sql"
    path.write_text("SELECT * FROM orders WHERE customer_id = 7 AND id > 0")
    verdict = payload(
        run("verify", "rewrite", "--dsn", pg_dsn, "--sql", QUERY, "--rewrite-file", str(path))
    )
    assert verdict["equivalent"] is True


def test_verify_rewrite_needs_exactly_one_new_query(pg_dsn: str, tmp_path: Path) -> None:
    neither = run("verify", "rewrite", "--dsn", pg_dsn, "--sql", QUERY)
    assert neither.exit_code == 2
    assert "exactly one of" in neither.stderr
    path = tmp_path / "new.sql"
    path.write_text(QUERY)
    both = run(
        *("verify", "rewrite", "--dsn", pg_dsn, "--sql", QUERY),
        *("--rewrite", QUERY, "--rewrite-file", str(path)),
    )
    assert both.exit_code == 2


def test_verify_rewrite_rejects_a_write(pg_dsn: str) -> None:
    result = run(
        "verify", "rewrite", "--dsn", pg_dsn, "--sql", QUERY, "--rewrite", "DELETE FROM orders"
    )
    assert result.exit_code == 2
    assert result.stderr.startswith("error:")


def test_dsn_and_connection_together_fail_cleanly() -> None:
    result = run("top", "--dsn", "postgresql://x/d", "--connection", "a")
    assert result.exit_code == 2
    assert "either --dsn or --connection" in result.stderr


def test_a_named_connection_reports_itself_on_stderr_only(pg_dsn: str) -> None:
    write_user_config(f'default_connection = "scratch"\n[connections.scratch]\ndsn = "{pg_dsn}"\n')
    result = run("inspect", "--sql", QUERY)
    assert result.exit_code == 0, result.stderr
    assert "using connection scratch" in result.stderr
    assert json.loads(result.stdout)["tables"][0]["name"] == "orders"


def test_protected_connections_refuse_every_proof(pg_dsn: str) -> None:
    write_user_config(f'[connections.prod]\ndsn = "{pg_dsn}"\nprotected = true\n')
    attempts = (
        ["verify", "index", "--table", "orders", "--columns", "id", "--sql", QUERY],
        [
            *("verify", "statistics", "--table", "orders", "--columns", "id,customer_id"),
            *("--sql", QUERY),
        ],
        ["verify", "rewrite", "--sql", QUERY, "--rewrite", QUERY + " "],
    )
    for args in attempts:
        result = run(*args, "-c", "prod")
        assert result.exit_code == 2, args
        assert "is protected" in result.stderr, args


def test_protected_connections_still_allow_inspect_and_top(stats_dsn: str) -> None:
    write_user_config(f'[connections.prod]\ndsn = "{stats_dsn}"\nprotected = true\n')
    assert run("inspect", "-c", "prod", "--sql", "SELECT * FROM t WHERE id = 5").exit_code == 0
    assert run("top", "-c", "prod").exit_code == 0


def test_top_prints_a_json_list_with_placeholders(stats_dsn: str) -> None:
    rows: list[dict[str, Any]] = payload(run("top", "--dsn", stats_dsn))
    assert "$1" in rows[0]["query"]
    assert {"calls", "total_ms", "mean_ms"} <= set(rows[0])


def test_top_without_extension_explains_how_to_enable(pg_dsn: str) -> None:
    result = run("top", "--dsn", pg_dsn)
    assert result.exit_code == 2
    assert "shared_preload_libraries" in result.stderr


def test_top_honours_limit_and_min_calls(stats_dsn: str) -> None:
    assert len(payload(run("top", "--dsn", stats_dsn, "--limit", "1"))) == 1
    assert payload(run("top", "--dsn", stats_dsn, "--min-calls", "1000000")) == []


def test_connections_lists_nothing_as_an_empty_json_list() -> None:
    assert payload(run("connections")) == []


def test_connections_json_lists_machine_readable_entries() -> None:
    write_project(
        'default_connection = "a"\n[connections.a]\nhost = "localhost"\nuser = "u"\ndbname = "d"\n'
        "protected = true\n"
    )
    (entry,) = payload(run("connections"))
    assert entry["name"] == "a" and entry["default"] and entry["protected"]
    assert entry["target"] == "u@localhost:5432/d"
    assert entry["source"].endswith("pgsage.toml")


def test_connections_test_reaches_a_real_server(pg_dsn: str) -> None:
    found = payload(run("connections", "test", "--dsn", pg_dsn))
    assert found["version"].startswith("PostgreSQL 17")
    assert found["read_only_session"] is False
    assert found["pg_stat_statements"] is False


def test_connections_test_sees_pg_stat_statements(stats_dsn: str) -> None:
    assert payload(run("connections", "test", "--dsn", stats_dsn))["pg_stat_statements"] is True


def test_connections_test_names_an_unreachable_server() -> None:
    write_project('[connections.dead]\nhost = "127.0.0.1"\nport = 1\n')
    result = run("connections", "test", "dead")
    assert result.exit_code == 2
    assert result.stderr.splitlines()[-1].startswith("error:")


def skill_text() -> str:
    return (files("pgsage") / "skill" / "SKILL.md").read_text(encoding="utf-8")


def test_install_skill_writes_the_packaged_skill_into_the_project() -> None:
    result = run("install-skill")
    path = Path.cwd() / ".claude" / "skills" / "pgsage" / "SKILL.md"
    assert result.exit_code == 0, result.stderr
    assert path.read_text(encoding="utf-8") == skill_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert str(path) in result.stdout


def test_install_skill_user_uses_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert run("install-skill", "--user").exit_code == 0
    assert (Path.home() / ".claude" / "skills" / "pgsage" / "SKILL.md").is_file()
    assert not (tmp_path / ".claude").exists()


def test_install_skill_dir_targets_another_agent(tmp_path: Path) -> None:
    target = tmp_path / "other-agent" / "skills"
    assert run("install-skill", "--dir", str(target)).exit_code == 0
    assert (target / "pgsage" / "SKILL.md").read_text(encoding="utf-8") == skill_text()


def test_install_skill_never_overwrites_without_force() -> None:
    assert run("install-skill").exit_code == 0
    path = Path.cwd() / ".claude" / "skills" / "pgsage" / "SKILL.md"
    path.write_text("customised")
    again = run("install-skill")
    assert again.exit_code == 2
    assert "already exists" in again.stderr
    assert path.read_text() == "customised"
    assert run("install-skill", "--force").exit_code == 0
    assert path.read_text(encoding="utf-8") == skill_text()


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


def test_summarize_digests_a_plan_from_a_file_or_stdin(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(PLAN))
    from_file = payload(run("summarize", "--file", str(plan)))
    from_stdin = payload(run("summarize", input=json.dumps(PLAN)))
    assert from_file == from_stdin
    assert from_file[0]["node"] == "Seq Scan"


def test_summarize_explains_how_to_get_a_json_plan() -> None:
    result = run("summarize", input="Seq Scan on orders  (cost=0.00..10.00)")
    assert result.exit_code == 2
    assert "FORMAT JSON" in result.stderr


def test_inspect_accepts_fetch_first(pg_dsn: str) -> None:
    result = run(
        "inspect",
        "--dsn",
        pg_dsn,
        "--sql",
        "SELECT * FROM orders ORDER BY id FETCH FIRST 3 ROWS ONLY",
    )
    assert result.exit_code == 0, result.stderr


def test_a_pasted_explain_prefix_gets_a_clear_message(pg_dsn: str) -> None:
    result = run("inspect", "--dsn", pg_dsn, "--sql", "EXPLAIN ANALYZE SELECT 1")
    assert result.exit_code == 2
    assert "remove the EXPLAIN keyword" in result.stderr


def test_an_explicit_flag_overrides_the_environment(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGSAGE_CONNECTION", "does-not-exist")
    result = run("inspect", "--dsn", pg_dsn, "--sql", "SELECT 1")
    assert result.exit_code == 0, result.stderr


def test_a_malformed_connection_string_is_not_echoed() -> None:
    result = run("inspect", "--dsn", "postgres://u:p%zzsecret@h/db", "--sql", "SELECT 1")
    assert result.exit_code == 2
    assert "secret" not in result.stderr and "secret" not in result.stdout


def test_verify_setting_reports_a_session_only_proof(pg_dsn: str) -> None:
    result = run(
        *("verify", "setting", "--dsn", pg_dsn, "--set", "work_mem=512MB", "--set", "jit=off"),
        *("--sql", "SELECT g FROM generate_series(1, 200000) g ORDER BY g DESC"),
    )
    verdict = payload(result)
    assert verdict["kind"] == "setting"
    assert verdict["error"] is None
    assert "SET work_mem = '512MB';" in verdict["apply"] or not verdict["improved"]


def test_verify_setting_rejects_a_setting_that_is_not_allowed(pg_dsn: str) -> None:
    result = run(
        *("verify", "setting", "--dsn", pg_dsn, "--set", "statement_timeout=0"),
        *("--sql", "SELECT 1"),
    )
    assert result.exit_code == 2
    assert "cannot be proven" in result.stderr


def test_verify_setting_needs_name_equals_value(pg_dsn: str) -> None:
    result = run(
        *("verify", "setting", "--dsn", pg_dsn, "--set", "work_mem"), *("--sql", "SELECT 1")
    )
    assert result.exit_code == 2
    assert "name=value" in result.stderr


def test_inspect_without_analyze_estimates_and_does_not_run_the_query(pg_dsn: str) -> None:
    result = run(
        *("inspect", "--dsn", pg_dsn, "--no-analyze"),
        *("--sql", "SELECT count(*) FROM orders, orders o2 WHERE orders.id = o2.id"),
    )
    found = payload(result)
    assert found["baseline_ms"] is None
    assert all(row["self_ms"] is None for row in found["digest"])
    assert found["digest"][0]["planned_rows"] >= 1


def test_a_query_over_the_time_limit_explains_the_way_out(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg

    from pgsage import context

    def cancelled(*args: object, **kwargs: object) -> None:
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(context, "run_explain", cancelled)
    result = run("inspect", "--dsn", pg_dsn, "--sql", "SELECT 1")
    assert result.exit_code == 2
    assert "--no-analyze" in result.stderr


def test_top_hides_pgsages_own_queries(stats_dsn: str) -> None:
    run(
        *("verify", "rewrite", "--dsn", stats_dsn, "--sql", "SELECT id FROM t WHERE id < 5"),
        *("--rewrite", "SELECT id FROM t WHERE id < 5 AND id > 0"),
    )
    run("inspect", "--dsn", stats_dsn, "--sql", "SELECT id FROM t WHERE id < 5")
    rows = payload(run("top", "--dsn", stats_dsn, "--limit", "100"))
    text = " ".join(row["query"] for row in rows)
    for own in ("EXCEPT ALL", "pg_settings", "pg_stat_user_tables", "AS q LIMIT 0"):
        assert own not in text
    assert all(isinstance(row["total_ms"], float) for row in rows)


def test_connections_test_accepts_the_connection_flag_too(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = conninfo_to_dict(pg_dsn)
    (Path.cwd() / "pgsage.toml").write_text(
        f'[connections.here]\nhost = "{info["host"]}"\nport = {info["port"]}\n'
        f'dbname = "{info["dbname"]}"\nuser = "{info["user"]}"\n'
        'password_env = "PGSAGE_TEST_PASSWORD"\n'
    )
    monkeypatch.setenv("PGSAGE_TEST_PASSWORD", str(info["password"]))
    flagged = payload(run("connections", "test", "-c", "here"))
    positional = payload(run("connections", "test", "here"))
    assert flagged["user"] == positional["user"]


def test_install_skill_rejects_dir_together_with_user(tmp_path: Path) -> None:
    result = run("install-skill", "--user", "--dir", str(tmp_path))
    assert result.exit_code == 2
    assert "either --dir or --user" in result.stderr


def test_a_rejected_setting_message_does_not_leak_validation_internals() -> None:
    result = run("verify", "setting", "--dsn", "host=x", "--set", "foo=1", "--sql", "SELECT 1")
    assert result.exit_code == 2
    assert "Value error" not in result.stderr and "cannot be proven" in result.stderr


def test_install_skill_never_writes_through_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "precious.txt"
    target.write_text("keep me")
    target.chmod(0o600)
    skills = tmp_path / "skills" / "pgsage"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").symlink_to(target)
    forced = run("install-skill", "--dir", str(tmp_path / "skills"), "--force")
    assert forced.exit_code == 2
    assert "symbolic link" in forced.stderr
    assert target.read_text() == "keep me"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_resolution_failure_does_not_echo_part_of_the_connection_string() -> None:
    result = run(
        "inspect",
        "--dsn",
        "postgresql://app:HUNTERpw@xx@nonexistent.invalid:5432/d",
        "--sql",
        "SELECT 1",
    )
    assert result.exit_code == 2
    assert "HUNTER" not in result.stderr and "xx@" not in result.stderr
