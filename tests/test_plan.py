import json

import pytest

from pgsage import parse_explain

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
