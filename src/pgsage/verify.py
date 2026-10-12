from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from typing import Any, Literal, LiteralString, Self, cast

import psycopg
from psycopg.sql import SQL, Composable
from pydantic import BaseModel, Field, field_validator, model_validator

from pgsage.plan import ExplainResult, parse_explain
from pgsage.sqlscan import mask, quote_ident, substitute

READ_ONLY_START = re.compile(r"^[\s(]*(select|with|values|table)\b", re.IGNORECASE)
FORBIDDEN_KEYWORDS = re.compile(
    r"\b(insert|update|delete|merge|alter|drop|create|truncate|grant|revoke|copy|call|do|vacuum|"
    r"reindex|refresh|lock|notify|listen|unlisten|commit|rollback|savepoint|into|set|reset|"
    r"prepare|execute|declare|fetch)\b|\bfor\s+(no\s+key\s+)?(update|share)\b|\bfor\s+key\s+share\b",
    re.IGNORECASE,
)
FORBIDDEN_FUNCTIONS = re.compile(
    r"\b(pg_sleep\w*|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|"
    r"pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|pg_file_\w+|lo_\w+|dblink\w*|"
    r"set_config|nextval|setval|pg_advisory\w*|pg_notify|query_to_xml\w*|"
    r"cursor_to_xml\w*|table_to_xml\w*)\s*\(",
    re.IGNORECASE,
)
VOLATILE_FUNCTIONS = re.compile(
    r"\b(random|clock_timestamp|timeofday|gen_random_uuid|uuid_generate_v\w+|statement_timestamp)\s*\(",
    re.IGNORECASE,
)
ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)
ROW_LIMIT = re.compile(r"\b(limit|offset|fetch)\b", re.IGNORECASE)

MIN_SPEEDUP = 1.1
MEASURE_RUNS = 3
STATEMENT_TIMEOUT = "60s"
LOCK_TIMEOUT = "5s"

ChangeKind = Literal["index", "rewrite", "statistics"]


def _identifier_part(value: str) -> str:
    return re.sub(r"\W+", "_", value).strip("_").lower()


def _safe_name(prefix: str, table: str, columns: list[str], extra: tuple[str, ...] = ()) -> str:
    parts = [_identifier_part(p) for p in (table, *columns)]
    readable = f"{prefix}_{'_'.join(parts)}"
    lossy = parts != [table, *columns]
    if not lossy and not extra and len(readable) <= 63:
        return readable
    digest = hashlib.sha1("\0".join((table, *columns, *extra)).encode()).hexdigest()[:8]
    return f"{readable[:54]}_{digest}"


class _TargetSuggestion(BaseModel):
    table: str = Field(min_length=1)
    columns: list[str] = Field(min_length=1)
    schema_name: str | None = None

    @field_validator("table", "schema_name")
    @classmethod
    def _no_nul_or_blank(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("identifier must be non-empty and contain no NUL character")
        return value

    @field_validator("columns")
    @classmethod
    def _columns_valid(cls, value: list[str]) -> list[str]:
        if any(not c.strip() or "\x00" in c for c in value):
            raise ValueError("column names must be non-empty and contain no NUL character")
        return value

    @property
    def qualified_table(self) -> str:
        if self.schema_name:
            return f"{quote_ident(self.schema_name)}.{quote_ident(self.table)}"
        return quote_ident(self.table)

    @property
    def quoted_columns(self) -> str:
        return ", ".join(quote_ident(c) for c in self.columns)


IndexMethod = Literal["btree", "hash", "gin", "brin"]
MAX_PREDICATE_LENGTH = 500


def _has_comment(sql: str) -> bool:
    found: list[str] = []

    def handler(kind: str, text: str) -> str:
        if kind == "comment":
            found.append(text)
        return text

    substitute(sql, handler)
    return bool(found)


def ensure_safe_predicate(predicate: str) -> None:
    core = mask(predicate).strip()
    if not core or len(predicate) > MAX_PREDICATE_LENGTH:
        raise ValueError("partial index predicate must be 1-500 characters")
    reason = None
    if _has_comment(predicate):
        reason = "comments are not allowed"
    elif ";" in core:
        reason = "multiple statements are not allowed"
    elif re.search(r"\bselect\b", core, re.IGNORECASE):
        reason = "subqueries are not allowed"
    elif (found := FORBIDDEN_KEYWORDS.search(core)) or (found := FORBIDDEN_FUNCTIONS.search(core)):
        reason = f"`{found.group(0).strip()}` is not allowed"
    if reason:
        raise ValueError(f"unsafe partial index predicate: {reason}")


class IndexSuggestion(_TargetSuggestion):
    method: IndexMethod = "btree"
    include: list[str] = Field(default_factory=lambda: list[str]())
    where: str | None = None

    @field_validator("include")
    @classmethod
    def _include_valid(cls, value: list[str]) -> list[str]:
        if any(not c.strip() or "\x00" in c for c in value):
            raise ValueError("include column names must be non-empty and contain no NUL character")
        return value

    @field_validator("where")
    @classmethod
    def _where_safe(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        if value is not None:
            ensure_safe_predicate(value)
        return value

    @model_validator(mode="after")
    def _method_constraints(self) -> Self:
        if self.include and self.method != "btree":
            raise ValueError("INCLUDE columns are only supported for btree indexes")
        return self

    @property
    def all_columns(self) -> list[str]:
        return [*self.columns, *self.include]

    @property
    def suggested_name(self) -> str:
        extra = (self.method, *self.include, self.where or "")
        plain = self.method == "btree" and not self.include and not self.where
        return _safe_name("idx", self.table, self.columns, () if plain else extra)

    def create_ddl(self, name: str | None = None, if_not_exists: bool = False) -> str:
        parts = ["CREATE INDEX"]
        if if_not_exists:
            parts.append("IF NOT EXISTS")
        if name:
            parts.append(quote_ident(name))
        parts.append(f"ON {self.qualified_table}")
        if self.method != "btree":
            parts.append(f"USING {self.method}")
        parts.append(f"({self.quoted_columns})")
        if self.include:
            parts.append(f"INCLUDE ({', '.join(quote_ident(c) for c in self.include)})")
        if self.where:
            parts.append(f"WHERE {self.where}")
        return " ".join(parts)

    @property
    def ddl(self) -> str:
        return self.create_ddl()


class StatsSuggestion(_TargetSuggestion):
    kinds: list[Literal["dependencies", "ndistinct", "mcv"]] = ["dependencies", "ndistinct"]

    @property
    def suggested_name(self) -> str:
        return _safe_name("stats", self.table, self.columns)

    def create_ddl(self, name: str | None = None) -> str:
        label = quote_ident(name or self.suggested_name)
        if self.schema_name:
            label = f"{quote_ident(self.schema_name)}.{label}"
        return (
            f"CREATE STATISTICS {label} ({', '.join(self.kinds)}) "
            f"ON {self.quoted_columns} FROM {self.qualified_table}"
        )

    @property
    def ddl(self) -> str:
        return self.create_ddl()


class PlanSummary(BaseModel):
    total_cost: float
    execution_ms: float | None
    node_types: list[str]
    uses_new_index: bool = False


class VerifyResult(BaseModel):
    kind: ChangeKind
    statement: str
    before: PlanSummary
    after: PlanSummary
    equivalent: bool | None = None
    note: str | None = None
    error: str | None = None

    @classmethod
    def failed(cls, kind: ChangeKind, statement: str, error: str) -> Self:
        empty = PlanSummary(total_cost=0.0, execution_ms=None, node_types=[])
        return cls(kind=kind, statement=statement, before=empty, after=empty, error=error)

    @property
    def ddl(self) -> str:
        return self.statement

    @property
    def speedup(self) -> float | None:
        if not self.before.execution_ms or not self.after.execution_ms:
            return None
        return self.before.execution_ms / self.after.execution_ms

    @property
    def improved(self) -> bool:
        if self.error is not None or self.equivalent is False:
            return False
        if self.kind == "index" and not self.after.uses_new_index:
            return False
        speedup = self.speedup
        if speedup is not None:
            return speedup >= MIN_SPEEDUP
        return self.after.total_cost < self.before.total_cost


class UnsafeStatementError(ValueError):
    pass


def ensure_read_only(sql: str) -> None:
    core = mask(sql).strip().rstrip(";").strip()
    reason = None
    if ";" in core:
        reason = "multiple statements are not allowed"
    elif not READ_ONLY_START.match(core):
        reason = "only SELECT/WITH/VALUES/TABLE statements are allowed"
    elif (found := FORBIDDEN_KEYWORDS.search(core)) or (found := FORBIDDEN_FUNCTIONS.search(core)):
        reason = f"`{found.group(0).strip()}` is not allowed"
    if reason:
        raise UnsafeStatementError(
            f"{reason}: verify runs the statement with EXPLAIN ANALYZE, so it must be a single "
            "read-only query"
        )


def literal(sql: str) -> SQL:
    return SQL(cast("LiteralString", sql))


def run_explain(conn: psycopg.Connection[Any], sql: str) -> ExplainResult:
    statement = SQL("EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON) {}").format(literal(sql))
    row = conn.execute(statement).fetchone()
    if row is None:
        raise RuntimeError("EXPLAIN returned no rows")
    return parse_explain(row[0])


def measure(conn: psycopg.Connection[Any], sql: str, runs: int = MEASURE_RUNS) -> ExplainResult:
    results = [run_explain(conn, sql) for _ in range(runs)]
    return min(results, key=lambda r: r.execution_time if r.execution_time is not None else 0.0)


def begin_guarded(conn: psycopg.Connection[Any]) -> None:
    conn.execute(literal(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
    conn.execute(literal(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))


def _lock_read_only(conn: psycopg.Connection[Any]) -> None:
    conn.execute("SET TRANSACTION READ ONLY")


def _summarize(result: ExplainResult, index_name: str | None = None) -> PlanSummary:
    nodes = result.nodes()
    return PlanSummary(
        total_cost=result.plan.total_cost,
        execution_ms=result.execution_time,
        node_types=[node.node_type for node in nodes],
        uses_new_index=index_name is not None
        and any(node.index_name == index_name for node in nodes),
    )


def _strip(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def _unprovable_reason(sql: str) -> str | None:
    core = mask(sql)
    if VOLATILE_FUNCTIONS.search(core):
        return "the query calls a volatile function, so its results cannot be compared"
    if ROW_LIMIT.search(core) and not ORDER_BY.search(core):
        return "LIMIT/OFFSET/FETCH without ORDER BY returns an arbitrary subset"
    return None


def _columns(conn: psycopg.Connection[Any], sql: str) -> list[tuple[str, int]]:
    statement = SQL("SELECT * FROM ({}) AS q LIMIT 0").format(literal(_strip(sql)))
    cursor = conn.execute(statement)
    return [(column.name, column.type_code) for column in cursor.description or []]


def _as_text(sql: str) -> Composable:
    return SQL("SELECT t::text AS r FROM ({}) AS t").format(literal(_strip(sql)))


def _results_equivalent(
    conn: psycopg.Connection[Any], original: str, rewritten: str
) -> tuple[bool, str | None]:
    reason = _unprovable_reason(original) or _unprovable_reason(rewritten)
    if reason:
        return False, reason
    if _columns(conn, original) != _columns(conn, rewritten):
        return False, "result columns (names or types) differ"
    if ORDER_BY.search(mask(original)):
        ordered = SQL("SELECT md5(coalesce(string_agg(r, E'\\n'), '')) FROM ({}) AS s")
        hashes = [
            conn.execute(ordered.format(_as_text(q))).fetchone() for q in (original, rewritten)
        ]
        if hashes[0] != hashes[1]:
            return False, "ordered results differ"
        return True, None
    for left, right in ((original, rewritten), (rewritten, original)):
        diff = SQL("SELECT count(*) FROM (({}) EXCEPT ALL ({})) AS diff")
        row = conn.execute(diff.format(_as_text(left), _as_text(right))).fetchone()
        if row is None or row[0] != 0:
            return False, "result rows differ"
    return True, None


def _experiment(
    dsn: str,
    sql: str,
    kind: ChangeKind,
    statement: str,
    change: Callable[[psycopg.Connection[Any]], None],
    before_setup: Callable[[psycopg.Connection[Any]], None] | None = None,
    after_sql: str | None = None,
    index_name: str | None = None,
) -> VerifyResult:
    ensure_read_only(sql)
    if after_sql is not None:
        ensure_read_only(after_sql)
    with psycopg.connect(dsn) as conn:
        try:
            begin_guarded(conn)
            if before_setup:
                before_setup(conn)
            _lock_read_only(conn)
            before = _summarize(measure(conn, sql))
            conn.rollback()

            begin_guarded(conn)
            change(conn)
            _lock_read_only(conn)
            equivalent, note = (
                _results_equivalent(conn, sql, after_sql) if after_sql is not None else (None, None)
            )
            after = _summarize(measure(conn, after_sql or sql), index_name)
        finally:
            conn.rollback()
    return VerifyResult(
        kind=kind,
        statement=statement,
        before=before,
        after=after,
        equivalent=equivalent,
        note=note,
    )


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def verify_index(dsn: str, sql: str, suggestion: IndexSuggestion) -> VerifyResult:
    index_name = _unique("pgsage_verify_idx")

    def change(conn: psycopg.Connection[Any]) -> None:
        conn.execute(literal(suggestion.create_ddl(index_name)))

    return _experiment(dsn, sql, "index", suggestion.ddl, change, index_name=index_name)


def verify_statistics(dsn: str, sql: str, suggestion: StatsSuggestion) -> VerifyResult:
    analyze = SQL("ANALYZE {}").format(literal(suggestion.qualified_table))

    def before_setup(conn: psycopg.Connection[Any]) -> None:
        conn.execute(analyze)

    def change(conn: psycopg.Connection[Any]) -> None:
        conn.execute(literal(suggestion.create_ddl(_unique("pgsage_verify_stats"))))
        conn.execute(analyze)

    return _experiment(dsn, sql, "statistics", suggestion.ddl, change, before_setup)


def verify_rewrite(dsn: str, sql: str, rewritten: str) -> VerifyResult:
    return _experiment(
        dsn, sql, "rewrite", _strip(rewritten), lambda conn: None, after_sql=rewritten
    )
