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
