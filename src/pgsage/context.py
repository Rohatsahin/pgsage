from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from pgsage.plan import ExplainResult
from pgsage.sqlscan import redact_condition
from pgsage.verify import begin_guarded, ensure_runnable, run_explain

COLUMNS_SQL = """
SELECT a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS is_nullable
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p', 'm', 'v', 'f')
ORDER BY a.attnum
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

ANCESTORS_SQL = """
WITH RECURSIVE ancestry(oid) AS (
    SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %s AND c.relname = %s
    UNION
    SELECT i.inhparent FROM pg_inherits i JOIN ancestry a ON i.inhrelid = a.oid
)
SELECT n.nspname AS schema, c.relname AS name
FROM ancestry a JOIN pg_class c ON c.oid = a.oid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT (n.nspname = %s AND c.relname = %s)
"""

TOP_QUERIES_SQL = """
SELECT query, calls, round(total_exec_time::numeric, 1) AS total_ms,
       round(mean_exec_time::numeric, 2) AS mean_ms, rows
FROM pg_stat_statements
WHERE calls >= %s
  AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND query ~* '^\\s*(select|with)\\y'
  AND query !~* '\\y(information_schema|pg_catalog|pg_stats|pg_stat_statements|pg_class|pg_attribute|pg_indexes|pg_inherits)\\y'
ORDER BY total_exec_time DESC
LIMIT %s
"""

MAX_TOP_QUERIES = 100


def _read_only(conn: psycopg.Connection[Any]) -> None:
    conn.read_only = True
    begin_guarded(conn)


def get_plan(dsn: str, sql: str) -> ExplainResult:
    ensure_runnable(sql)
    with psycopg.connect(dsn) as conn:
        _read_only(conn)
        try:
            return run_explain(conn, sql)
        finally:
            conn.rollback()


def table_info(dsn: str, table: str, schema: str = "public") -> dict[str, Any]:
    with psycopg.Connection[dict[str, Any]].connect(dsn, row_factory=dict_row) as conn:
        _read_only(conn)
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


def table_ancestors(dsn: str, table: str, schema: str = "public") -> list[tuple[str, str]]:
    with psycopg.connect(dsn) as conn:
        _read_only(conn)
        rows = conn.execute(ANCESTORS_SQL, (schema, table, schema, table)).fetchall()
        return [(row[0], row[1]) for row in rows]


def tables_in_plan(result: ExplainResult) -> list[tuple[str, str]]:
    seen: dict[tuple[str, str], None] = {}
    for node in result.nodes():
        if node.relation_name:
            seen[(node.schema_name or "public", node.relation_name)] = None
    return list(seen)


def top_queries(dsn: str, limit: int = 10, min_calls: int = 1) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    with psycopg.Connection[dict[str, Any]].connect(dsn, row_factory=dict_row) as conn:
        _read_only(conn)
        try:
            return conn.execute(
                TOP_QUERIES_SQL, (min_calls, min(limit, MAX_TOP_QUERIES))
            ).fetchall()
        except psycopg.errors.UndefinedTable as error:
            raise ValueError(
                "pg_stat_statements is not available: add it to shared_preload_libraries "
                "and run CREATE EXTENSION pg_stat_statements"
            ) from error
        except psycopg.errors.UndefinedColumn as error:
            raise ValueError(
                "this pg_stat_statements has no total_exec_time column: pgsage top needs PostgreSQL 13+"
            ) from error
