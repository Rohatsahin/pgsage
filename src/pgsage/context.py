from __future__ import annotations

from typing import Any

import psycopg

from pgsage.db import connect, connect_dicts
from pgsage.plan import ExplainResult, plan_digest
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
SELECT c.reltuples::bigint AS estimated_rows, pg_relation_size(c.oid) AS table_bytes,
       c.relpages AS pages, c.relallvisible AS all_visible_pages
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s
"""

MAINTENANCE_SQL = """
SELECT last_analyze, last_autoanalyze, last_vacuum, last_autovacuum,
       n_live_tup, n_dead_tup, n_mod_since_analyze
FROM pg_stat_user_tables
WHERE schemaname = %s AND relname = %s
"""

SERVER_SETTINGS = (
    "work_mem",
    "shared_buffers",
    "effective_cache_size",
    "random_page_cost",
    "seq_page_cost",
    "default_statistics_target",
    "max_parallel_workers_per_gather",
    "jit",
    "jit_above_cost",
    "track_io_timing",
)

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
SELECT left(query, 2000) AS query, calls, round(total_exec_time::numeric, 1)::float8 AS total_ms,
       round(mean_exec_time::numeric, 2)::float8 AS mean_ms, rows
FROM pg_stat_statements
WHERE calls >= %s
  AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND query ~* '^\\s*(select|with)\\y'
  AND query !~* '(EXCEPT ALL|AS q LIMIT 0|t::text AS r|set_config\\(|pg_stat_statements_reset)'
  AND query !~* '\\y(information_schema|pg_catalog|pg_stats|pg_stat_statements|pg_class|pg_attribute|pg_indexes|pg_inherits|pg_settings|pg_stat_user_tables|pg_extension|pg_index|pg_am)\\y'
ORDER BY total_exec_time DESC
LIMIT %s
"""

MAX_TOP_QUERIES = 100
MAX_TABLES = 20
MAX_COLUMNS = 150
MAX_INDEXES = 50
MAX_DIGEST_ROWS = 150
MAX_QUERY_TEXT = 2000


def _read_only(conn: psycopg.Connection[Any]) -> None:
    conn.read_only = True
    begin_guarded(conn)


def get_plan(dsn: str, sql: str, analyze: bool = True) -> ExplainResult:
    ensure_runnable(sql)
    with connect(dsn) as conn:
        _read_only(conn)
        try:
            return run_explain(conn, sql, analyze)
        except psycopg.errors.QueryCanceled as error:
            raise ValueError(
                "the query did not finish within the 60 s limit; use `inspect --no-analyze` "
                "(estimates only, the query is not run) or ask the user for an "
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) plan"
            ) from error
        finally:
            conn.rollback()


def _capped(rows: list[dict[str, Any]], limit: int, omitted_key: str) -> dict[str, Any]:
    return {omitted_key: len(rows) - limit} if len(rows) > limit else {}


def _table_facts(
    conn: psycopg.Connection[dict[str, Any]], schema: str, table: str
) -> dict[str, Any]:
    params = (schema, table)
    columns = conn.execute(COLUMNS_SQL, params).fetchall()
    if not columns:
        raise ValueError(f"table {schema}.{table} not found")
    indexes = conn.execute(INDEXES_SQL, params).fetchall()
    stats = conn.execute(STATS_SQL, params).fetchall()
    return {
        "table": f"{schema}.{table}",
        "schema": schema,
        "name": table,
        "columns": columns[:MAX_COLUMNS],
        "indexes": indexes[:MAX_INDEXES],
        "size": conn.execute(SIZE_SQL, params).fetchone(),
        "maintenance": conn.execute(MAINTENANCE_SQL, params).fetchone() or {},
        "column_stats": stats[:MAX_COLUMNS],
        **_capped(columns, MAX_COLUMNS, "columns_omitted"),
        **_capped(indexes, MAX_INDEXES, "indexes_omitted"),
    }


def table_info(dsn: str, table: str, schema: str = "public") -> dict[str, Any]:
    with connect_dicts(dsn) as conn:
        _read_only(conn)
        return _table_facts(conn, schema, table)


def server_info(dsn: str) -> dict[str, Any]:
    with connect_dicts(dsn) as conn:
        _read_only(conn)
        version = conn.execute("SHOW server_version").fetchone() or {}
        rows = conn.execute(
            "SELECT name, setting, unit FROM pg_settings WHERE name = ANY(%s)",
            (list(SERVER_SETTINGS),),
        ).fetchall()
        changed = conn.execute(
            "SELECT name, setting FROM pg_settings "
            "WHERE name LIKE 'enable\\_%%' AND setting <> boot_val ORDER BY name"
        ).fetchall()
        return {
            "version": version.get("server_version"),
            "settings": {row["name"]: f"{row['setting']}{row['unit'] or ''}" for row in rows},
            "planner_options_changed": {row["name"]: row["setting"] for row in changed},
        }


def table_ancestors(dsn: str, table: str, schema: str = "public") -> list[tuple[str, str]]:
    with connect(dsn) as conn:
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
    with connect_dicts(dsn) as conn:
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


def collect_tables(dsn: str, plan_tables: list[tuple[str, str]]) -> list[dict[str, Any]]:
    collected: dict[tuple[str, str], dict[str, Any]] = {}
    with connect_dicts(dsn) as conn:
        _read_only(conn)
        for schema, name in plan_tables:
            ancestry = conn.execute(ANCESTORS_SQL, (schema, name, schema, name)).fetchall()
            for candidate in [(schema, name), *((row["schema"], row["name"]) for row in ancestry)]:
                if candidate in collected:
                    continue
                if len(collected) >= MAX_TABLES:
                    return list(collected.values())
                try:
                    collected[candidate] = _table_facts(conn, *candidate)
                except ValueError:
                    continue
    return list(collected.values())


def _bounded_digest(digest: list[dict[str, Any]]) -> dict[str, Any]:
    if len(digest) <= MAX_DIGEST_ROWS:
        return {"digest": digest}
    hottest = sorted(
        range(len(digest)), key=lambda i: digest[i].get("self_ms") or 0.0, reverse=True
    )
    keep = set(hottest[:MAX_DIGEST_ROWS])
    return {
        "digest": [row for i, row in enumerate(digest) if i in keep],
        "digest_rows_omitted": len(digest) - MAX_DIGEST_ROWS,
    }


def inspect_query(
    dsn: str, sql: str, raw_plan: bool = False, analyze: bool = True
) -> dict[str, Any]:
    plan = get_plan(dsn, sql, analyze)
    plan_tables = tables_in_plan(plan)
    found: dict[str, Any] = {
        "baseline_ms": plan.execution_time,
        **_bounded_digest(plan_digest(plan)),
        "server": server_info(dsn),
        "tables": collect_tables(dsn, plan_tables),
    }
    if len(plan_tables) > MAX_TABLES:
        found["tables_omitted"] = len(plan_tables) - MAX_TABLES
    if raw_plan:
        found["plan"] = plan.model_dump(by_alias=True, mode="json", exclude_none=True)
    return found
