from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from pgsage.plan import ExplainResult
from pgsage.sqlscan import redact_condition
from pgsage.verify import begin_guarded, ensure_read_only, run_explain

COLUMNS_SQL = """
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = %s AND table_name = %s
ORDER BY ordinal_position
"""

INDEXES_SQL = "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s"

SIZE_SQL = """
SELECT c.reltuples::bigint AS estimated_rows, pg_relation_size(c.oid) AS table_bytes
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s
"""

STATS_SQL = """
SELECT attname AS column_name, n_distinct, null_frac, correlation
FROM pg_stats
WHERE schemaname = %s AND tablename = %s
"""


def get_plan(dsn: str, sql: str) -> ExplainResult:
    ensure_read_only(sql)
    with psycopg.connect(dsn) as conn:
        conn.read_only = True
        try:
            begin_guarded(conn)
            return run_explain(conn, sql)
        finally:
            conn.rollback()


def table_info(dsn: str, table: str, schema: str = "public") -> dict[str, Any]:
    with psycopg.Connection[dict[str, Any]].connect(dsn, row_factory=dict_row) as conn:
        conn.read_only = True
        params = (schema, table)
        columns = conn.execute(COLUMNS_SQL, params).fetchall()
        if not columns:
            raise ValueError(f"table {schema}.{table} not found")
        size = conn.execute(SIZE_SQL, params).fetchone()
        return {
            "table": f"{schema}.{table}",
            "schema": schema,
            "name": table,
            "columns": columns,
            "indexes": [
                {**index, "indexdef": redact_condition(index["indexdef"])}
                for index in conn.execute(INDEXES_SQL, params).fetchall()
            ],
            "size": size,
            "column_stats": conn.execute(STATS_SQL, params).fetchall(),
        }


def tables_in_plan(result: ExplainResult) -> list[tuple[str, str]]:
    seen: dict[tuple[str, str], None] = {}
    for node in result.nodes():
        if node.relation_name:
            seen[(node.schema_name or "public", node.relation_name)] = None
    return list(seen)


TOP_QUERIES_SQL = """
SELECT query, calls, round(total_exec_time::numeric, 1) AS total_ms,
       round(mean_exec_time::numeric, 2) AS mean_ms, rows
FROM pg_stat_statements
WHERE calls >= %s AND query NOT ILIKE '%%pg_stat_statements%%'
  AND query ~* '^\\s*(select|with)\\y'
ORDER BY total_exec_time DESC
LIMIT %s
"""


def top_queries(dsn: str, limit: int = 10, min_calls: int = 1) -> list[dict[str, Any]]:
    with psycopg.Connection[dict[str, Any]].connect(dsn, row_factory=dict_row) as conn:
        conn.read_only = True
        try:
            return conn.execute(TOP_QUERIES_SQL, (min_calls, limit)).fetchall()
        except psycopg.errors.UndefinedTable as error:
            raise ValueError(
                "pg_stat_statements is not available: add it to shared_preload_libraries "
                "and run CREATE EXTENSION pg_stat_statements"
            ) from error
