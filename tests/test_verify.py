from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest

from pgsage import IndexSuggestion, parse_explain
from pgsage import verify as verify_module
from pgsage.context import get_plan
from pgsage.verify import (
    MEASURE_RUNS,
    MIN_ABSOLUTE_GAIN_MS,
    MIN_SPEEDUP,
    PlanSummary,
    StatsSuggestion,
    UnsafeStatementError,
    VerifyResult,
    begin_guarded,
    index_apply_sql,
    measure,
    measure_interleaved,
    statistics_apply_sql,
    unprovable_reason,
    verify_index,
    verify_rewrite,
    verify_statistics,
)


@pytest.fixture
def eq(pg_conn: psycopg.Connection) -> Iterator[None]:
    pg_conn.execute("DROP TABLE IF EXISTS eq")
    pg_conn.execute(
        "CREATE TABLE eq AS SELECT g AS id, g % 10 AS grp, ('{\"a\": ' || g || '}')::json AS doc "
        "FROM generate_series(1, 2000) g"
    )
    pg_conn.execute("ANALYZE eq")
    yield
    pg_conn.execute("DROP TABLE IF EXISTS eq")


def check(dsn: str, original: str, rewritten: str) -> tuple[bool | None, str | None]:
    result = verify_rewrite(dsn, original, rewritten)
    assert not result.improved or result.equivalent
    return result.equivalent, result.note


INJECTION = 'id"); COMMIT; DROP TABLE victim; --'


def exists(conn: psycopg.Connection, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()
    return row == (True,)


def leaked_verify_objects(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT count(*) FROM pg_class WHERE relname LIKE 'pgsage_verify%%'"
    ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def side_effect_function(pg_conn: psycopg.Connection) -> Iterator[None]:
    pg_conn.execute("DROP TABLE IF EXISTS counter")
    pg_conn.execute("CREATE TABLE counter (n int)")
    pg_conn.execute(
        "CREATE OR REPLACE FUNCTION bump() RETURNS int LANGUAGE plpgsql AS "
        "$$ BEGIN INSERT INTO counter VALUES (1); RETURN 1; END $$"
    )
    yield
    pg_conn.execute("DROP FUNCTION IF EXISTS bump()")
    pg_conn.execute("DROP TABLE IF EXISTS counter")


def counter_rows(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM counter").fetchone()
    assert row is not None
    return int(row[0])


def summary(ms: float | None, cost: float = 100.0, uses_index: bool = False) -> PlanSummary:
    return PlanSummary(total_cost=cost, execution_ms=ms, node_types=[], uses_new_index=uses_index)


def result(
    before: PlanSummary,
    after: PlanSummary,
    kind: Any = "rewrite",
    equivalent: bool | None = None,
    error: str | None = None,
) -> VerifyResult:
    return VerifyResult(
        kind=kind, statement="x", before=before, after=after, equivalent=equivalent, error=error
    )


def plan(ms: float) -> Any:
    return parse_explain(
        [
            {
                "Plan": {
                    "Node Type": "Seq Scan",
                    "Startup Cost": 0,
                    "Total Cost": 1,
                    "Plan Rows": 1,
                },
                "Execution Time": ms,
            }
        ]
    )


QUERY = "SELECT * FROM orders WHERE customer_id = 7"


@pytest.fixture
def partitioned(pg_conn: psycopg.Connection) -> Iterator[None]:
    pg_conn.execute("DROP TABLE IF EXISTS pt")
    pg_conn.execute("CREATE TABLE pt (id int, k int) PARTITION BY RANGE (id)")
    pg_conn.execute("CREATE TABLE pt1 PARTITION OF pt FOR VALUES FROM (0) TO (100000)")
    pg_conn.execute("CREATE TABLE pt2 PARTITION OF pt FOR VALUES FROM (100000) TO (200000)")
    pg_conn.execute("INSERT INTO pt SELECT g, g % 500 FROM generate_series(1, 199999) g")
    pg_conn.execute("ANALYZE pt")
    yield
    pg_conn.execute("DROP TABLE IF EXISTS pt")


PARTITIONED_QUERY = "SELECT * FROM pt WHERE id < 90000 AND k = 3"


class Spy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
        self.calls = 0
        original: Callable[..., None] = module.begin_guarded

        def wrapper(conn: Any) -> None:
            self.calls += 1
            original(conn)

        monkeypatch.setattr(module, "begin_guarded", wrapper)


def test_identical_semantics_are_equivalent_even_for_json_columns(pg_dsn: str, eq: None) -> None:
    equivalent, _ = check(
        pg_dsn, "SELECT id, doc FROM eq WHERE grp = 3", "SELECT id, doc FROM eq WHERE 3 = grp"
    )
    assert equivalent is True


@pytest.mark.parametrize(
    ("original", "rewritten", "reason"),
    [
        ("SELECT id AS a FROM eq", "SELECT id AS b FROM eq", "columns"),
        ("SELECT id FROM eq", "SELECT id::numeric AS id FROM eq", "columns"),
        ("SELECT id, grp FROM eq", "SELECT grp, id FROM eq", "columns"),
        ("SELECT id FROM eq ORDER BY id DESC", "SELECT id FROM eq", "ordered"),
        ("SELECT id FROM eq WHERE grp = 1", "SELECT id FROM eq WHERE grp = 2", "rows"),
        ("SELECT id FROM eq LIMIT 5", "SELECT id FROM eq ORDER BY id LIMIT 5", "arbitrary"),
        ("SELECT random() FROM eq", "SELECT random() FROM eq WHERE true", "volatile"),
    ],
)
def test_not_equivalent_or_unprovable(
    pg_dsn: str, eq: None, original: str, rewritten: str, reason: str
) -> None:
    equivalent, note = check(pg_dsn, original, rewritten)
    assert equivalent is False
    assert note is not None


def test_ordered_queries_are_compared_in_order(pg_dsn: str, eq: None) -> None:
    equivalent, _ = check(
        pg_dsn,
        "SELECT id FROM eq WHERE grp = 1 ORDER BY id",
        "SELECT id FROM eq WHERE grp = 1 ORDER BY id ASC",
    )
    assert equivalent is True


def test_a_rewrite_that_returns_extra_rows_is_not_equivalent(pg_dsn: str, eq: None) -> None:
    equivalent, note = check(
        pg_dsn,
        "SELECT id FROM eq WHERE grp = 1",
        "SELECT id FROM eq WHERE grp IN (1, 2)",
    )
    assert equivalent is False
    assert note == "result rows differ"


def test_a_rewrite_that_returns_fewer_rows_is_not_equivalent(pg_dsn: str, eq: None) -> None:
    equivalent, _ = check(
        pg_dsn,
        "SELECT id FROM eq WHERE grp IN (1, 2)",
        "SELECT id FROM eq WHERE grp = 1",
    )
    assert equivalent is False


def test_numeric_precision_and_scale_are_part_of_the_column_identity(pg_dsn: str, eq: None) -> None:
    equivalent, note = check(
        pg_dsn,
        "SELECT id::numeric(10,2) AS v FROM eq",
        "SELECT id::numeric AS v FROM eq",
    )
    assert equivalent is False
    assert note is not None and "columns" in note


def test_a_window_order_by_does_not_switch_on_ordered_comparison(pg_dsn: str, eq: None) -> None:
    equivalent, _ = check(
        pg_dsn,
        "SELECT id, row_number() OVER (ORDER BY id) AS rn FROM eq WHERE grp = 1",
        "SELECT id, row_number() OVER (ORDER BY id) AS rn FROM eq WHERE 1 = grp",
    )
    assert equivalent is True


def test_a_limit_with_only_a_window_order_by_is_unprovable(pg_dsn: str, eq: None) -> None:
    equivalent, note = check(
        pg_dsn,
        "SELECT id, row_number() OVER (ORDER BY id) AS rn FROM eq LIMIT 5",
        "SELECT id, row_number() OVER (ORDER BY id) AS rn FROM eq WHERE true LIMIT 5",
    )
    assert equivalent is False
    assert note is not None and "top-level ORDER BY" in note


def test_column_name_injection_cannot_commit_or_drop(
    pg_dsn: str, pg_conn: psycopg.Connection, victim: None
) -> None:
    result = verify_index(
        pg_dsn,
        "SELECT * FROM orders WHERE id = 5",
        IndexSuggestion(table="orders", columns=[INJECTION]),
    )
    assert not result.improved and result.error
    assert exists(pg_conn, "victim")
    assert leaked_verify_objects(pg_conn) == 0


def test_table_name_injection_cannot_commit_or_drop(
    pg_dsn: str, pg_conn: psycopg.Connection, victim: None
) -> None:
    result = verify_index(
        pg_dsn,
        "SELECT * FROM orders WHERE id = 5",
        IndexSuggestion(table='orders"; COMMIT; DROP TABLE victim; --', columns=["id"]),
    )
    assert not result.improved and result.error
    assert exists(pg_conn, "victim")


def test_schema_name_injection_cannot_commit_or_drop(
    pg_dsn: str, pg_conn: psycopg.Connection, victim: None
) -> None:
    with pytest.raises(psycopg.Error):
        verify_index(
            pg_dsn,
            "SELECT * FROM orders WHERE id = 5",
            IndexSuggestion(
                table="orders", columns=["id"], schema_name='x"; DROP TABLE victim; --'
            ),
        )
    assert exists(pg_conn, "victim")


def test_user_defined_side_effects_are_blocked_by_the_read_only_transaction(
    pg_dsn: str, pg_conn: psycopg.Connection, side_effect_function: None
) -> None:
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        get_plan(pg_dsn, "SELECT bump()")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        verify_rewrite(pg_dsn, "SELECT bump() AS bump", "SELECT bump() + 0 AS bump")
    assert counter_rows(pg_conn) == 0


def test_proof_phases_are_read_only_even_for_the_after_measurement(
    pg_dsn: str, pg_conn: psycopg.Connection, side_effect_function: None
) -> None:
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        verify_index(
            pg_dsn,
            "SELECT bump() FROM orders WHERE customer_id = 7 LIMIT 1",
            IndexSuggestion(table="orders", columns=["customer_id"]),
        )
    assert counter_rows(pg_conn) == 0


def test_every_transaction_carries_a_statement_timeout(pg_dsn: str) -> None:
    with psycopg.connect(pg_dsn) as conn:
        begin_guarded(conn)
        row = conn.execute("SHOW statement_timeout").fetchone()
        assert row == ("1min",)
        row = conn.execute("SHOW lock_timeout").fetchone()
        assert row == ("5s",)


def test_the_speedup_floor_is_exactly_the_documented_one() -> None:
    assert MIN_SPEEDUP == 1.1
    just_over = result(summary(100.0), summary(100.0 / 1.11))
    just_under = result(summary(100.0), summary(100.0 / 1.09))
    assert just_over.improved is True
    assert just_under.improved is False


def test_a_big_ratio_on_a_microscopic_query_is_not_a_win() -> None:
    assert MIN_ABSOLUTE_GAIN_MS > 0
    assert result(summary(0.056), summary(0.003)).improved is False
    assert result(summary(0.6), summary(0.05)).improved is True


def test_equal_or_slower_is_never_improved() -> None:
    assert result(summary(50.0), summary(50.0)).improved is False
    assert result(summary(50.0), summary(80.0)).improved is False


def test_an_index_that_the_plan_does_not_use_is_never_proven_however_fast() -> None:
    unused = result(summary(100.0), summary(1.0, uses_index=False), kind="index")
    used = result(summary(100.0), summary(1.0, uses_index=True), kind="index")
    assert unused.improved is False
    assert used.improved is True


def test_an_error_or_a_semantic_difference_always_blocks() -> None:
    fast = summary(1.0)
    assert result(summary(100.0), fast, error="boom").improved is False
    assert result(summary(100.0), fast, equivalent=False).improved is False
    assert result(summary(100.0), fast, equivalent=True).improved is True


def test_without_timings_the_cost_decides_in_the_right_direction() -> None:
    assert result(summary(None, cost=100.0), summary(None, cost=50.0)).improved is True
    assert result(summary(None, cost=50.0), summary(None, cost=100.0)).improved is False
    assert result(summary(None, cost=50.0), summary(None, cost=50.0)).improved is False


def test_measure_runs_the_query_several_times_and_keeps_the_fastest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timings = iter([9.0, 3.0, 7.0, 1.0, 1.0])
    calls: list[str] = []

    def fake(conn: object, sql: str, analyze: bool = True, timing: bool = True) -> Any:
        assert timing is False
        calls.append(sql)
        return plan(next(timings))

    monkeypatch.setattr(verify_module, "run_explain", fake)
    best = measure(object(), "SELECT 1")  # pyright: ignore[reportArgumentType]
    assert MEASURE_RUNS == 5
    assert len(calls) == MEASURE_RUNS
    assert best.result.execution_time == 1.0
    assert best.noise == 0.0


def test_interleaved_measurement_alternates_between_the_two_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    def fake(conn: object, sql: str, analyze: bool = True, timing: bool = True) -> Any:
        order.append(sql)
        return plan(5.0 if sql == "A" else 2.0)

    monkeypatch.setattr(verify_module, "run_explain", fake)
    first, second = measure_interleaved(object(), "A", "B")  # pyright: ignore[reportArgumentType]
    assert order == ["A", "B"] * MEASURE_RUNS
    assert (first.result.execution_time, second.result.execution_time) == (5.0, 2.0)


@pytest.mark.parametrize(
    ("sql", "unprovable"),
    [
        ("SELECT id FROM t ORDER BY id LIMIT 5", False),
        ("SELECT id FROM t LIMIT 5", True),
        ("SELECT row_number() OVER (ORDER BY id) FROM t LIMIT 5", True),
        ("SELECT * FROM (SELECT id FROM t LIMIT 5) s", True),
        ("SELECT * FROM (SELECT id FROM t ORDER BY id LIMIT 5) s", False),
        ("SELECT id FROM t", False),
        ("SELECT random() FROM t", True),
        ("SELECT id FROM t OFFSET 10", True),
    ],
)
def test_which_queries_cannot_be_compared(sql: str, unprovable: bool) -> None:
    assert (unprovable_reason(sql) is not None) is unprovable


def test_an_index_on_a_partitioned_parent_is_recognised_as_used(
    pg_dsn: str, partitioned: None
) -> None:
    result = verify_index(pg_dsn, PARTITIONED_QUERY, IndexSuggestion(table="pt", columns=["k"]))
    assert result.after.uses_new_index
    assert result.improved


def test_queries_with_placeholders_are_explained_clearly(pg_dsn: str) -> None:
    with pytest.raises(ValueError, match="placeholders"):
        get_plan(pg_dsn, "SELECT * FROM orders WHERE id = $1")
    with pytest.raises(ValueError, match="placeholders"):
        verify_rewrite(pg_dsn, "SELECT $1", "SELECT $1 + 0")


def test_every_proof_transaction_gets_timeouts(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = Spy(monkeypatch, verify_module)
    verify_index(pg_dsn, QUERY, IndexSuggestion(table="orders", columns=["customer_id"]))
    assert spy.calls >= 2
    spy.calls = 0
    verify_statistics(pg_dsn, QUERY, StatsSuggestion(table="orders", columns=["customer_id", "id"]))
    assert spy.calls >= 2
    spy.calls = 0
    verify_rewrite(pg_dsn, QUERY, QUERY + " AND id > 0")
    assert spy.calls >= 1


def test_the_timeouts_are_restored_before_every_measured_statement(pg_dsn: str) -> None:
    with psycopg.connect(pg_dsn) as conn:
        conn.execute("SET LOCAL statement_timeout = 0")
        conn.execute("SET LOCAL lock_timeout = 0")
        verify_module.run_explain(conn, "SELECT 1 AS a")
        timeout = conn.execute("SHOW statement_timeout").fetchone()
        lock = conn.execute("SHOW lock_timeout").fetchone()
        conn.rollback()
    assert timeout is not None and timeout[0] == "1min"
    assert lock is not None and lock[0] == "5s"


def test_an_identical_rewrite_is_not_a_proof(pg_dsn: str) -> None:
    result = verify_rewrite(pg_dsn, QUERY, "  SELECT *   FROM orders\nWHERE customer_id = 7 ; ")
    assert result.error is not None and "identical" in result.error
    assert not result.improved


@pytest.mark.parametrize("columns", [[], ["only_one"]])
def test_statistics_need_at_least_two_columns(columns: list[str]) -> None:
    with pytest.raises(ValueError):
        StatsSuggestion(table="t", columns=columns)


def test_statistics_need_at_least_one_kind() -> None:
    with pytest.raises(ValueError):
        StatsSuggestion(table="t", columns=["a", "b"], kinds=[])


def test_verified_index_improves_the_plan(pg_dsn: str) -> None:
    result = verify_index(pg_dsn, QUERY, IndexSuggestion(table="orders", columns=["customer_id"]))
    assert result.before.uses_new_index is False
    assert result.after.uses_new_index is True
    assert result.after.total_cost < result.before.total_cost
    assert result.improved


def test_verify_leaves_no_index_behind(pg_dsn: str, pg_conn: psycopg.Connection) -> None:
    verify_index(pg_dsn, QUERY, IndexSuggestion(table="orders", columns=["customer_id"]))
    row = pg_conn.execute(
        "SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'pgsage_verify_idx%'"
    ).fetchone()
    assert row == (0,)


def test_useless_index_is_not_reported_as_improvement(pg_dsn: str) -> None:
    result = verify_index(
        pg_dsn, "SELECT * FROM orders", IndexSuggestion(table="orders", columns=["customer_id"])
    )
    assert result.after.uses_new_index is False
    assert not result.improved


def test_equivalent_rewrite_is_measured(pg_dsn: str, pg_conn: psycopg.Connection) -> None:
    pg_conn.execute("DROP TABLE IF EXISTS rw")
    pg_conn.execute(
        "CREATE TABLE rw AS SELECT g AS id, timestamp '2024-01-01' + g * interval '1 minute' AS ts "
        "FROM generate_series(1, 300000) g"
    )
    try:
        pg_conn.execute("CREATE INDEX ON rw (ts)")
        pg_conn.execute("ANALYZE rw")
        result = verify_rewrite(
            pg_dsn,
            "SELECT * FROM rw WHERE ts::date = date '2024-03-01'",
            "SELECT * FROM rw WHERE ts >= date '2024-03-01' AND ts < date '2024-03-02'",
        )
    finally:
        pg_conn.execute("DROP TABLE IF EXISTS rw")
    assert result.kind == "rewrite"
    assert result.equivalent is True
    assert result.improved


def test_non_equivalent_rewrite_is_never_proven(pg_dsn: str) -> None:
    result = verify_rewrite(
        pg_dsn,
        "SELECT * FROM orders WHERE customer_id = 7",
        "SELECT * FROM orders WHERE customer_id = 7 AND id < 10",
    )
    assert result.equivalent is False
    assert not result.improved


def test_rewrite_rejects_write_statements(pg_dsn: str) -> None:
    with pytest.raises(UnsafeStatementError):
        verify_rewrite(pg_dsn, "SELECT 1", "DELETE FROM orders")


def test_statistics_verification_runs_and_rolls_back(
    pg_dsn: str, pg_conn: psycopg.Connection
) -> None:
    result = verify_statistics(
        pg_dsn,
        "SELECT * FROM orders WHERE customer_id = 7",
        StatsSuggestion(table="orders", columns=["customer_id", "id"]),
    )
    row = pg_conn.execute(
        "SELECT count(*) FROM pg_statistic_ext WHERE stxname = 'pgsage_verify_stats'"
    ).fetchone()
    assert result.kind == "statistics"
    assert row == (0,)
    assert result.error is None
    assert result.before.execution_ms is not None and result.after.execution_ms is not None


def test_index_ddl_and_names() -> None:
    suggestion = IndexSuggestion(table="orders", columns=["customer_id", "status"], schema_name="s")
    assert suggestion.ddl == 'CREATE INDEX ON "s"."orders" ("customer_id", "status")'
    assert suggestion.suggested_name == "idx_orders_customer_id_status"
    default = StatsSuggestion(table="orders", columns=["a", "b"])
    mcv = StatsSuggestion(table="orders", columns=["a", "b"], kinds=["mcv"])
    assert default.suggested_name == "stats_orders_a_b"
    assert mcv.suggested_name != default.suggested_name
    assert mcv.ddl.endswith('(mcv) ON "a", "b" FROM "orders"')


def test_partial_index_is_proven_when_the_query_matches_its_predicate(pg_dsn: str) -> None:
    result = verify_index(
        pg_dsn,
        "SELECT id FROM orders WHERE customer_id = 7",
        IndexSuggestion(table="orders", columns=["id"], where="customer_id = 7"),
    )
    assert result.statement.endswith("WHERE customer_id = 7")
    assert result.after.uses_new_index
    assert result.improved


def test_covering_index_enables_an_index_only_scan(
    pg_dsn: str, pg_conn: psycopg.Connection
) -> None:
    pg_conn.execute("VACUUM orders")
    result = verify_index(
        pg_dsn,
        "SELECT customer_id, created_at FROM orders WHERE customer_id = 7",
        IndexSuggestion(table="orders", columns=["customer_id"], include=["created_at"]),
    )
    assert result.after.uses_new_index
    assert "Index Only Scan" in result.after.node_types


def test_index_ddl_forms() -> None:
    assert IndexSuggestion(table="t", columns=["a"]).ddl == 'CREATE INDEX ON "t" ("a")'
    assert (
        IndexSuggestion(table="t", columns=["a", "b"], include=["c"], where="d > 0").ddl
        == 'CREATE INDEX ON "t" ("a", "b") INCLUDE ("c") WHERE d > 0'
    )
    assert (
        IndexSuggestion(table="t", columns=["j"], method="gin").create_ddl("n", if_not_exists=True)
        == 'CREATE INDEX IF NOT EXISTS "n" ON "t" USING gin ("j")'
    )


def test_index_names_are_distinct_per_variant() -> None:
    plain = IndexSuggestion(table="t", columns=["a"])
    partial = IndexSuggestion(table="t", columns=["a"], where="b = 1")
    gin = IndexSuggestion(table="t", columns=["a"], method="gin")
    names = {plain.suggested_name, partial.suggested_name, gin.suggested_name}
    assert len(names) == 3
    assert plain.suggested_name == "idx_t_a"


@pytest.mark.parametrize(
    "bad",
    [
        {"include": ["x"], "method": "gin"},
        {"where": "a = 1; DROP TABLE t"},
        {"where": "a IN (SELECT 1)"},
        {"where": "a = 1 -- c"},
        {"where": "pg_sleep(1) IS NULL"},
        {"where": "x" * 501},
        {"method": "gist"},
    ],
)
def test_unsafe_or_unsupported_index_options_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        IndexSuggestion.model_validate({"table": "t", "columns": ["a"], **bad})


def timed(ms: float, nodes: list[str], uses_index: bool = False) -> PlanSummary:
    return PlanSummary(
        total_cost=100.0, execution_ms=ms, node_types=nodes, uses_new_index=uses_index
    )


def proven_index_result(suggestion: IndexSuggestion) -> VerifyResult:
    return VerifyResult(
        kind="index",
        statement=suggestion.ddl,
        before=timed(14.0, ["Seq Scan"]),
        after=timed(1.0, ["Index Scan"], True),
        apply=index_apply_sql(suggestion),
    )


def test_apply_sql_is_idempotent_and_builds_indexes_concurrently() -> None:
    suggestion = IndexSuggestion(table="orders", columns=["customer_id"])
    assert index_apply_sql(suggestion) == [
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS "idx_orders_customer_id" '
        'ON "orders" ("customer_id");'
    ]


def test_apply_sql_can_avoid_concurrently() -> None:
    suggestion = IndexSuggestion(table="orders", columns=["customer_id"])
    (statement,) = index_apply_sql(suggestion, concurrently=False)
    assert "CONCURRENTLY" not in statement
    assert statement.startswith('CREATE INDEX IF NOT EXISTS "idx_orders_customer_id"')


def test_statistics_apply_sql_is_idempotent_and_followed_by_analyze() -> None:
    suggestion = StatsSuggestion(table="orders", columns=["a", "b"])
    statements = statistics_apply_sql(suggestion)
    assert statements[0].startswith('CREATE STATISTICS IF NOT EXISTS "stats_orders_a_b"')
    assert statements[1] == 'ANALYZE "orders";'


def test_partial_and_covering_indexes_keep_their_shape_in_the_apply_sql() -> None:
    suggestion = IndexSuggestion(
        table="orders", columns=["customer_id"], where="status = 'open'", include=["created_at"]
    )
    (statement,) = index_apply_sql(suggestion)
    assert statement.endswith("""INCLUDE ("created_at") WHERE status = 'open';""")


def test_identifier_quotes_are_escaped_in_the_apply_sql() -> None:
    suggestion = IndexSuggestion(table="t", columns=['x"); DROP TABLE v; --'])
    (statement,) = index_apply_sql(suggestion)
    assert '"x""); DROP TABLE v; --"' in statement


def test_as_dict_offers_the_apply_sql_only_for_a_proven_change() -> None:
    suggestion = IndexSuggestion(table="orders", columns=["customer_id"])
    proven = proven_index_result(suggestion)
    assert proven.as_dict()["improved"] is True
    assert proven.as_dict()["speedup"] == 14.0
    assert proven.as_dict()["apply"] == index_apply_sql(suggestion)
    unproven = proven.model_copy(update={"after": timed(14.0, ["Seq Scan"])})
    assert unproven.as_dict()["improved"] is False
    assert unproven.as_dict()["apply"] == []


def test_a_failed_result_has_no_speedup_and_nothing_to_apply() -> None:
    failed = VerifyResult.failed("index", "CREATE INDEX ON t (a)", "boom")
    assert failed.as_dict()["speedup"] is None
    assert failed.as_dict()["apply"] == []


def test_verify_index_returns_the_apply_sql(pg_dsn: str) -> None:
    suggestion = IndexSuggestion(table="orders", columns=["customer_id"])
    result = verify_index(pg_dsn, "SELECT * FROM orders WHERE customer_id = 7", suggestion)
    assert result.improved
    assert result.as_dict()["apply"] == index_apply_sql(suggestion)


def test_verify_statistics_returns_the_apply_sql(pg_dsn: str, eq: None) -> None:
    suggestion = StatsSuggestion(table="eq", columns=["id", "grp"])
    result = verify_statistics(pg_dsn, "SELECT * FROM eq WHERE id = 1 AND grp = 1", suggestion)
    assert result.apply == statistics_apply_sql(suggestion)


def test_a_rewrite_has_nothing_to_apply(pg_dsn: str) -> None:
    result = verify_rewrite(
        pg_dsn,
        "SELECT * FROM orders WHERE customer_id = 7",
        "SELECT * FROM orders WHERE customer_id = 7 AND true",
    )
    assert result.apply == []


def plan_shape(nodes: list[str], ms: float) -> Any:
    return verify_module.PlanSummary(total_cost=1.0, execution_ms=ms, node_types=nodes)


def test_a_statistics_proof_needs_a_changed_plan() -> None:
    same = verify_module.VerifyResult(
        kind="statistics",
        statement="CREATE STATISTICS s ON a, b FROM t",
        before=plan_shape(["Seq Scan"], 10.0),
        after=plan_shape(["Seq Scan"], 5.0),
    )
    changed = same.model_copy(update={"after": plan_shape(["Index Scan"], 5.0)})
    assert not same.improved
    assert changed.improved


def test_index_names_stay_within_the_identifier_limit_for_multibyte_names() -> None:
    suggestion = IndexSuggestion(table="é" * 40, columns=["ü" * 40, "plain"])
    assert len(suggestion.suggested_name.encode()) <= 63


def test_an_index_on_a_partitioned_table_is_applied_without_concurrently(
    pg_dsn: str, pg_conn: psycopg.Connection
) -> None:
    pg_conn.execute("CREATE TABLE parted (id int, d date) PARTITION BY RANGE (d)")
    pg_conn.execute(
        "CREATE TABLE parted_1 PARTITION OF parted FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')"
    )
    try:
        result = verify_index(
            pg_dsn,
            "SELECT * FROM parted WHERE id = 5",
            IndexSuggestion(table="parted", columns=["id"]),
        )
    finally:
        pg_conn.execute("DROP TABLE parted")
    assert result.apply == ['CREATE INDEX IF NOT EXISTS "idx_parted_id" ON "parted" ("id");']
    assert result.note is not None and "partitioned" in result.note


def test_a_trailing_line_comment_does_not_break_a_rewrite_proof(pg_dsn: str) -> None:
    result = verify_rewrite(
        pg_dsn,
        "SELECT id FROM orders WHERE id < 5 ORDER BY id -- newest first",
        "SELECT id FROM orders WHERE id < 5 ORDER BY id, customer_id",
    )
    assert result.error is None
    assert result.equivalent is True


def test_a_statement_timeout_is_an_unproven_result_not_a_crash(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cancelled(*args: object, **kwargs: object) -> None:
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(verify_module, "_prove_rewrite", cancelled)
    result = verify_rewrite(pg_dsn, "SELECT 1 AS a", "SELECT 2 AS a")
    assert not result.improved
    assert result.error is not None and "timed out" in result.error


def test_a_setting_suggestion_accepts_only_planner_memory_jit_and_parallel_settings() -> None:
    suggestion = verify_module.SettingSuggestion(settings={"work_mem": "64MB", "jit": "off"})
    assert suggestion.apply_sql == ["SET work_mem = '64MB';", "SET jit = 'off';"]
    assert suggestion.statement == "SET work_mem = '64MB', jit = 'off'"
    for bad in ("statement_timeout", "search_path", "session_replication_role", "enable_x; DROP"):
        with pytest.raises(ValueError, match="cannot be proven"):
            verify_module.SettingSuggestion(settings={bad: "1"})
    for value in ("64MB'; DROP TABLE t; --", "", "a b", "x" * 40):
        with pytest.raises(ValueError, match="plain word or number"):
            verify_module.SettingSuggestion(settings={"work_mem": value})
    with pytest.raises(ValueError):
        verify_module.SettingSuggestion(settings={})


def test_a_setting_proof_measures_with_the_value_applied_and_leaves_nothing_behind(
    pg_dsn: str, pg_conn: psycopg.Connection
) -> None:
    result = verify_module.verify_setting(
        pg_dsn,
        "SELECT g FROM generate_series(1, 300000) g ORDER BY g DESC",
        verify_module.SettingSuggestion(settings={"work_mem": "512MB"}),
    )
    assert result.error is None
    assert result.kind == "setting"
    assert result.apply == ["SET work_mem = '512MB';"]
    assert result.note is not None and "this session only" in result.note
    row = pg_conn.execute("SHOW work_mem").fetchone()
    assert row is not None and row[0] != "512MB"


def test_a_setting_proof_with_an_unknown_value_is_an_unproven_result(pg_dsn: str) -> None:
    result = verify_module.verify_setting(
        pg_dsn, "SELECT 1 AS a", verify_module.SettingSuggestion(settings={"jit": "maybe"})
    )
    assert not result.improved and result.error


def test_every_unproven_verdict_carries_a_reason() -> None:
    def result(kind: verify_module.ChangeKind, before: Any, after: Any, **extra: Any) -> Any:
        return verify_module.VerifyResult(
            kind=kind, statement="s", before=before, after=after, **extra
        )

    slow = plan_shape(["Seq Scan"], 10.0)
    barely = plan_shape(["Seq Scan"], 9.8)
    unused = result("index", slow, barely)
    assert unused.reason == "the planner did not use the new index"
    same_plan = result("statistics", slow, barely)
    assert same_plan.reason == "the plan did not change"
    below = result("setting", slow, barely)
    assert below.reason is not None and "below the 1.10x and 0.5 ms floor" in below.reason
    differs = result("rewrite", slow, barely, equivalent=False, note="result rows differ")
    assert differs.reason == "result rows differ"
    assert result("rewrite", slow, plan_shape(["Index Scan"], 1.0), equivalent=True).reason is None
    assert below.as_dict()["reason"] == below.reason


def test_an_index_that_already_exists_is_reported_not_re_proven(
    pg_dsn: str, pg_conn: psycopg.Connection
) -> None:
    pg_conn.execute("CREATE INDEX covering_probe ON orders (customer_id, id)")
    try:
        prefix = verify_index(
            pg_dsn,
            "SELECT * FROM orders WHERE customer_id = 5",
            IndexSuggestion(table="orders", columns=["customer_id"]),
        )
        wider = verify_index(
            pg_dsn,
            "SELECT * FROM orders WHERE customer_id = 5",
            IndexSuggestion(table="orders", columns=["customer_id", "id", "created_at"]),
        )
    finally:
        pg_conn.execute("DROP INDEX covering_probe")
    assert prefix.error is not None and "covering_probe" in prefix.error
    assert not prefix.improved
    assert wider.error is None or "covering_probe" not in wider.error


def test_a_partial_index_is_never_mistaken_for_an_existing_equivalent(
    pg_dsn: str, pg_conn: psycopg.Connection
) -> None:
    pg_conn.execute("CREATE INDEX partial_probe ON orders (customer_id) WHERE id > 10")
    try:
        result = verify_index(
            pg_dsn,
            "SELECT * FROM orders WHERE customer_id = 5",
            IndexSuggestion(table="orders", columns=["customer_id"]),
        )
    finally:
        pg_conn.execute("DROP INDEX partial_probe")
    assert result.error is None


@pytest.mark.parametrize(
    "settings",
    [
        {"work_mem": "64MB\n"},
        {"work_mem": "9999GB"},
        {"work_mem": "5000000"},
        {"hash_mem_multiplier": "1000"},
        {"max_parallel_workers_per_gather": "500"},
        {"join_collapse_limit": "1000000"},
        {"work_mem\n": "1MB"},
    ],
)
def test_a_setting_proof_rejects_unbounded_or_sloppy_values(settings: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        verify_module.SettingSuggestion(settings=settings)


def test_a_setting_proof_accepts_sensible_values() -> None:
    suggestion = verify_module.SettingSuggestion(
        settings={
            "work_mem": "256MB",
            "hash_mem_multiplier": "4",
            "random_page_cost": "1.1",
            "jit": "off",
        }
    )
    assert len(suggestion.apply_sql) == 4


def test_an_ordered_rewrite_is_compared_per_row_so_the_result_never_needs_one_huge_string(
    pg_dsn: str,
) -> None:
    result = verify_rewrite(
        pg_dsn,
        "SELECT id FROM orders WHERE id < 2000 ORDER BY id",
        "SELECT id FROM orders WHERE id < 2000 ORDER BY id, customer_id",
    )
    assert result.error is None and result.equivalent is True


def noisy_shape(nodes: list[str], ms: float, noise: float) -> Any:
    return verify_module.PlanSummary(total_cost=1.0, execution_ms=ms, node_types=nodes, noise=noise)


def test_the_speedup_floor_rises_with_measured_noise() -> None:
    quiet = verify_module.VerifyResult(
        kind="rewrite",
        statement="s",
        before=noisy_shape(["Seq Scan"], 10.0, 0.01),
        after=noisy_shape(["Seq Scan"], 8.5, 0.01),
        equivalent=True,
    )
    busy = quiet.model_copy(
        update={
            "before": noisy_shape(["Seq Scan"], 10.0, 0.4),
            "after": noisy_shape(["Seq Scan"], 8.5, 0.4),
        }
    )
    assert quiet.required_speedup == 1.1 and quiet.improved
    assert busy.required_speedup == pytest.approx(2.2) and not busy.improved
    assert busy.reason is not None and "2.20x" in busy.reason


def test_a_large_effect_still_passes_on_a_noisy_host() -> None:
    result = verify_module.VerifyResult(
        kind="index",
        statement="s",
        before=noisy_shape(["Seq Scan"], 100.0, 0.4),
        after=verify_module.PlanSummary(
            total_cost=1.0,
            execution_ms=5.0,
            node_types=["Index Scan"],
            uses_new_index=True,
            noise=0.4,
        ),
    )
    assert result.improved


def test_a_narrow_pass_that_does_not_repeat_is_reported_as_noise() -> None:
    outcomes = iter([1.18, 1.0])

    def run() -> Any:
        ratio = next(outcomes)
        return verify_module.VerifyResult(
            kind="rewrite",
            statement="s",
            before=plan_shape(["Seq Scan"], 10.0 * ratio),
            after=plan_shape(["Seq Scan"], 10.0),
            equivalent=True,
        )

    result = verify_module._confirmed(run)  # pyright: ignore[reportPrivateUsage]
    assert result.noisy and not result.improved
    assert result.note is not None and "treat this as noise" in result.note
    assert result.reason == result.note


def test_a_clear_pass_is_not_re_run() -> None:
    calls = 0

    def run() -> Any:
        nonlocal calls
        calls += 1
        return verify_module.VerifyResult(
            kind="rewrite",
            statement="s",
            before=plan_shape(["Seq Scan"], 50.0),
            after=plan_shape(["Index Scan"], 5.0),
            equivalent=True,
        )

    assert verify_module._confirmed(run).improved  # pyright: ignore[reportPrivateUsage]
    assert calls == 1


def test_measured_runs_leave_per_node_timing_off(pg_dsn: str) -> None:
    with psycopg.connect(pg_dsn) as conn:
        result = verify_module.measure(conn, "SELECT count(*) FROM orders").result
        conn.rollback()
    assert result.execution_time is not None
    assert all(node.actual_total_time is None for node in result.nodes())


def test_a_proof_reports_planning_time_and_noise(pg_dsn: str) -> None:
    result = verify_rewrite(pg_dsn, QUERY, QUERY + " AND id > 0")
    shown = result.as_dict()
    assert shown["before"]["planning_ms"] is not None
    assert shown["after"]["noise"] is not None


def test_tablesample_is_never_provable() -> None:
    reason = verify_module.unprovable_reason("SELECT * FROM orders TABLESAMPLE SYSTEM (10)")
    assert reason is not None and "TABLESAMPLE" in reason


def test_a_setting_proof_alternates_between_the_default_and_the_new_value(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = verify_module.run_explain

    def spy(conn: Any, sql: str, analyze: bool = True, timing: bool = True) -> Any:
        row = conn.execute("SHOW work_mem").fetchone()
        seen.append(row[0])
        return real(conn, sql, analyze, timing)

    monkeypatch.setattr(verify_module, "run_explain", spy)
    verify_module.verify_setting(
        pg_dsn, QUERY, verify_module.SettingSuggestion(settings={"work_mem": "96MB"})
    )
    assert seen[:4] == ["4MB", "96MB", "4MB", "96MB"]


def test_statistics_with_an_unstable_control_are_reported_as_sampling_noise(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shapes = iter([["Seq Scan"], ["Index Scan"], ["Index Scan"]] * 2)

    def fake(conn: Any, sql: str, runs: int = 5) -> Any:
        return verify_module.Measurement(
            parse_explain(
                [
                    {
                        "Plan": {
                            "Node Type": next(shapes)[0],
                            "Total Cost": 1,
                            "Plan Rows": 1,
                        },
                        "Execution Time": 10.0,
                    }
                ]
            ),
            0.0,
        )

    monkeypatch.setattr(verify_module, "measure", fake)
    result = verify_module.verify_statistics(
        pg_dsn, QUERY, StatsSuggestion(table="orders", columns=["customer_id", "id"])
    )
    assert result.noisy and not result.improved
    assert result.note is not None and "sampling noise" in result.note
