from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Literal

import psycopg
from psycopg.conninfo import make_conninfo
from pydantic import BaseModel

from pgsage.ai import LLMClient
from pgsage.sqlscan import quote_ident
from pgsage.tune import TuneReport, tune
from pgsage.verify import literal

Expectation = Literal["fix", "no_change"]

ORDERS_SETUP = (
    "CREATE TABLE ev_orders AS SELECT g AS id, g % 1000 AS customer_id, "
    "'status' || (g % 5) AS status, timestamp '2024-01-01' + g * interval '1 minute' AS created_at "
    "FROM generate_series(1, 300000) g",
    "ANALYZE ev_orders",
)

EVENTS_SETUP = (
    "CREATE TABLE ev_events AS SELECT g AS id, "
    "timestamp '2024-01-01' + g * interval '1 minute' AS created_at, g % 50 AS kind "
    "FROM generate_series(1, 300000) g",
    "CREATE INDEX ON ev_events (created_at)",
    "ANALYZE ev_events",
)

USERS_SETUP = (
    "CREATE TABLE ev_users AS SELECT g AS id, 'user' || g AS name FROM generate_series(1, 100000) g",
    "ALTER TABLE ev_users ADD PRIMARY KEY (id)",
    "ANALYZE ev_users",
)


class EvalCase(BaseModel):
    name: str
    setup: tuple[str, ...]
    query: str
    expectation: Expectation
    oracle: dict[str, object]


class EvalOutcome(BaseModel):
    name: str
    passed: bool
    detail: str


def _index_reply(summary: str, table: str, columns: list[str]) -> dict[str, object]:
    return {
        "summary": summary,
        "root_cause": "The filtered columns have no index, so the table is scanned",
        "actions": [
            {
                "description": f"Index {table}({', '.join(columns)})",
                "index": {"table": table, "columns": columns},
            }
        ],
    }


CASES: list[EvalCase] = [
    EvalCase(
        name="missing-index",
        setup=ORDERS_SETUP,
        query="SELECT * FROM ev_orders WHERE customer_id = 42",
        expectation="fix",
        oracle=_index_reply("Seq scan discards 99.9% of rows", "ev_orders", ["customer_id"]),
    ),
    EvalCase(
        name="composite-index",
        setup=ORDERS_SETUP,
        query="SELECT * FROM ev_orders WHERE customer_id = 42 AND status = 'status2'",
        expectation="fix",
        oracle=_index_reply(
            "Two-column filter scans the table", "ev_orders", ["customer_id", "status"]
        ),
    ),
    EvalCase(
        name="date-cast-rewrite",
        setup=EVENTS_SETUP,
        query="SELECT * FROM ev_events WHERE created_at::date = date '2024-03-01'",
        expectation="fix",
        oracle={
            "summary": "The cast on created_at prevents the existing index from being used",
            "root_cause": "created_at::date is not indexable; a range on the bare column is",
            "actions": [
                {
                    "description": "Compare the bare column against a range",
                    "rewrite": (
                        "SELECT * FROM ev_events WHERE created_at >= date '2024-03-01' "
                        "AND created_at < date '2024-03-02'"
                    ),
                }
            ],
        },
    ),
    EvalCase(
        name="primary-key-lookup",
        setup=USERS_SETUP,
        query="SELECT * FROM ev_users WHERE id = 500",
        expectation="no_change",
        oracle={"summary": "Index scan on the primary key", "root_cause": "None", "actions": []},
    ),
    EvalCase(
        name="full-table-aggregate",
        setup=ORDERS_SETUP,
        query="SELECT count(*) FROM ev_orders",
        expectation="no_change",
        oracle={
            "summary": "Counting every row must read them",
            "root_cause": "None",
            "actions": [],
        },
    ),
]


def _eval_dsn(dsn: str, schema: str) -> str:
    return make_conninfo(dsn, options=f"-c search_path={schema}")


def _setup(dsn: str, schema: str, case: EvalCase) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(literal(f"CREATE SCHEMA {quote_ident(schema)}"))
    with psycopg.connect(_eval_dsn(dsn, schema), autocommit=True) as conn:
        for statement in case.setup:
            conn.execute(literal(statement))


def _teardown(dsn: str, schema: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(literal(f"DROP SCHEMA IF EXISTS {quote_ident(schema)} CASCADE"))


def _judge(case: EvalCase, report: TuneReport) -> EvalOutcome:
    if case.expectation == "no_change":
        actions = len(report.explanation.actions)
        return EvalOutcome(
            name=case.name,
            passed=actions == 0,
            detail="no changes proposed"
            if actions == 0
            else f"proposed {actions} needless change(s)",
        )
    proven = report.proven
    if proven:
        best = max(proven, key=lambda v: v.result.speedup or 0.0)
        return EvalOutcome(
            name=case.name,
            passed=True,
            detail=f"proven {best.result.kind}: {best.result.statement} "
            f"({best.result.speedup or 0:.1f}x)",
        )
    return EvalOutcome(name=case.name, passed=False, detail="no proven improvement")


def run_evals(
    client_for_case: Callable[[EvalCase], LLMClient],
    dsn: str,
    cases: list[EvalCase] | None = None,
) -> list[EvalOutcome]:
    outcomes: list[EvalOutcome] = []
    for case in cases if cases is not None else CASES:
        schema = f"pgsage_eval_{uuid.uuid4().hex[:8]}"
        try:
            _setup(dsn, schema, case)
            report = tune(client_for_case(case), _eval_dsn(dsn, schema), case.query)
            outcomes.append(_judge(case, report))
        except Exception as error:
            outcomes.append(EvalOutcome(name=case.name, passed=False, detail=f"error: {error}"))
        finally:
            _teardown(dsn, schema)
    return outcomes


class OracleClient:
    def __init__(self, case: EvalCase) -> None:
        self._reply = json.dumps(case.oracle)

    def complete(self, system: str, prompt: str) -> str:
        return self._reply
