from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import psycopg
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from pgsage.config import resolve_target
from pgsage.context import get_plan, table_info, top_queries
from pgsage.db import describe_error
from pgsage.plan import parse_explain, plan_digest
from pgsage.verify import (
    IndexMethod,
    IndexSuggestion,
    StatisticsKind,
    StatsSuggestion,
    verify_index,
    verify_rewrite,
    verify_statistics,
)

DSN_ENV = "PGSAGE_DSN"
MAX_MESSAGE_LENGTH = 500


def _dsn(writes: bool = False) -> str:
    return resolve_target(None, None, writes).conninfo


def _message(error: Exception) -> str:
    return describe_error(error)[:MAX_MESSAGE_LENGTH]


def _reported[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except ToolError:
            raise
        except (ValueError, psycopg.Error, OSError) as error:
            raise ToolError(_message(error)) from error

    return wrapper


INSTRUCTIONS = """\
pgsage proves PostgreSQL query fixes by measuring them. You propose; the database decides.
The pgsage skill (`pgsage install-skill`) holds the full plan-reading and tuning guide.

Workflow:
0. No query yet? get_top_queries lists the costliest ones (values appear as $n placeholders:
   substitute realistic literals first). Pasted an EXPLAIN (ANALYZE, FORMAT JSON) plan instead of
   a query? summarize_plan digests it without a database.
1. get_query_plan(sql): the plan digest plus the raw plan. Start from the node with the largest
   self_ms; a misestimate (estimate_off_by of about 10 or more) or many rows removed by a filter is
   the usual cause.
2. get_table_info(table, schema) for each table in the plan (pass `schema` for anything outside
   public): existing indexes, sizes, column statistics.
3. Propose ONE change and prove it: verify_index_suggestion, verify_statistics_suggestion or
   verify_query_rewrite. Each runs inside a rolled-back transaction (statistics proofs also
   refresh row-count estimates with ANALYZE).
4. Read `improved`. true means measured faster (at least 1.1x and 0.5 ms), the index was used,
   a statistics proof changed the plan, and a rewrite returned the same rows. false is
   information: read `error`, `note` and `equivalent`, then try a different idea or stop. An
   `error` about a missing table or column means the call was wrong, not the idea. Never report a
   change as helpful unless `improved` is true. A proven index or statistics change carries
   `apply`: idempotent SQL to show the user; do not run it unless asked.

These tools run EXPLAIN ANALYZE, which executes the query: use a dev or scratch database.
Only single read-only SELECT statements are accepted (strip a leading EXPLAIN).
"""

server = MCPServer("pgsage", instructions=INSTRUCTIONS)


@server.tool()
@_reported
def get_query_plan(sql: str) -> dict[str, Any]:
    """Run EXPLAIN ANALYZE on one read-only SELECT. Returns `digest` (one row per node with self_ms,
    estimate_off_by and loops already applied: read this first) and the raw `plan`."""
    result = get_plan(_dsn(), sql)
    return {
        "digest": plan_digest(result),
        "plan": result.model_dump(by_alias=True, mode="json", exclude_none=True),
    }


@server.tool()
@_reported
def summarize_plan(plan_json: str) -> list[dict[str, Any]]:
    """Per-node digest for EXPLAIN (ANALYZE, FORMAT JSON) output you already have. Does not touch a database."""
    return plan_digest(parse_explain(plan_json))


@server.tool()
@_reported
def get_table_info(table: str, schema: str = "public") -> dict[str, Any]:
    """Columns, existing indexes, size and column statistics (n_distinct, null_frac, correlation).

    Check this before proposing an index: never propose one that already exists."""
    return table_info(_dsn(), table, schema)


@server.tool()
@_reported
def verify_index_suggestion(
    sql: str,
    table: str,
    columns: list[str],
    schema: str | None = None,
    method: IndexMethod = "btree",
    include: list[str] | None = None,
    where: str | None = None,
) -> dict[str, Any]:
    """Create the index in a rolled-back transaction and compare before and after.

    Returns `improved` (true only if the index was used and the query is measurably faster),
    before/after timings, `error` and `note`. Plain columns only. `where` makes a partial
    index and `include` a covering one (btree only); both are validated. Covering indexes
    often fail to prove a gain on recently written tables, because VACUUM cannot run here.
    """
    suggestion = IndexSuggestion(
        table=table,
        columns=columns,
        schema_name=schema,
        method=method,
        include=include or [],
        where=where,
    )
    result = verify_index(_dsn(writes=True), sql, suggestion)
    return result.as_dict()


@server.tool()
@_reported
def verify_statistics_suggestion(
    sql: str,
    table: str,
    columns: list[str],
    kinds: list[StatisticsKind] | None = None,
    schema: str | None = None,
) -> dict[str, Any]:
    """Create extended statistics (CREATE STATISTICS + ANALYZE) in a rolled-back transaction.

    Use when two or more columns of one table are correlated and the plan shows a misestimate
    (estimate_off_by of about 10 or more). Needs at least two columns. This is the weakest proof:
    it counts only if the plan changed, and ANALYZE refreshes planner estimates permanently.
    """
    suggestion = StatsSuggestion.model_validate(
        {"table": table, "columns": columns, "schema_name": schema}
        | ({"kinds": kinds} if kinds else {})
    )
    result = verify_statistics(_dsn(writes=True), sql, suggestion)
    return result.as_dict()


@server.tool()
@_reported
def get_top_queries(limit: int = 10) -> list[dict[str, Any]]:
    """Most expensive SELECTs from pg_stat_statements. Values appear as $n placeholders: substitute
    realistic literals before passing a query to get_query_plan."""
    return top_queries(_dsn(), limit)


@server.tool()
@_reported
def verify_query_rewrite(sql: str, rewritten_sql: str) -> dict[str, Any]:
    """Check that a rewritten SELECT returns the same columns, types and rows on the current data,
    and measure whether it is faster. `improved` is false if results differ, if the rewrite is
    identical, or if the query uses volatile functions or LIMIT without ORDER BY."""
    result = verify_rewrite(_dsn(writes=True), sql, rewritten_sql)
    return result.as_dict()


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
