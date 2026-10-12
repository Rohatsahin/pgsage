from __future__ import annotations

import re
from typing import Any

import psycopg
from pydantic import BaseModel

from pgsage.ai import Action, Explanation, LLMClient, Redaction, explain_plan
from pgsage.context import get_plan, table_info, tables_in_plan
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
QUOTED_VALUE = re.compile(r"'[^']*'|\"[^\"]*\"")
MAX_ERROR_LENGTH = 300


class VerifiedChange(BaseModel):
    description: str
    index: IndexSuggestion | None = None
    statistics: StatsSuggestion | None = None
    result: VerifyResult


class TuneReport(BaseModel):
    explanation: Explanation
    verified: list[VerifiedChange]
    rounds: int

    @property
    def proven(self) -> list[VerifiedChange]:
        return [v for v in self.verified if v.result.improved]


def _error_text(error: psycopg.Error) -> str:
    message = error.diag.message_primary or str(error).splitlines()[0]
    return f"{type(error).__name__}: {QUOTED_VALUE.sub('<value>', message)}"[:MAX_ERROR_LENGTH]


def _allowlist_error(
    action: Action, tables: list[dict[str, Any]], redaction: Redaction
) -> str | None:
    target = action.index or action.statistics
    if target is None:
        return None
    alias = redaction.alias_name
    matches = [
        t
        for t in tables
        if t["name"] == target.table
        and (target.schema_name is None or t["schema"] == target.schema_name)
    ]
    if not matches:
        known = ", ".join(alias(t["name"]) for t in tables)
        return f"table {alias(target.table)} is not part of this query; tables in the plan: {known}"
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


def _proposal(action: Action) -> tuple[ChangeKind, str] | None:
    if action.index is not None:
        return "index", action.index.ddl
    if action.statistics is not None:
        return "statistics", action.statistics.ddl
    if action.rewrite is not None:
        return "rewrite", action.rewrite
    return None


def _verify_action(
    dsn: str, sql: str, action: Action, tables: list[dict[str, Any]], redaction: Redaction
) -> VerifiedChange | None:
    proposal = _proposal(action)
    if proposal is None:
        return None
    kind, statement = proposal
    if (problem := _allowlist_error(action, tables, redaction)) is not None:
        return VerifiedChange(
            description=action.description,
            index=action.index,
            statistics=action.statistics,
            result=VerifyResult.failed(kind, statement, problem),
        )
    try:
        if action.index is not None:
            result = verify_index(dsn, sql, action.index)
        elif action.statistics is not None:
            result = verify_statistics(dsn, sql, action.statistics)
        else:
            result = verify_rewrite(dsn, sql, action.rewrite or "")
    except psycopg.Error as error:
        result = VerifyResult.failed(kind, statement, _error_text(error))
    return VerifiedChange(
        description=action.description,
        index=action.index,
        statistics=action.statistics,
        result=result,
    )


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


def tune(client: LLMClient, dsn: str, sql: str, max_rounds: int = DEFAULT_ROUNDS) -> TuneReport:
    plan = get_plan(dsn, sql)
    tables = [table_info(dsn, name, schema) for schema, name in tables_in_plan(plan)]
    redaction = Redaction()
    verified: list[VerifiedChange] = []
    attempts: list[dict[str, Any]] = []
    explanation = explain_plan(client, plan, sql, tables, redaction=redaction)
    rounds = 1
    while True:
        round_changes = [
            c for a in explanation.actions if (c := _verify_action(dsn, sql, a, tables, redaction))
        ]
        verified.extend(round_changes)
        if any(c.result.improved for c in round_changes) or not round_changes:
            break
        if rounds >= max_rounds:
            break
        attempts.extend(_attempt_summary(c, redaction) for c in round_changes)
        explanation = explain_plan(client, plan, sql, tables, attempts, redaction)
        rounds += 1
    return TuneReport(explanation=explanation, verified=verified, rounds=rounds)
