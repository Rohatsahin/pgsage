from __future__ import annotations

import os
import sys
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated

import click
import psycopg
import typer

from pgsage.ai import (
    DEFAULT_MODEL,
    SYSTEM_PROMPT,
    AnthropicClient,
    Explanation,
    ExplanationError,
    LLMClient,
    OpenAICompatibleClient,
    explain_plan,
    prompt_stats,
)
from pgsage.context import top_queries
from pgsage.evals import CASES, OracleClient, run_evals
from pgsage.plan import parse_explain
from pgsage.report import write_report
from pgsage.shape import diff_shapes
from pgsage.tune import DEFAULT_ROUNDS, TuneReport, prepare_prompt, tune
from pgsage.verify import IndexSuggestion, VerifyResult, verify_index

app = typer.Typer(help="AI-native PostgreSQL query plan tuning", no_args_is_help=True)

Dsn = Annotated[str, typer.Option(help="Connection string of a dev or scratch database")]


class ProviderChoice(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class EffortChoice(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


Provider = Annotated[
    ProviderChoice, typer.Option(help="anthropic, or openai for any OpenAI-compatible API")
]
Model = Annotated[str | None, typer.Option(help="Model name; defaults per provider")]
Effort = Annotated[EffortChoice, typer.Option(help="Thinking effort for Claude models")]
BaseUrl = Annotated[str | None, typer.Option(help="Base URL for the openai provider")]


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


MAX_QUERY_BYTES = 1_000_000
EDITOR_HEADER = "-- Write or paste one read-only SELECT below, then save and close the editor.\n"

SqlOption = Annotated[
    str | None, typer.Option("--sql", help="The SELECT, given inline (see also --file, --edit)")
]
FileOption = Annotated[
    Path | None,
    typer.Option("--file", "-f", help="Read the SELECT from a file (use - for stdin)"),
]
EditOption = Annotated[bool, typer.Option("--edit", "-e", help="Write the SELECT in your $EDITOR")]


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


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


def _edit_query() -> str:
    edited = click.edit(EDITOR_HEADER + "\n", extension=".sql")
    if edited is None:
        raise ValueError("the editor closed without saving a query")
    return edited.replace(EDITOR_HEADER, "", 1)


def read_query(sql: str | None = None, file: Path | None = None, edit: bool = False) -> str:
    if sum(source is not None and source is not False for source in (sql, file, edit)) > 1:
        raise ValueError("use only one of --sql, --file and --edit")
    if sql is not None:
        text = sql
    elif file is not None:
        text = _read_file(file)
    elif edit or _stdin_is_tty():
        typer.echo(
            "Opening your editor for the query (or use --sql, --file, or pipe it in)...", err=True
        )
        text = _edit_query()
    else:
        text = _read_stdin()
    text = text.lstrip("\ufeff").strip()
    if not text:
        raise ValueError("the query is empty")
    return text


def make_client(
    model: str | None,
    provider: str = "anthropic",
    base_url: str | None = None,
    effort: str | None = "high",
) -> LLMClient:
    if provider == "openai":
        if not model:
            raise ValueError("--model is required with --provider openai")
        return OpenAICompatibleClient(
            model=model,
            base_url=base_url or "http://localhost:11434/v1",
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
    if provider != "anthropic":
        raise ValueError(f"unknown provider {provider!r}")
    return AnthropicClient(model=model or DEFAULT_MODEL, effort=effort)


def _fail(error: Exception, code: int = 2) -> typer.Exit:
    typer.echo(f"error: {error}", err=True)
    return typer.Exit(code)


def _echo_usage(client: object) -> None:
    log: list[dict[str, int]] = getattr(client, "usage_log", [])
    if not log:
        return
    total = {key: sum(call.get(key, 0) for call in log) for key in log[0]}
    typer.echo(
        f"\n(tokens: {total['input_tokens']} input, {total['cache_read_input_tokens']} read from "
        f"cache, {total['cache_creation_input_tokens']} written to cache, "
        f"{total['output_tokens']} output)"
    )


def _echo_explanation(explanation: Explanation) -> None:
    typer.echo(explanation.summary)
    typer.echo(f"\nCause: {explanation.root_cause}")
    for line in explanation.evidence:
        typer.echo(f"  evidence: {line}")
    for problem in explanation.rejected:
        typer.echo(f"  ignored: {problem}")
    for action in explanation.actions:
        typer.echo(f"\n- {action.description} [{action.confidence} confidence]")
        if action.expected_effect:
            typer.echo(f"    expect: {action.expected_effect}")
        if action.sql:
            typer.echo(
                f"    (advice from the model, not verified; review before running) {action.sql}"
            )
        if action.index:
            typer.echo(f"    {action.index.ddl}")
        if action.statistics:
            typer.echo(f"    {action.statistics.ddl}")
        if action.rewrite:
            typer.echo(f"    {action.rewrite}")


def _echo_result(result: VerifyResult) -> None:
    verdict = "PROVEN" if result.improved else "NOT PROVEN"
    speedup = f", {result.speedup:.1f}x faster" if result.improved and result.speedup else ""
    equivalence = " (results differ!)" if result.equivalent is False else ""
    if result.error:
        typer.echo(f"\n[NOT PROVEN] {result.kind}: {result.statement}\n    {result.error}")
        return
    if result.note:
        equivalence += f" ({result.note})"
    typer.echo(
        f"\n[{verdict}] {result.kind}: {result.statement}{equivalence}\n"
        f"    cost {result.before.total_cost:.1f} -> {result.after.total_cost:.1f}, "
        f"{result.before.execution_ms} ms -> {result.after.execution_ms} ms{speedup}"
    )


def _echo_report(report: TuneReport) -> None:
    _echo_explanation(report.explanation)
    for change in report.verified:
        _echo_result(change.result)
    if report.rounds > 1:
        typer.echo(f"\n({report.rounds} rounds)")


@app.command(name="explain", help="Explain an EXPLAIN (ANALYZE, FORMAT JSON) plan with an LLM.")
def explain_command(
    file: Annotated[Path | None, typer.Argument(help="EXPLAIN JSON file; stdin if omitted")] = None,
    model: Model = None,
    provider: Provider = ProviderChoice.ANTHROPIC,
    base_url: BaseUrl = None,
    effort: Effort = EffortChoice.HIGH,
) -> None:
    try:
        raw = file.read_text() if file else sys.stdin.read()
        client = make_client(model, provider.value, base_url, effort.value)
        explanation = explain_plan(client, parse_explain(raw))
    except (ValueError, ExplanationError, OSError) as error:
        raise _fail(error) from error
    _echo_explanation(explanation)
    _echo_usage(client)


def _dry_run(dsn: str, query: str, show_prompt: bool) -> None:
    prepared = prepare_prompt(dsn, query)
    stats = prompt_stats(SYSTEM_PROMPT, prepared.prompt)
    typer.echo("Prompt built; nothing was sent to a model.")
    typer.echo(f"  system prompt: {stats['system_chars']:,} characters (static, cached)")
    typer.echo(
        f"  query, plan and tables: {stats['stable_chars']:,} characters (cached across rounds)"
    )
    typer.echo(
        f"  about {stats['approx_tokens']:,} tokens in total (estimate: 1 token per 4 characters)"
    )
    plan = (
        f"{prepared.plan_nodes} nodes, raw plan omitted (digest and hotspots sent)"
        if prepared.raw_plan_omitted
        else f"{prepared.plan_nodes} nodes, sent in full"
    )
    typer.echo(f"  plan: {plan}; tables: {prepared.tables}")
    if show_prompt:
        typer.echo("\n--- prompt (after the system prompt) ---")
        typer.echo(str(prepared.prompt))


@app.command(
    name="tune",
    help="Fetch the plan, ask the LLM for fixes, and prove each proposal on the database.",
)
def tune_command(
    dsn: Dsn,
    sql: SqlOption = None,
    file: FileOption = None,
    edit: EditOption = False,
    model: Model = None,
    provider: Provider = ProviderChoice.ANTHROPIC,
    base_url: BaseUrl = None,
    effort: Effort = EffortChoice.HIGH,
    rounds: Annotated[
        int, typer.Option(help="Max AI attempts if nothing is proven")
    ] = DEFAULT_ROUNDS,
    report_dir: Annotated[
        Path | None,
        typer.Option(
            help="Write a Markdown report, a JSON report and a SQL script of proven changes"
        ),
    ] = None,
    concurrently: Annotated[
        bool, typer.Option(help="Use CREATE INDEX CONCURRENTLY in the SQL script")
    ] = True,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Build the prompt and print its size; call no model"),
    ] = False,
    show_prompt: Annotated[
        bool,
        typer.Option("--show-prompt", help="Print the exact prompt that would be sent (dry run)"),
    ] = False,
) -> None:
    try:
        query = read_query(sql, file, edit)
        if dry_run or show_prompt:
            _dry_run(dsn, query, show_prompt)
            return
        client = make_client(model, provider.value, base_url, effort.value)
        report = tune(client, dsn, query, max_rounds=rounds)
    except (ValueError, ExplanationError, psycopg.Error, OSError) as error:
        raise _fail(error) from error
    _echo_report(report)
    _echo_usage(client)
    if report_dir is not None:
        for path in write_report(report_dir, report, concurrently=concurrently):
            typer.echo(f"wrote {path}")


@app.command(name="verify", help="Prove one index you chose: apply, measure, roll back.")
def verify_command(
    dsn: Dsn,
    table: Annotated[str, typer.Option()],
    columns: Annotated[str, typer.Option(help="Comma-separated index columns")],
    schema: Annotated[str | None, typer.Option()] = None,
    sql: SqlOption = None,
    file: FileOption = None,
    edit: EditOption = False,
) -> None:
    suggestion = IndexSuggestion(
        table=table, columns=[c.strip() for c in columns.split(",")], schema_name=schema
    )
    try:
        result = verify_index(dsn, read_query(sql, file, edit), suggestion)
    except (ValueError, psycopg.Error, OSError) as error:
        raise _fail(error) from error
    _echo_result(result)
    if not result.improved:
        raise typer.Exit(1)


@app.command(name="top", help="List the costliest SELECTs from pg_stat_statements.")
def top_command(
    dsn: Dsn,
    limit: Annotated[int, typer.Option()] = 10,
    min_calls: Annotated[int, typer.Option()] = 1,
) -> None:
    try:
        rows = top_queries(dsn, limit, min_calls)
    except (ValueError, psycopg.Error) as error:
        raise _fail(error) from error
    for row in rows:
        query = " ".join(str(row["query"]).split())
        typer.echo(f"{row['total_ms']} ms total, {row['calls']} calls, {row['mean_ms']} ms avg")
        typer.echo(f"    {query}")
    typer.echo("\nQueries show $n placeholders: substitute real values, then run `pgsage tune`.")


@app.command(name="diff", help="Exit 1 if the plan shape differs between two EXPLAIN JSON files.")
def diff_command(
    before: Annotated[Path, typer.Argument(help="Baseline EXPLAIN JSON")],
    after: Annotated[Path, typer.Argument(help="New EXPLAIN JSON")],
) -> None:
    try:
        result = diff_shapes(parse_explain(before.read_text()), parse_explain(after.read_text()))
    except (ValueError, OSError) as error:
        raise _fail(error) from error
    if not result.changed:
        typer.echo("Plan shape unchanged")
        return
    typer.echo("Plan shape changed")
    typer.echo("before: " + " > ".join(n.node for n in result.before))
    typer.echo("after:  " + " > ".join(n.node for n in result.after))
    if result.slowdown is not None:
        typer.echo(f"execution time x{result.slowdown:.2f}")
    raise typer.Exit(1)


@app.command(name="eval", help="Score a model on cases with known fixes.")
def eval_command(
    dsn: Annotated[
        str,
        typer.Option(help="A scratch database: each case runs in a throwaway pgsage_eval_* schema"),
    ],
    oracle: Annotated[
        bool, typer.Option(help="Use known-good answers to validate the harness")
    ] = False,
    model: Model = None,
    provider: Provider = ProviderChoice.ANTHROPIC,
    base_url: BaseUrl = None,
    effort: Effort = EffortChoice.HIGH,
) -> None:
    try:
        shared = None if oracle else make_client(model, provider.value, base_url, effort.value)
    except (ValueError, ExplanationError) as error:
        raise _fail(error) from error
    outcomes = run_evals(lambda case: OracleClient(case) if shared is None else shared, dsn, CASES)
    for outcome in outcomes:
        typer.echo(f"{'PASS' if outcome.passed else 'FAIL'}  {outcome.name}: {outcome.detail}")
    passed = sum(o.passed for o in outcomes)
    typer.echo(f"\n{passed}/{len(outcomes)} passed")
    if passed != len(outcomes):
        raise typer.Exit(1)
