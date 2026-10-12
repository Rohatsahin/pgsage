from typing import Any

import pytest

from pgsage import parse_explain
from pgsage import verify as verify_module
from pgsage.verify import (
    MEASURE_RUNS,
    MIN_ABSOLUTE_GAIN_MS,
    MIN_SPEEDUP,
    PlanSummary,
    VerifyResult,
    measure,
    measure_interleaved,
    top_level,
    unprovable_reason,
)


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


def test_measure_runs_the_query_several_times_and_keeps_the_fastest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timings = iter([9.0, 3.0, 7.0, 1.0, 1.0])
    calls: list[str] = []

    def fake(conn: object, sql: str) -> Any:
        calls.append(sql)
        return plan(next(timings))

    monkeypatch.setattr(verify_module, "run_explain", fake)
    best = measure(object(), "SELECT 1")  # pyright: ignore[reportArgumentType]
    assert MEASURE_RUNS == 3
    assert len(calls) == MEASURE_RUNS
    assert best.execution_time == 1.0 or best.execution_time == 3.0
    assert best.execution_time == min(9.0, 3.0, 7.0)


def test_interleaved_measurement_alternates_between_the_two_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    def fake(conn: object, sql: str) -> Any:
        order.append(sql)
        return plan(5.0 if sql == "A" else 2.0)

    monkeypatch.setattr(verify_module, "run_explain", fake)
    first, second = measure_interleaved(object(), "A", "B")  # pyright: ignore[reportArgumentType]
    assert order == ["A", "B"] * MEASURE_RUNS
    assert (first.execution_time, second.execution_time) == (5.0, 2.0)


def testtop_level_ignores_everything_inside_parentheses() -> None:
    assert top_level("SELECT a FROM (SELECT b ORDER BY c) x") == "SELECT a FROM  x"
    assert (
        top_level("SELECT row_number() OVER (ORDER BY id) FROM t")
        == "SELECT row_number OVER  FROM t"
    )


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
