import pytest

from pgsage import parse_explain, plan_digest

PLAN = [
    {
        "Plan": {
            "Node Type": "Gather",
            "Startup Cost": 0.0,
            "Total Cost": 10.0,
            "Plan Rows": 100,
            "Actual Rows": 100,
            "Actual Loops": 1,
            "Actual Total Time": 10.0,
            "Plans": [
                {
                    "Node Type": "Parallel Seq Scan",
                    "Relation Name": "t",
                    "Startup Cost": 0.0,
                    "Total Cost": 9.0,
                    "Plan Rows": 50,
                    "Actual Rows": 50,
                    "Actual Loops": 3,
                    "Actual Total Time": 9.0,
                }
            ],
        },
        "Execution Time": 10.0,
    }
]


def test_workers_do_not_multiply_wall_time() -> None:
    result = parse_explain(PLAN)
    scan = result.nodes()[1]
    assert scan.total_actual_time == 9.0
    assert scan.total_actual_rows == 150
    assert result.execution_time is not None
    self_times: list[float] = [d["self_ms"] or 0.0 for d in plan_digest(result)]
    assert all(ms <= result.execution_time for ms in self_times)
    assert result.plan.self_time == 1.0


def test_in_parallel_flag_is_not_serialized_into_prompts() -> None:
    dumped = parse_explain(PLAN).model_dump(by_alias=True, mode="json", exclude_none=True)
    assert "in_parallel" not in str(dumped)


def nested_parallel_plan(launched: int | None = 2) -> list[dict[str, object]]:
    gather: dict[str, object] = {
        "Node Type": "Gather",
        "Startup Cost": 0.0,
        "Total Cost": 10.0,
        "Plan Rows": 1,
        "Actual Rows": 1,
        "Actual Loops": 1,
        "Actual Total Time": 100.0,
        "Plans": [
            {
                "Node Type": "Nested Loop",
                "Startup Cost": 0.0,
                "Total Cost": 9.0,
                "Plan Rows": 1,
                "Actual Rows": 1,
                "Actual Loops": 3,
                "Actual Total Time": 98.0,
                "Plans": [
                    {
                        "Node Type": "Parallel Seq Scan",
                        "Relation Name": "a",
                        "Startup Cost": 0.0,
                        "Total Cost": 1.0,
                        "Plan Rows": 1,
                        "Actual Rows": 1,
                        "Actual Loops": 3,
                        "Actual Total Time": 5.0,
                        "Rows Removed by Filter": 1000,
                    },
                    {
                        "Node Type": "Index Only Scan",
                        "Relation Name": "b",
                        "Index Name": "b_idx",
                        "Startup Cost": 0.0,
                        "Total Cost": 1.0,
                        "Plan Rows": 1,
                        "Actual Rows": 1,
                        "Actual Loops": 90000,
                        "Actual Total Time": 0.001,
                    },
                ],
            }
        ],
    }
    if launched is not None:
        gather["Workers Launched"] = launched
    return [{"Plan": gather, "Execution Time": 100.0}]


def test_inner_loops_under_a_gather_are_not_dropped() -> None:
    result = parse_explain(nested_parallel_plan(launched=2))
    scan, inner = result.nodes()[2], result.nodes()[3]
    assert scan.total_actual_time == pytest.approx(5.0)
    assert inner.total_actual_time == pytest.approx(0.001 * 90000 / 3)


def test_the_participant_count_comes_from_workers_launched() -> None:
    two = parse_explain(nested_parallel_plan(launched=2)).nodes()[3]
    four = parse_explain(nested_parallel_plan(launched=4)).nodes()[3]
    assert two.total_actual_time == pytest.approx(30.0)
    assert four.total_actual_time == pytest.approx(18.0)


def test_without_workers_launched_the_scan_loop_count_is_used() -> None:
    inner = parse_explain(nested_parallel_plan(launched=None)).nodes()[3]
    assert inner.total_actual_time == pytest.approx(30.0)


def test_the_digest_reports_filter_removals_as_totals_across_loops() -> None:
    digest = plan_digest(parse_explain(nested_parallel_plan()))
    scan = next(row for row in digest if row["node"] == "Parallel Seq Scan")
    assert scan["rows_removed_by_filter"] == 3000
    assert scan["actual_rows_total"] == 3


def test_a_node_outside_a_gather_is_unaffected() -> None:
    plain = parse_explain(
        [
            {
                "Plan": {
                    "Node Type": "Index Scan",
                    "Startup Cost": 0,
                    "Total Cost": 1,
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Actual Loops": 1000,
                    "Actual Total Time": 0.01,
                },
                "Execution Time": 10.0,
            }
        ]
    )
    assert plain.plan.total_actual_time == pytest.approx(10.0)
