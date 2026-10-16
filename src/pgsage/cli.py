from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any

import psycopg
import typer

from pgsage.config import (
    PROJECT_FILE,
    TEMPLATE,
    ConfigError,
    Target,
    load_config,
    resolve_target,
    user_config_path,
)
from pgsage.context import inspect_query, top_queries
from pgsage.db import connect, describe_error
from pgsage.plan import parse_explain, plan_digest
from pgsage.verify import (
    IndexMethod,
    IndexSuggestion,
    SettingSuggestion,
    StatsSuggestion,
    VerifyResult,
    verify_index,
    verify_rewrite,
    verify_setting,
    verify_statistics,
)

app = typer.Typer(
    help="Plan facts and proofs for PostgreSQL query tuning, built for AI agents. "
    "Every command prints JSON.",
    no_args_is_help=True,
    add_completion=False,
)
verify_app = typer.Typer(
    help="Prove a change: apply it, measure it, roll it back.", no_args_is_help=True
)
connections_app = typer.Typer(
    help="Named database connections. Without a subcommand, lists them (as JSON)."
)
app.add_typer(verify_app, name="verify")
app.add_typer(connections_app, name="connections")

MAX_QUERY_BYTES = 1_000_000
SKILL_NAME = "pgsage"
PRIVATE_FILE = 0o600
PUBLIC_FILE = 0o644

DsnOption = Annotated[
    str | None,
    typer.Option(
        help="Connection string (or set PGSAGE_DSN). Prefer --connection: a password here "
        "shows up in `ps`",
    ),
]
ConnectionOption = Annotated[
    str | None,
    typer.Option(
        "--connection",
        "-c",
        help="Named connection from pgsage.toml or ~/.config/pgsage/config.toml "
        "(or set PGSAGE_CONNECTION)",
    ),
]
SqlOption = Annotated[str | None, typer.Option("--sql", help="The SELECT, given inline")]
FileOption = Annotated[
    Path | None, typer.Option("--file", "-f", help="Read the SELECT from a file (- for stdin)")
]


def _print_version(value: bool) -> None:
    if value:
        try:
            typer.echo(f"pgsage {version('pgsage')}")
        except PackageNotFoundError:
            typer.echo("pgsage (not installed)")
        raise typer.Exit()


@app.callback()
def main(
    show_version: Annotated[
        bool,
        typer.Option("--version", callback=_print_version, is_eager=True, help="Show the version"),
    ] = False,
) -> None:
    pass


def _print_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


def _fail(error: Exception, code: int = 2) -> typer.Exit:
    typer.echo(f"error: {describe_error(error)}", err=True)
    return typer.Exit(code)


def _decode(data: bytes, source: str) -> str:
    if b"\x00" in data:
        raise ValueError(f"{source} looks like a binary file, not SQL text")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{source} is not valid UTF-8 text") from error


def _read_stdin() -> str:
    data = sys.stdin.buffer.read(MAX_QUERY_BYTES + 1) if hasattr(sys.stdin, "buffer") else b""
    if not data and not hasattr(sys.stdin, "buffer"):
        data = sys.stdin.read(MAX_QUERY_BYTES + 1).encode()
    if len(data) > MAX_QUERY_BYTES:
        raise ValueError(f"the query on stdin is larger than {MAX_QUERY_BYTES:,} bytes")
    return _decode(data, "stdin")


def _read_file(path: Path) -> str:
    if str(path) == "-":
        return _read_stdin()
    if path.stat().st_size > MAX_QUERY_BYTES:
        raise ValueError(f"{path} is larger than {MAX_QUERY_BYTES:,} bytes")
    return _decode(path.read_bytes(), str(path))


def read_query(sql: str | None = None, file: Path | None = None) -> str:
    if sql is not None and file is not None:
        raise ValueError("use only one of --sql and --file")
    if sql is not None:
        text = sql
    elif file is not None:
        text = _read_file(file)
    elif sys.stdin.isatty():
        raise ValueError("give the query with --sql or --file, or pipe it in on stdin")
    else:
        text = _read_stdin()
    return _clean_query(text)


def _clean_query(text: str) -> str:
    cleaned = text.lstrip(chr(0xFEFF)).strip()
    if not cleaned:
        raise ValueError("the query is empty")
    return cleaned


def _target(dsn: str | None, connection: str | None, writes: bool = False) -> Target:
    target = resolve_target(dsn, connection, writes)
    if target.name:
        typer.echo(f"using connection {target.name} ({target.label})", err=True)
    return target


def _split(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _prove(run: Callable[[], VerifyResult]) -> None:
    try:
        result = run()
    except (ValueError, OSError, psycopg.Error) as error:
        raise _fail(error) from error
    _print_json(result.as_dict())
    if not result.improved:
        raise typer.Exit(1)


@app.command(
    name="inspect",
    help="The plan digest and table facts for a SELECT. Runs EXPLAIN ANALYZE.",
)
def inspect_command(
    connection: ConnectionOption = None,
    dsn: DsnOption = None,
    sql: SqlOption = None,
    file: FileOption = None,
    raw_plan: Annotated[bool, typer.Option(help="Also include the raw EXPLAIN plan")] = False,
    analyze: Annotated[
        bool,
        typer.Option(
            "--analyze/--no-analyze",
            help="--no-analyze runs plain EXPLAIN: estimates only, the query is not executed "
            "(for queries slower than 60 s)",
        ),
    ] = True,
) -> None:
    try:
        target = _target(dsn, connection)
        _print_json(inspect_query(target.conninfo, read_query(sql, file), raw_plan, analyze))
    except (ValueError, psycopg.Error, OSError) as error:
        raise _fail(error) from error


@app.command(
    name="summarize",
    help="The plan digest for EXPLAIN (ANALYZE, FORMAT JSON) output you already have. "
    "No database needed.",
)
def summarize_command(
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="Plan file; stdin if omitted")
    ] = None,
) -> None:
    try:
        raw = _read_file(file) if file is not None else _read_stdin()
        _print_json(plan_digest(parse_explain(raw)))
    except (ValueError, OSError) as error:
        raise _fail(error) from error


@verify_app.command(name="index", help="Prove an index. Exit 1 if it is not proven.")
def verify_index_command(
    table: Annotated[str, typer.Option(help="Bare table name, as in tables[].name")],
    columns: Annotated[str, typer.Option(help="Comma-separated key columns")],
    connection: ConnectionOption = None,
    dsn: DsnOption = None,
    schema: Annotated[str | None, typer.Option(help="Schema, from tables[].schema")] = None,
    method: Annotated[IndexMethod, typer.Option(help="btree, hash, gin or brin")] = "btree",
    include: Annotated[str, typer.Option(help="Comma-separated covering columns")] = "",
    where: Annotated[str | None, typer.Option(help="Partial index predicate")] = None,
    sql: SqlOption = None,
    file: FileOption = None,
) -> None:
    try:
        suggestion = IndexSuggestion(
            table=table,
            columns=_split(columns),
            schema_name=schema,
            method=method,
            include=_split(include),
            where=where,
        )
        target = _target(dsn, connection, writes=True)
        query = read_query(sql, file)
    except (ValueError, OSError) as error:
        raise _fail(error) from error
    _prove(lambda: verify_index(target.conninfo, query, suggestion))


@verify_app.command(name="statistics", help="Prove extended statistics. Exit 1 if not proven.")
def verify_statistics_command(
    table: Annotated[str, typer.Option(help="Bare table name, as in tables[].name")],
    columns: Annotated[str, typer.Option(help="Comma-separated columns (at least two)")],
    connection: ConnectionOption = None,
    dsn: DsnOption = None,
    schema: Annotated[str | None, typer.Option(help="Schema, from tables[].schema")] = None,
    kinds: Annotated[
        str,
        typer.Option(help="dependencies, ndistinct, mcv, comma-separated (default: first two)"),
    ] = "",
    sql: SqlOption = None,
    file: FileOption = None,
) -> None:
    try:
        wanted: dict[str, Any] = {"table": table, "columns": _split(columns), "schema_name": schema}
        if kinds:
            wanted["kinds"] = _split(kinds)
        suggestion = StatsSuggestion.model_validate(wanted)
        target = _target(dsn, connection, writes=True)
        query = read_query(sql, file)
    except (ValueError, OSError) as error:
        raise _fail(error) from error
    _prove(lambda: verify_statistics(target.conninfo, query, suggestion))


@verify_app.command(
    name="setting",
    help="Prove a settings change (work_mem, jit, random_page_cost, ...) for this session "
    "only. Exit 1 if not proven.",
)
def verify_setting_command(
    set_: Annotated[
        list[str], typer.Option("--set", help="name=value, e.g. work_mem=64MB (repeat up to 5)")
    ],
    connection: ConnectionOption = None,
    dsn: DsnOption = None,
    sql: SqlOption = None,
    file: FileOption = None,
) -> None:
    try:
        settings: dict[str, str] = {}
        for item in set_:
            name, separator, value = item.partition("=")
            if not separator:
                raise ValueError(f"--set expects name=value, got {item!r}")
            settings[name.strip()] = value.strip()
        suggestion = SettingSuggestion(settings=settings)
        target = _target(dsn, connection, writes=True)
        query = read_query(sql, file)
    except (ValueError, OSError) as error:
        raise _fail(error) from error
    _prove(lambda: verify_setting(target.conninfo, query, suggestion))


@verify_app.command(
    name="rewrite", help="Prove a rewrite returns the same rows, faster. Exit 1 if not proven."
)
def verify_rewrite_command(
    rewrite: Annotated[str | None, typer.Option(help="The rewritten SELECT")] = None,
    rewrite_file: Annotated[
        Path | None, typer.Option(help="Read the rewritten SELECT from a file")
    ] = None,
    connection: ConnectionOption = None,
    dsn: DsnOption = None,
    sql: SqlOption = None,
    file: FileOption = None,
) -> None:
    try:
        if (rewrite is None) == (rewrite_file is None):
            raise ValueError("give the new query with exactly one of --rewrite and --rewrite-file")
        rewritten = _clean_query(
            rewrite if rewrite is not None else _read_file(rewrite_file or Path("-"))
        )
        target = _target(dsn, connection, writes=True)
        query = read_query(sql, file)
    except (ValueError, OSError) as error:
        raise _fail(error) from error
    _prove(lambda: verify_rewrite(target.conninfo, query, rewritten))


@app.command(name="top", help="The costliest SELECTs from pg_stat_statements ($n placeholders).")
def top_command(
    connection: ConnectionOption = None,
    dsn: DsnOption = None,
    limit: Annotated[int, typer.Option(help="How many queries to list (at most 100)")] = 10,
    min_calls: Annotated[int, typer.Option(help="Ignore queries called fewer times")] = 1,
) -> None:
    try:
        rows = top_queries(_target(dsn, connection).conninfo, limit, min_calls)
    except (ValueError, psycopg.Error, OSError) as error:
        raise _fail(error) from error
    _print_json(rows)


@connections_app.callback(invoke_without_command=True)
def connections_list(context: typer.Context) -> None:
    if context.invoked_subcommand is not None:
        return
    try:
        config = load_config()
    except ConfigError as error:
        raise _fail(error) from error
    _print_json(
        [
            {
                "name": name,
                "target": found.describe(),
                "default": name == config.default_connection,
                "protected": found.protected,
                "source": str(found.source),
            }
            for name, found in sorted(config.connections.items())
        ]
    )


@connections_app.command(name="test", help="Connect and report who and what you reached.")
def connections_test(
    name: Annotated[str | None, typer.Argument(help="Connection name; default if omitted")] = None,
    connection: ConnectionOption = None,
    dsn: DsnOption = None,
) -> None:
    try:
        target = _target(dsn, name or connection)
        with connect(target.conninfo) as conn:
            conn.autocommit = True
            conn.execute("SET statement_timeout = '10s'")
            conn.execute("SET lock_timeout = '5s'")
            version_text, user, read_only = conn.execute(
                "SELECT version(), current_user, current_setting('transaction_read_only')"
            ).fetchone() or ("?", "?", "?")
            statements = conn.execute(
                "SELECT count(*) FROM pg_extension WHERE extname = 'pg_stat_statements'"
            ).fetchone()
    except (ValueError, psycopg.Error, OSError) as error:
        raise _fail(error) from error
    _print_json(
        {
            "target": target.label,
            "user": user,
            "version": version_text,
            "read_only_session": read_only == "on",
            "pg_stat_statements": bool(statements and statements[0]),
        }
    )


def _write_new(path: Path, text: str, mode: int, force: bool = False, hint: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_TRUNC if force else os.O_EXCL
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError as error:
        raise ValueError(f"{path} already exists; {hint}") from error
    except OSError as error:
        if path.is_symlink():
            raise ValueError(f"{path} is a symbolic link; refusing to write through it") from error
        raise
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(text)


@app.command(name="init", help=f"Write a starter {PROJECT_FILE} (never overwrites).")
def init_command(
    user: Annotated[bool, typer.Option("--user", help="Write the user config instead")] = False,
) -> None:
    path = user_config_path() if user else Path.cwd() / PROJECT_FILE
    try:
        _write_new(path, TEMPLATE, PRIVATE_FILE, hint="edit it, or delete it to start over")
    except (ValueError, OSError) as error:
        raise _fail(error) from error
    typer.echo(f"wrote {path}")
    typer.echo("Edit it, then run: pgsage connections test")
    try:
        load_config()
    except ConfigError as error:
        typer.echo(f"warning: {error}", err=True)


@app.command(
    name="install-skill",
    help="Install the pgsage skill so your coding agent knows how to tune a pasted query.",
)
def install_skill_command(
    user: Annotated[
        bool, typer.Option("--user", help="Install for every project (~/.claude/skills)")
    ] = False,
    directory: Annotated[
        Path | None, typer.Option("--dir", help="Skills directory for another agent")
    ] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing skill")] = False,
) -> None:
    root = directory or (Path.home() if user else Path.cwd()) / ".claude" / "skills"
    path = root / SKILL_NAME / "SKILL.md"
    try:
        if directory is not None and user:
            raise ValueError("use either --dir or --user, not both")
        text = (files("pgsage") / "skill" / "SKILL.md").read_text(encoding="utf-8")
        _write_new(path, text, PUBLIC_FILE, force, hint="pass --force to replace it")
    except (ValueError, OSError) as error:
        raise _fail(error) from error
    typer.echo(f"wrote {path}")
