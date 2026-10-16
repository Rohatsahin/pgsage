from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable, Collection
from typing import Any, Literal, LiteralString, Self, cast

import psycopg
from psycopg.sql import SQL, Composed
from pydantic import BaseModel, Field, field_validator, model_validator

from pgsage.db import connect, describe_error
from pgsage.plan import ExplainResult, parse_explain
from pgsage.sqlscan import is_ambiguous, mask, mask_keeping_names, quote_ident, substitute

READ_ONLY_START = re.compile(r"^[\s(]*(select|with|values|table)\b", re.IGNORECASE)
FORBIDDEN_KEYWORDS = re.compile(
    r"\b(insert|update|delete|merge|alter|drop|create|truncate|grant|revoke|copy|call|do|vacuum|"
    r"reindex|refresh|lock|notify|listen|unlisten|commit|rollback|savepoint|into|set|reset|"
    r"prepare|execute|declare)\b|\bfor\s+(no\s+key\s+)?(update|share)\b|\bfor\s+key\s+share\b",
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
PLACEHOLDER = re.compile(r"\$\d+")
UNICODE_ESCAPE = re.compile(r"(?<![\w$])[uU]&\s*(?:''|\"\")")
MAX_SQL_CHARS = 1_000_000

MAX_IDENTIFIER_BYTES = 63
LOCK_NOTE = "could not get a lock within 5 s: another session holds a conflicting lock on the table"
SETTING_NOTE = (
    "measured with the setting applied to this session only; making it permanent (ALTER ROLE or "
    "ALTER DATABASE ... SET, or postgresql.conf) is a separate decision that affects every query"
)
TIMEOUT_NOTE = (
    "a statement timed out (60 s limit); a proof runs the query several times, so use a query "
    "that finishes in a few seconds"
)
PARTITIONED_NOTE = (
    "partitioned table: CREATE INDEX CONCURRENTLY is not supported on the parent, so the apply "
    "SQL builds the index without it (writes are blocked while it runs)"
)
DATA_ERROR_CLASSES = ("22", "42", "54")
MIN_SPEEDUP = 1.1
MIN_ABSOLUTE_GAIN_MS = 0.5
MEASURE_RUNS = 3
STATEMENT_TIMEOUT = "60s"
LOCK_TIMEOUT = "5s"

ChangeKind = Literal["index", "rewrite", "statistics", "setting"]


def _identifier_part(value: str) -> str:
    return re.sub(r"\W+", "_", value).strip("_").lower()


def _safe_name(prefix: str, table: str, columns: list[str], extra: tuple[str, ...] = ()) -> str:
    parts = [_identifier_part(p) for p in (table, *columns)]
    readable = f"{prefix}_{'_'.join(parts)}"
    lossy = parts != [table, *columns]
    if not lossy and not extra and len(readable.encode()) <= MAX_IDENTIFIER_BYTES:
        return readable
    digest = hashlib.sha1("\0".join((table, *columns, *extra)).encode()).hexdigest()[:8]
    head = readable.encode()[: MAX_IDENTIFIER_BYTES - len(digest) - 1].decode(errors="ignore")
    return f"{head}_{digest}"


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

SETTING_NAME = re.compile(
    r"^(work_mem|hash_mem_multiplier|random_page_cost|seq_page_cost|cpu_tuple_cost|"
    r"cpu_index_tuple_cost|cpu_operator_cost|effective_cache_size|jit|jit_above_cost|"
    r"jit_inline_above_cost|jit_optimize_above_cost|max_parallel_workers_per_gather|"
    r"parallel_setup_cost|parallel_tuple_cost|from_collapse_limit|join_collapse_limit|geqo|"
    r"enable_[a-z_]+)$"
)
SETTING_VALUE = re.compile(r"^[A-Za-z0-9_.]{1,32}$")
MAX_SETTINGS = 5


SETTING_LIMITS = {
    "hash_mem_multiplier": 100.0,
    "from_collapse_limit": 100.0,
    "join_collapse_limit": 100.0,
    "max_parallel_workers_per_gather": 64.0,
}
WORK_MEM_LIMIT_KB = 4 * 1024 * 1024
MEMORY_UNITS_KB = {"kb": 1, "mb": 1024, "gb": 1024 * 1024, "tb": 1024 * 1024 * 1024}
MEMORY_VALUE = re.compile(r"(\d+)([A-Za-z]*)")


def _check_setting_range(name: str, value: str) -> None:
    if name == "work_mem":
        found = MEMORY_VALUE.fullmatch(value)
        unit = (found.group(2) if found else "kB").lower() or "kb"
        if found is None or unit not in MEMORY_UNITS_KB:
            raise ValueError("work_mem must look like 64MB")
        if int(found.group(1)) * MEMORY_UNITS_KB[unit] > WORK_MEM_LIMIT_KB:
            raise ValueError("work_mem above 4GB is not allowed here")
    limit = SETTING_LIMITS.get(name)
    if limit is not None:
        try:
            number = float(value)
        except ValueError:
            return
        if number > limit:
            raise ValueError(f"{name} above {limit:g} is not allowed here")


class SettingSuggestion(BaseModel):
    settings: dict[str, str] = Field(min_length=1, max_length=MAX_SETTINGS)

    @field_validator("settings")
    @classmethod
    def _allowed(cls, value: dict[str, str]) -> dict[str, str]:
        for name, setting in value.items():
            if not SETTING_NAME.fullmatch(name):
                raise ValueError(
                    f"{name!r} cannot be proven: only planner, memory, JIT and parallelism "
                    "settings are allowed"
                )
            if not SETTING_VALUE.fullmatch(setting):
                raise ValueError(f"the value for {name} must be a plain word or number like 64MB")
            _check_setting_range(name, setting)
        return value

    @property
    def statement(self) -> str:
        return "SET " + ", ".join(f"{name} = '{value}'" for name, value in self.settings.items())

    @property
    def apply_sql(self) -> list[str]:
        return [f"SET {name} = '{value}';" for name, value in self.settings.items()]


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
    if is_ambiguous(predicate):
        reason = "unterminated quote or comment, or nested comments"
    elif _has_comment(predicate):
        reason = "comments are not allowed"
    elif ";" in core:
        reason = "multiple statements are not allowed"
    elif re.search(r"\bselect\b", core, re.IGNORECASE):
        reason = "subqueries are not allowed"
    elif UNICODE_ESCAPE.search(core):
        reason = "unicode-escape identifiers and strings (U&) are not supported"
    elif (found := FORBIDDEN_KEYWORDS.search(core)) or (
        found := FORBIDDEN_FUNCTIONS.search(mask_keeping_names(predicate))
    ):
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

    def create_ddl(
        self, name: str | None = None, if_not_exists: bool = False, concurrently: bool = False
    ) -> str:
        parts = ["CREATE INDEX"]
        if concurrently:
            parts.append("CONCURRENTLY")
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


StatisticsKind = Literal["dependencies", "ndistinct", "mcv"]
DEFAULT_STATISTICS_KINDS: list[StatisticsKind] = ["dependencies", "ndistinct"]


class StatsSuggestion(_TargetSuggestion):
    columns: list[str] = Field(min_length=2)
    kinds: list[StatisticsKind] = Field(default=["dependencies", "ndistinct"], min_length=1)

    @property
    def suggested_name(self) -> str:
        default = sorted(self.kinds) == sorted(DEFAULT_STATISTICS_KINDS)
        return _safe_name("stats", self.table, self.columns, () if default else tuple(self.kinds))

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
    apply: list[str] = []

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
    def reason(self) -> str | None:
        if self.improved or self.error is not None:
            return None
        if self.equivalent is False:
            return self.note or "the results differ"
        if self.kind == "index" and not self.after.uses_new_index:
            return "the planner did not use the new index"
        if self.kind == "statistics" and self.before.node_types == self.after.node_types:
            return "the plan did not change"
        speedup = self.speedup
        if speedup is not None and self.before.execution_ms and self.after.execution_ms:
            gain = self.before.execution_ms - self.after.execution_ms
            return (
                f"{speedup:.2f}x and {gain:.2f} ms is below the {MIN_SPEEDUP}x and "
                f"{MIN_ABSOLUTE_GAIN_MS} ms floor"
            )
        return "the plan cost did not drop"

    def as_dict(self) -> dict[str, Any]:
        speedup = self.speedup
        return {
            **self.model_dump(mode="json"),
            "improved": self.improved,
            "reason": self.reason,
            "speedup": None if speedup is None else round(speedup, 2),
            "apply": self.apply if self.improved else [],
        }

    @property
    def improved(self) -> bool:
        if self.error is not None or self.equivalent is False:
            return False
        if self.kind == "index" and not self.after.uses_new_index:
            return False
        if self.kind == "statistics" and self.before.node_types == self.after.node_types:
            return False
        speedup = self.speedup
        if speedup is not None and self.before.execution_ms and self.after.execution_ms:
            gain = self.before.execution_ms - self.after.execution_ms
            return speedup >= MIN_SPEEDUP and gain >= MIN_ABSOLUTE_GAIN_MS
        return self.after.total_cost < self.before.total_cost


class UnsafeStatementError(ValueError):
    pass


def ensure_read_only(sql: str) -> None:
    if len(sql) > MAX_SQL_CHARS:
        raise UnsafeStatementError(f"the query is longer than {MAX_SQL_CHARS:,} characters")
    core = mask(sql).strip().rstrip(";").strip()
    reason = None
    if is_ambiguous(sql):
        reason = "unterminated quote or comment, or nested comments"
    elif ";" in core:
        reason = "multiple statements are not allowed"
    elif core.lower().startswith("explain"):
        reason = "remove the EXPLAIN keyword; pgsage runs EXPLAIN ANALYZE itself"
    elif not READ_ONLY_START.match(core):
        reason = "only SELECT/WITH/VALUES/TABLE statements are allowed"
    elif UNICODE_ESCAPE.search(core):
        reason = "unicode-escape identifiers and strings (U&) are not supported"
    elif (found := FORBIDDEN_KEYWORDS.search(core)) or (
        found := FORBIDDEN_FUNCTIONS.search(mask_keeping_names(sql))
    ):
        reason = f"`{found.group(0).strip()}` is not allowed"
    if reason:
        if reason.startswith("remove the EXPLAIN"):
            raise UnsafeStatementError(reason)
        raise UnsafeStatementError(
            f"{reason}: pgsage runs the statement with EXPLAIN ANALYZE, so it must be a single "
            "read-only query"
        )


def ensure_runnable(sql: str) -> None:
    ensure_read_only(sql)
    if PLACEHOLDER.search(mask(sql)):
        raise ValueError(
            "the query contains $1-style placeholders (pg_stat_statements replaces constants): "
            "substitute real values first"
        )


def literal(sql: str) -> SQL:
    return SQL(cast("LiteralString", sql))


def run_single(conn: psycopg.Connection[Any], query: SQL | Composed) -> psycopg.Cursor[Any]:
    return conn.execute(query, prepare=True)


def run_explain(conn: psycopg.Connection[Any], sql: str, analyze: bool = True) -> ExplainResult:
    begin_guarded(conn)
    options = "ANALYZE, BUFFERS, VERBOSE, FORMAT JSON" if analyze else "VERBOSE, FORMAT JSON"
    statement = SQL("EXPLAIN ({}) {}").format(SQL(options), literal(sql))
    row = run_single(conn, statement).fetchone()
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


def measure_interleaved(
    conn: psycopg.Connection[Any], first: str, second: str, runs: int = MEASURE_RUNS
) -> tuple[ExplainResult, ExplainResult]:
    firsts: list[ExplainResult] = []
    seconds: list[ExplainResult] = []
    for _ in range(runs):
        firsts.append(run_explain(conn, first))
        seconds.append(run_explain(conn, second))

    def fastest(results: list[ExplainResult]) -> ExplainResult:
        return min(results, key=lambda r: r.execution_time if r.execution_time is not None else 0.0)

    return fastest(firsts), fastest(seconds)


def _summarize(result: ExplainResult, new_index_names: Collection[str] = ()) -> PlanSummary:
    nodes = result.nodes()
    names = set(new_index_names)
    return PlanSummary(
        total_cost=result.plan.total_cost,
        execution_ms=result.execution_time,
        node_types=[node.node_type for node in nodes],
        uses_new_index=bool(names) and any(node.index_name in names for node in nodes),
    )


def _strip(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def _embed(sql: str) -> str:
    return _strip(sql) + "\n"


def top_level(masked: str) -> str:
    depth = 0
    kept: list[str] = []
    for char in masked:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif depth == 0:
            kept.append(char)
    return "".join(kept)


def unprovable_reason(sql: str) -> str | None:
    core = mask(sql)
    top = top_level(core)
    if VOLATILE_FUNCTIONS.search(mask_keeping_names(sql)):
        return "the query calls a volatile function, so its results cannot be compared"
    if ROW_LIMIT.search(top) and not ORDER_BY.search(top):
        return "LIMIT/OFFSET/FETCH without a top-level ORDER BY returns an arbitrary subset"
    if ROW_LIMIT.search(core) and not ORDER_BY.search(core):
        return "a nested LIMIT/OFFSET/FETCH without ORDER BY returns an arbitrary subset"
    return None


def _columns(
    conn: psycopg.Connection[Any], sql: str
) -> list[tuple[str, int, int | None, int | None]]:
    statement = SQL("SELECT * FROM ({}) AS q LIMIT 0").format(literal(_embed(sql)))
    cursor = run_single(conn, statement)
    return [(c.name, c.type_code, c.precision, c.scale) for c in cursor.description or []]


def _as_text(sql: str) -> Composed:
    return SQL("SELECT t::text AS r FROM ({}) AS t").format(literal(_embed(sql)))


def _results_equivalent(
    conn: psycopg.Connection[Any], original: str, rewritten: str
) -> tuple[bool, str | None]:
    reason = unprovable_reason(original) or unprovable_reason(rewritten)
    if reason:
        return False, reason
    if _columns(conn, original) != _columns(conn, rewritten):
        return False, "result columns (names, types or precision) differ"
    if ORDER_BY.search(top_level(mask(original))):
        ordered = SQL("SELECT md5(coalesce(string_agg(md5(r), ''), '')) FROM ({}) AS s")
        hashes = [
            run_single(conn, ordered.format(_as_text(q))).fetchone() for q in (original, rewritten)
        ]
        if hashes[0] != hashes[1]:
            return False, "ordered results differ"
        return True, None
    for left, right in ((original, rewritten), (rewritten, original)):
        diff = SQL("SELECT count(*) FROM (({}) EXCEPT ALL ({})) AS diff")
        row = run_single(conn, diff.format(_as_text(left), _as_text(right))).fetchone()
        if row is None or row[0] != 0:
            return False, "result rows differ"
    return True, None


def _normalize(sql: str) -> str:
    return " ".join(_strip(sql).split())


def _experiment(
    dsn: str,
    sql: str,
    kind: ChangeKind,
    statement: str,
    change: Callable[[psycopg.Connection[Any]], Collection[str] | None],
    before_setup: Callable[[psycopg.Connection[Any]], None] | None = None,
) -> VerifyResult:
    ensure_runnable(sql)
    with connect(dsn) as conn:
        try:
            begin_guarded(conn)
            if before_setup:
                before_setup(conn)
            _lock_read_only(conn)
            before = _summarize(measure(conn, sql))
            conn.rollback()

            begin_guarded(conn)
            new_names = change(conn) or ()
            _lock_read_only(conn)
            after = _summarize(measure(conn, sql), new_names)
        finally:
            conn.rollback()
    return VerifyResult(kind=kind, statement=statement, before=before, after=after)


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


FAMILY_SQL = """
WITH RECURSIVE tree(oid) AS (
    SELECT c.oid FROM pg_class c WHERE c.relname = %s
    UNION ALL
    SELECT i.inhrelid FROM pg_inherits i JOIN tree t ON i.inhparent = t.oid
)
SELECT c.relname FROM tree JOIN pg_class c ON c.oid = tree.oid
"""


def index_apply_sql(suggestion: IndexSuggestion, concurrently: bool = True) -> list[str]:
    ddl = suggestion.create_ddl(
        suggestion.suggested_name, if_not_exists=True, concurrently=concurrently
    )
    return [f"{ddl};"]


def statistics_apply_sql(suggestion: StatsSuggestion) -> list[str]:
    ddl = suggestion.ddl.replace("CREATE STATISTICS ", "CREATE STATISTICS IF NOT EXISTS ", 1)
    return [f"{ddl};", f"ANALYZE {suggestion.qualified_table};"]


def _failed_on_data_error(
    kind: ChangeKind, statement: str, run: Callable[[], VerifyResult]
) -> VerifyResult:
    try:
        return run()
    except psycopg.errors.QueryCanceled:
        return VerifyResult.failed(kind, statement, TIMEOUT_NOTE)
    except psycopg.errors.LockNotAvailable:
        return VerifyResult.failed(kind, statement, LOCK_NOTE)
    except psycopg.Error as error:
        if not (error.sqlstate or "").startswith(DATA_ERROR_CLASSES):
            raise
        return VerifyResult.failed(kind, statement, describe_error(error))


def _is_partitioned(dsn: str, suggestion: IndexSuggestion) -> bool:
    with connect(dsn) as conn:
        begin_guarded(conn)
        row = conn.execute(
            "SELECT relkind FROM pg_class WHERE oid = to_regclass(%s)",
            (suggestion.qualified_table,),
        ).fetchone()
    return bool(row and row[0] == "p")


COVERING_INDEX_SQL = """
SELECT i.relname
FROM pg_index x
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_class t ON t.oid = x.indrelid
JOIN pg_am am ON am.oid = i.relam
WHERE t.oid = to_regclass(%s)
  AND x.indisvalid AND x.indpred IS NULL AND x.indexprs IS NULL
  AND am.amname = %s
  AND (
      SELECT array_agg(a.attname::text ORDER BY k.ord)
      FROM unnest(x.indkey[0:%s]) WITH ORDINALITY AS k(attnum, ord)
      JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
  ) = %s::text[]
LIMIT 1
"""


def _covering_index(dsn: str, suggestion: IndexSuggestion) -> str | None:
    if suggestion.where is not None:
        return None
    width = len(suggestion.columns)
    with connect(dsn) as conn:
        row = conn.execute(
            COVERING_INDEX_SQL,
            (suggestion.qualified_table, suggestion.method, width - 1, suggestion.columns),
        ).fetchone()
    return None if row is None else str(row[0])


def verify_index(dsn: str, sql: str, suggestion: IndexSuggestion) -> VerifyResult:
    index_name = _unique("pgsage_verify_idx")

    def change(conn: psycopg.Connection[Any]) -> Collection[str]:
        run_single(conn, literal(suggestion.create_ddl(index_name)))
        return [row[0] for row in conn.execute(FAMILY_SQL, (index_name,)).fetchall()]

    def run() -> VerifyResult:
        existing = _covering_index(dsn, suggestion)
        if existing is not None:
            return VerifyResult.failed(
                "index",
                suggestion.ddl,
                f"the index {existing} already covers these columns; a new index is not needed",
            )
        result = _experiment(dsn, sql, "index", suggestion.ddl, change)
        partitioned = _is_partitioned(dsn, suggestion)
        update: dict[str, Any] = {"apply": index_apply_sql(suggestion, not partitioned)}
        if partitioned and result.note is None:
            update["note"] = PARTITIONED_NOTE
        return result.model_copy(update=update)

    return _failed_on_data_error("index", suggestion.ddl, run)


def verify_setting(dsn: str, sql: str, suggestion: SettingSuggestion) -> VerifyResult:
    def change(conn: psycopg.Connection[Any]) -> None:
        for name, value in suggestion.settings.items():
            conn.execute("SELECT set_config(%s, %s, true)", (name, value))

    def run() -> VerifyResult:
        result = _experiment(dsn, sql, "setting", suggestion.statement, change)
        return result.model_copy(
            update={"apply": suggestion.apply_sql, "note": result.note or SETTING_NOTE}
        )

    return _failed_on_data_error("setting", suggestion.statement, run)


def verify_statistics(dsn: str, sql: str, suggestion: StatsSuggestion) -> VerifyResult:
    analyze = SQL("ANALYZE {}").format(literal(suggestion.qualified_table))

    def before_setup(conn: psycopg.Connection[Any]) -> None:
        run_single(conn, analyze)

    def change(conn: psycopg.Connection[Any]) -> None:
        run_single(conn, literal(suggestion.create_ddl(_unique("pgsage_verify_stats"))))
        run_single(conn, analyze)

    def run() -> VerifyResult:
        result = _experiment(dsn, sql, "statistics", suggestion.ddl, change, before_setup)
        return result.model_copy(update={"apply": statistics_apply_sql(suggestion)})

    return _failed_on_data_error("statistics", suggestion.ddl, run)


def verify_rewrite(dsn: str, sql: str, rewritten: str) -> VerifyResult:
    ensure_runnable(sql)
    ensure_runnable(rewritten)
    return _failed_on_data_error(
        "rewrite", _strip(rewritten), lambda: _prove_rewrite(dsn, sql, rewritten)
    )


def _prove_rewrite(dsn: str, sql: str, rewritten: str) -> VerifyResult:
    statement = _strip(rewritten)
    if _normalize(sql) == _normalize(rewritten):
        return VerifyResult.failed("rewrite", statement, "the rewrite is identical to the original")
    with connect(dsn) as conn:
        try:
            begin_guarded(conn)
            _lock_read_only(conn)
            equivalent, note = _results_equivalent(conn, sql, rewritten)
            if not equivalent:
                empty = PlanSummary(total_cost=0.0, execution_ms=None, node_types=[])
                return VerifyResult(
                    kind="rewrite",
                    statement=statement,
                    before=empty,
                    after=empty,
                    equivalent=False,
                    note=note,
                )
            before, after = measure_interleaved(conn, sql, rewritten)
        finally:
            conn.rollback()
    return VerifyResult(
        kind="rewrite",
        statement=statement,
        before=_summarize(before),
        after=_summarize(after),
        equivalent=True,
    )
