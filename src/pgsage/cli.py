from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import psycopg
import typer

from pgsage.ai import (
    DEFAULT_MODEL,
    AnthropicClient,
    Explanation,
    ExplanationError,
    LLMClient,
    OpenAICompatibleClient,
    explain_plan,
)
from pgsage.context import top_queries
from pgsage.evals import CASES, OracleClient, run_evals
from pgsage.migrations import write_migrations
from pgsage.plan import parse_explain
from pgsage.shape import diff_shapes
from pgsage.tune import DEFAULT_ROUNDS, TuneReport, tune
from pgsage.verify import IndexSuggestion, VerifyResult, verify_index

app = typer.Typer(help="AI-native PostgreSQL query plan tuning", no_args_is_help=True)

Dsn = Annotated[str, typer.Option(help="Connection string of a dev or scratch database")]
Provider = Annotated[str, typer.Option(help="anthropic or openai (any OpenAI-compatible API)")]
Model = Annotated[str | None, typer.Option(help="Model name; defaults per provider")]
Effort = Annotated[
    str, typer.Option(help="Thinking effort for Claude models: low, medium, high, xhigh, max")
]
BaseUrl = Annotated[str | None, typer.Option(help="Base URL for the openai provider")]


@app.callback()
def main() -> None:
    pass


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


@app.command(name="explain")
def explain_command(
    file: Annotated[Path | None, typer.Argument(help="EXPLAIN JSON file; stdin if omitted")] = None,
    model: Model = None,
    provider: Provider = "anthropic",
    base_url: BaseUrl = None,
    effort: Effort = "high",
) -> None:
    try:
        raw = file.read_text() if file else sys.stdin.read()
        client = make_client(model, provider, base_url, effort)
        explanation = explain_plan(client, parse_explain(raw))
    except (ValueError, ExplanationError) as error:
        raise _fail(error) from error
    _echo_explanation(explanation)
    _echo_usage(client)


@app.command(name="tune")
def tune_command(
    dsn: Dsn,
    sql: Annotated[str, typer.Option(help="Read-only SELECT statement to tune")],
    model: Model = None,
    provider: Provider = "anthropic",
    base_url: BaseUrl = None,
    effort: Effort = "high",
    rounds: Annotated[
        int, typer.Option(help="Max AI attempts if nothing is proven")
    ] = DEFAULT_ROUNDS,
    apply_dir: Annotated[
        Path | None, typer.Option(help="Write proven indexes/statistics as Flyway migrations")
    ] = None,
) -> None:
    try:
        client = make_client(model, provider, base_url, effort)
        report = tune(client, dsn, sql, max_rounds=rounds)
    except (ValueError, ExplanationError, psycopg.Error) as error:
        raise _fail(error) from error
    _echo_report(report)
    _echo_usage(client)
    if apply_dir is not None:
        proven = [
            (suggestion, change.result)
            for change in report.proven
            if (suggestion := change.index or change.statistics) is not None
        ]
        for path in write_migrations(apply_dir, proven):
            typer.echo(f"\nwrote {path}")


@app.command(name="verify")
def verify_command(
    dsn: Dsn,
    sql: Annotated[str, typer.Option(help="Read-only SELECT statement to test")],
    table: Annotated[str, typer.Option()],
    columns: Annotated[str, typer.Option(help="Comma-separated index columns")],
    schema: Annotated[str | None, typer.Option()] = None,
) -> None:
    suggestion = IndexSuggestion(
        table=table, columns=[c.strip() for c in columns.split(",")], schema_name=schema
    )
    try:
        result = verify_index(dsn, sql, suggestion)
    except (ValueError, psycopg.Error) as error:
        raise _fail(error) from error
    _echo_result(result)
    if not result.improved:
        raise typer.Exit(1)


@app.command(name="top")
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


@app.command(name="diff")
def diff_command(
    before: Annotated[Path, typer.Argument(help="Baseline EXPLAIN JSON")],
    after: Annotated[Path, typer.Argument(help="New EXPLAIN JSON")],
) -> None:
    try:
        result = diff_shapes(parse_explain(before.read_text()), parse_explain(after.read_text()))
    except ValueError as error:
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


@app.command(name="eval")
def eval_command(
    dsn: Annotated[str, typer.Option(help="An EMPTY scratch database: eval creates ev_* tables")],
    oracle: Annotated[
        bool, typer.Option(help="Use known-good answers to validate the harness")
    ] = False,
    model: Model = None,
    provider: Provider = "anthropic",
    base_url: BaseUrl = None,
    effort: Effort = "high",
) -> None:
    try:
        shared = None if oracle else make_client(model, provider, base_url, effort)
    except (ValueError, ExplanationError) as error:
        raise _fail(error) from error
    outcomes = run_evals(lambda case: OracleClient(case) if shared is None else shared, dsn, CASES)
    for outcome in outcomes:
        typer.echo(f"{'PASS' if outcome.passed else 'FAIL'}  {outcome.name}: {outcome.detail}")
    passed = sum(o.passed for o in outcomes)
    typer.echo(f"\n{passed}/{len(outcomes)} passed")
    if passed != len(outcomes):
        raise typer.Exit(1)
