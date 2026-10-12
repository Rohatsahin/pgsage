from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from pydantic import BaseModel

from pgsage.ai import (
    MAX_PLAN_NODES,
    Action,
    Explanation,
    LLMClient,
    Prompt,
    Redaction,
    build_prompt,
    explain_plan,
)
from pgsage.context import get_plan, table_ancestors, table_info, tables_in_plan
from pgsage.plan import plan_digest
from pgsage.verify import (
    ChangeKind,
    IndexSuggestion,
    StatsSuggestion,
    VerifyResult,
    verify_index,
    verify_rewrite,
    verify_statistics,
)

DEFAULT_ROUNDS = 2
MAX_ERROR_LENGTH = 300
SCHEMA_ERROR_CLASS = "42"


class VerifiedChange(BaseModel):
    description: str
    confidence: str = "medium"
    expected_effect: str | None = None
    index: IndexSuggestion | None = None
    statistics: StatsSuggestion | None = None
    result: VerifyResult


class TuneReport(BaseModel):
    explanation: Explanation
    verified: list[VerifiedChange]
    rounds: int
    query: str = ""
    baseline_ms: float | None = None
    hotspots: list[dict[str, Any]] = []

    @property
    def proven(self) -> list[VerifiedChange]:
        return [v for v in self.verified if v.result.improved]


def _error_text(error: Exception, redaction: Redaction) -> str:
    if isinstance(error, psycopg.Error):
        message = error.diag.message_primary or str(error).splitlines()[0]
        if (error.sqlstate or "").startswith(SCHEMA_ERROR_CLASS):
            text = redaction.apply(message)
        else:
            text = redaction.message(message)
        return f"{type(error).__name__}: {text}"[:MAX_ERROR_LENGTH]
    return redaction.message(str(error).splitlines()[0] if str(error) else type(error).__name__)[
        :MAX_ERROR_LENGTH
    ]


def _matching_tables(
    target: IndexSuggestion | StatsSuggestion, tables: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        t
        for t in tables
        if t["name"] == target.table
        and (target.schema_name is None or t["schema"] == target.schema_name)
    ]


def _allowlist_error(
    action: Action, tables: list[dict[str, Any]], redaction: Redaction
) -> str | None:
    target = action.index or action.statistics
    if target is None:
        return None
    alias = redaction.alias_name
    matches = _matching_tables(target, tables)
    if not matches:
        known = ", ".join(alias(t["name"]) for t in tables)
        return f"table {alias(target.table)} is not part of this query; tables in the plan: {known}"
    if target.schema_name is None and len({t["schema"] for t in matches}) > 1:
        schemas = ", ".join(sorted(alias(t["schema"]) for t in matches))
        return f"table {alias(target.table)} exists in several schemas ({schemas}); set schema_name"
    table = matches[0]
    columns = {c["column_name"] for c in table["columns"]}
    wanted = action.index.all_columns if action.index else target.columns
    missing = [c for c in wanted if c not in columns]
    if missing:
        have = ", ".join(alias(c) for c in sorted(columns))
        return (
            f"column(s) {', '.join(alias(c) for c in missing)} do not exist on "
            f"{alias(target.table)}; columns: {have}"
        )
    return None


def _with_resolved_schema(action: Action, tables: list[dict[str, Any]]) -> Action:
    target = action.index or action.statistics
    if target is None or target.schema_name is not None:
        return action
    schemas = {t["schema"] for t in _matching_tables(target, tables)}
    if len(schemas) != 1:
        return action
    resolved = target.model_copy(update={"schema_name": next(iter(schemas))})
    if action.index is not None:
        return action.model_copy(update={"index": resolved})
    return action.model_copy(update={"statistics": resolved})


def _change(action: Action, result: VerifyResult) -> VerifiedChange:
    return VerifiedChange(
        description=action.description,
        confidence=action.confidence,
        expected_effect=action.expected_effect,
        index=action.index,
        statistics=action.statistics,
        result=result,
    )


def _hotspots(plan: Any, limit: int = 3) -> list[dict[str, Any]]:
    rows = [row for row in plan_digest(plan) if row.get("self_ms")]
    rows.sort(key=lambda row: row["self_ms"], reverse=True)
    keys = ("node", "relation", "index", "self_ms", "estimate_off_by", "rows_removed_by_filter")
    return [{key: row.get(key) for key in keys} for row in rows[:limit]]


def _proposal(action: Action) -> tuple[ChangeKind, str] | None:
    if action.index is not None:
        return "index", action.index.ddl
    if action.statistics is not None:
        return "statistics", action.statistics.ddl
    if action.rewrite is not None:
        return "rewrite", action.rewrite
    return None


def _verify_action(
    dsn: str,
    sql: str,
    action: Action,
    tables: list[dict[str, Any]],
    redaction: Redaction,
    seen: set[tuple[str, str]],
) -> VerifiedChange | None:
    action = _with_resolved_schema(action, tables)
    proposal = _proposal(action)
    if proposal is None:
        return None
    kind, statement = proposal

    def failed(reason: str) -> VerifiedChange:
        return _change(action, VerifyResult.failed(kind, statement, reason))

    key = (kind, " ".join(statement.split()))
    if key in seen:
        return failed(
            "duplicate of an earlier proposal in this session; propose something different"
        )
    seen.add(key)
    if (problem := _allowlist_error(action, tables, redaction)) is not None:
        return failed(problem)
    try:
        if action.index is not None:
            result = verify_index(dsn, sql, action.index)
        elif action.statistics is not None:
            result = verify_statistics(dsn, sql, action.statistics)
        else:
            result = verify_rewrite(dsn, sql, action.rewrite or "")
    except (psycopg.Error, ValueError) as error:
        return failed(_error_text(error, redaction))
    return _change(action, result)


def _attempt_summary(change: VerifiedChange, redaction: Redaction) -> dict[str, Any]:
    result = change.result
    return {
        "kind": result.kind,
        "statement": redaction.apply(result.statement),
        "proven": result.improved,
        "equivalent": result.equivalent,
        "note": result.note,
        "error": result.error,
        "before_ms": result.before.execution_ms,
        "after_ms": result.after.execution_ms,
        "used_new_index": result.after.uses_new_index,
    }


def _collect_tables(dsn: str, plan_tables: list[tuple[str, str]]) -> list[dict[str, Any]]:
    collected: dict[tuple[str, str], dict[str, Any]] = {}
    for schema, name in plan_tables:
        candidates = [(schema, name), *table_ancestors(dsn, name, schema)]
        for candidate_schema, candidate_name in candidates:
            if (candidate_schema, candidate_name) in collected:
                continue
            try:
                collected[(candidate_schema, candidate_name)] = table_info(
                    dsn, candidate_name, candidate_schema
                )
            except ValueError:
                continue
    return list(collected.values())


@dataclass
class PreparedPrompt:
    prompt: Prompt
    plan_nodes: int
    tables: int

    @property
    def raw_plan_omitted(self) -> bool:
        return self.plan_nodes > MAX_PLAN_NODES


def prepare_prompt(dsn: str, sql: str) -> PreparedPrompt:
    plan = get_plan(dsn, sql)
    tables = _collect_tables(dsn, tables_in_plan(plan))
    prompt = build_prompt(plan, sql, tables, redaction=Redaction())
    return PreparedPrompt(prompt=prompt, plan_nodes=len(plan.nodes()), tables=len(tables))


def tune(client: LLMClient, dsn: str, sql: str, max_rounds: int = DEFAULT_ROUNDS) -> TuneReport:
    plan = get_plan(dsn, sql)
    tables = _collect_tables(dsn, tables_in_plan(plan))
    redaction = Redaction()
    verified: list[VerifiedChange] = []
    attempts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    explanation = explain_plan(client, plan, sql, tables, redaction=redaction)
    rounds = 1
    while True:
        round_changes = [
            c
            for a in explanation.actions
            if (c := _verify_action(dsn, sql, a, tables, redaction, seen))
        ]
        verified.extend(round_changes)
        if any(c.result.improved for c in round_changes) or not round_changes:
            break
        if rounds >= max_rounds:
            break
        attempts.extend(_attempt_summary(c, redaction) for c in round_changes)
        explanation = explain_plan(client, plan, sql, tables, attempts, redaction)
        rounds += 1
    return TuneReport(
        explanation=explanation,
        verified=verified,
        rounds=rounds,
        query=sql,
        baseline_ms=plan.execution_time,
        hotspots=_hotspots(plan),
    )
