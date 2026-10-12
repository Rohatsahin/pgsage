from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from pgsage.context import get_plan, table_info, top_queries
from pgsage.plan import parse_explain, plan_digest
from pgsage.verify import (
    IndexMethod,
    IndexSuggestion,
    UnsafeStatementError,
    verify_index,
    verify_rewrite,
)

DSN_ENV = "PGSAGE_DSN"


def _dsn() -> str:
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        raise ValueError(f"set {DSN_ENV} in the MCP server environment to a dev/scratch database")
    return dsn


server = MCPServer(
    "pgsage",
    instructions=(
        "Tools for tuning PostgreSQL queries on the database configured in PGSAGE_DSN. Typical loop: get_plan, table_info for each "
        "table in the plan, propose an index, then verify_index_suggestion to prove it helps. "
        "These tools run EXPLAIN ANALYZE: only point them at a dev or scratch database."
    ),
)


@server.tool()
def get_query_plan(sql: str) -> dict[str, Any]:
    """Run EXPLAIN ANALYZE on a read-only SELECT; returns the plan digest and the raw plan."""
    try:
        result = get_plan(_dsn(), sql)
    except UnsafeStatementError as error:
        raise ValueError(str(error)) from error
    return {
        "digest": plan_digest(result),
        "plan": result.model_dump(by_alias=True, mode="json", exclude_none=True),
    }


@server.tool()
def summarize_plan(plan_json: str) -> list[dict[str, Any]]:
    """Per-node digest (self time, estimate error, loops) for existing EXPLAIN JSON output."""
    return plan_digest(parse_explain(plan_json))


@server.tool()
def get_table_info(table: str, schema: str = "public") -> dict[str, Any]:
    """Columns, indexes, size and column statistics for a table."""
    return table_info(_dsn(), table, schema)


@server.tool()
def verify_index_suggestion(
    sql: str,
    table: str,
    columns: list[str],
    schema: str | None = None,
    method: IndexMethod = "btree",
    include: list[str] | None = None,
    where: str | None = None,
) -> dict[str, Any]:
    """Create the index in a rolled-back transaction and compare the plan before and after.

    `where` makes a partial index and `include` a covering index (btree only); both are validated.
    """
    suggestion = IndexSuggestion(
        table=table,
        columns=columns,
        schema_name=schema,
        method=method,
        include=include or [],
        where=where,
    )
    try:
        result = verify_index(_dsn(), sql, suggestion)
    except UnsafeStatementError as error:
        raise ValueError(str(error)) from error
    return {**result.model_dump(mode="json"), "improved": result.improved}


@server.tool()
def get_top_queries(limit: int = 10) -> list[dict[str, Any]]:
    """Most expensive SELECTs from pg_stat_statements (values appear as $n placeholders)."""
    return top_queries(_dsn(), limit)


@server.tool()
def verify_query_rewrite(sql: str, rewritten_sql: str) -> dict[str, Any]:
    """Check a rewritten SELECT returns identical rows and measure whether it is faster."""
    try:
        result = verify_rewrite(_dsn(), sql, rewritten_sql)
    except UnsafeStatementError as error:
        raise ValueError(str(error)) from error
    return {**result.model_dump(mode="json"), "improved": result.improved}


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
