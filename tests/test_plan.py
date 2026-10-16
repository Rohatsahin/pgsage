import json

import psycopg
import pytest

from pgsage import parse_explain, plan_digest
from tests.conftest import explain_json

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


JOIN_PLAN = [
    {
        "Plan": {
            "Node Type": "Nested Loop",
            "Startup Cost": 0.0,
            "Total Cost": 100.0,
            "Plan Rows": 10,
            "Actual Rows": 5000,
            "Actual Loops": 1,
            "Actual Startup Time": 0.1,
            "Actual Total Time": 50.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "a",
                    "Startup Cost": 0.0,
                    "Total Cost": 10.0,
                    "Plan Rows": 10,
                    "Actual Rows": 10,
                    "Actual Loops": 1,
                    "Actual Total Time": 1.0,
                },
                {
                    "Node Type": "Index Scan",
                    "Relation Name": "b",
                    "Startup Cost": 0.0,
                    "Total Cost": 5.0,
                    "Plan Rows": 1,
                    "Actual Rows": 500,
                    "Actual Loops": 10,
                    "Actual Total Time": 4.0,
                },
            ],
        },
        "Planning Time": 0.2,
        "Execution Time": 51.0,
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


def test_parses_list_string_and_dict_inputs() -> None:
    as_string = parse_explain(json.dumps(JOIN_PLAN))
    as_list = parse_explain(JOIN_PLAN)
    as_dict = parse_explain(JOIN_PLAN[0])
    assert as_string.plan.node_type == as_list.plan.node_type == as_dict.plan.node_type


def test_walk_visits_every_node_depth_first() -> None:
    result = parse_explain(JOIN_PLAN)
    assert [n.node_type for n in result.nodes()] == ["Nested Loop", "Seq Scan", "Index Scan"]


def test_loops_multiply_rows_and_time() -> None:
    inner = parse_explain(JOIN_PLAN).nodes()[2]
    assert inner.total_actual_rows == 5000
    assert inner.total_actual_time == 40.0


def test_self_time_excludes_children() -> None:
    root = parse_explain(JOIN_PLAN).plan
    assert root.self_time == pytest.approx(50.0 - 1.0 - 40.0)


def test_row_estimate_ratio_flags_underestimate() -> None:
    root = parse_explain(JOIN_PLAN).plan
    assert root.row_estimate_ratio == 500


def test_plain_explain_has_no_actuals() -> None:
    plan = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Startup Cost": 0.0,
                "Total Cost": 1.0,
                "Plan Rows": 1,
            }
        }
    ]
    result = parse_explain(plan)
    assert not result.analyzed
    assert result.plan.self_time is None
    assert result.plan.row_estimate_ratio is None


@pytest.mark.parametrize("bad", ["[]", "[{}]", '{"foo": 1}', "[1]"])
def test_rejects_non_explain_input(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_explain(bad)


def test_parses_real_analyze_output(pg_conn: psycopg.Connection) -> None:
    raw = explain_json(pg_conn, "SELECT * FROM orders WHERE customer_id = 7")
    result = parse_explain(raw)
    scan = result.plan
    assert result.analyzed
    assert scan.node_type == "Seq Scan"
    assert scan.relation_name == "orders"
    assert scan.total_actual_rows == 2000
    assert scan.rows_removed_by_filter == 198000
    assert scan.self_time is not None


def test_parses_real_join_with_children(pg_conn: psycopg.Connection) -> None:
    raw = explain_json(
        pg_conn,
        "SELECT * FROM orders a JOIN orders b ON a.id = b.id WHERE a.customer_id = 1",
    )
    nodes = parse_explain(raw).nodes()
    assert len(nodes) >= 3
    assert all(n.actual_rows is not None for n in nodes)


def test_parses_plain_explain(pg_conn: psycopg.Connection) -> None:
    raw = explain_json(pg_conn, "SELECT * FROM orders", analyze=False)
    assert not parse_explain(raw).analyzed


def test_a_plan_pasted_from_psql_with_its_header_still_parses() -> None:
    pasted = (
        "                 QUERY PLAN\n"
        "------------------------------------------\n"
        " [                                        +\n"
        "   {                                      +\n"
        '     "Plan": {"Node Type": "Result", "Total Cost": 0.01, "Plan Rows": 1}+\n'
        "   }                                      +\n"
        " ]\n"
        "(1 row)\n"
    ).replace("+\n", "\n")
    assert parse_explain(pasted).plan.node_type == "Result"


def test_the_digest_marks_subplan_nodes() -> None:
    plan = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Total Cost": 1.0,
                "Plan Rows": 1,
                "Plans": [
                    {
                        "Node Type": "Index Scan",
                        "Parent Relationship": "SubPlan",
                        "Subplan Name": "SubPlan 1",
                        "Total Cost": 1.0,
                        "Plan Rows": 1,
                    }
                ],
            }
        }
    ]
    rows = plan_digest(parse_explain(plan))
    assert "subplan" not in rows[0]
    assert rows[1]["subplan"] == "SubPlan 1"
