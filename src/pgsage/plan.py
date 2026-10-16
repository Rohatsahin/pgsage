from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MAX_PLAN_DEPTH = 250


class PlanNode(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    node_type: str = Field(alias="Node Type")
    relation_name: str | None = Field(default=None, alias="Relation Name")
    schema_name: str | None = Field(default=None, alias="Schema")
    index_name: str | None = Field(default=None, alias="Index Name")
    total_cost: float = Field(alias="Total Cost")
    plan_rows: int = Field(alias="Plan Rows")
    actual_rows: float | None = Field(default=None, alias="Actual Rows")
    actual_loops: int | None = Field(default=None, alias="Actual Loops")
    actual_total_time: float | None = Field(default=None, alias="Actual Total Time")
    rows_removed_by_filter: float | None = Field(default=None, alias="Rows Removed by Filter")
    workers_launched: int | None = Field(default=None, alias="Workers Launched")
    subplan_name: str | None = Field(default=None, alias="Subplan Name")
    children: list[PlanNode] = Field(default_factory=lambda: list[PlanNode](), alias="Plans")
    in_parallel: bool = Field(default=False, exclude=True)
    parallel_participants: int = Field(default=1, exclude=True)

    @property
    def loops(self) -> int:
        return self.actual_loops or 1

    @property
    def total_actual_rows(self) -> float | None:
        if self.actual_rows is None:
            return None
        return self.actual_rows * self.loops

    @property
    def total_actual_time(self) -> float | None:
        if self.actual_total_time is None:
            return None
        divisor = self.parallel_participants if self.in_parallel else 1
        return self.actual_total_time * self.loops / divisor

    @property
    def self_time(self) -> float | None:
        total = self.total_actual_time
        if total is None:
            return None
        children = sum(c.total_actual_time or 0.0 for c in self.children)
        return max(total - children, 0.0)

    @property
    def row_estimate_ratio(self) -> float | None:
        if self.actual_rows is None:
            return None
        planned = max(self.plan_rows, 1)
        actual = max(self.actual_rows, 1)
        return actual / planned

    def walk(self) -> Iterator[PlanNode]:
        yield self
        for child in self.children:
            yield from child.walk()


class ExplainResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    plan: PlanNode = Field(alias="Plan")
    planning_time: float | None = Field(default=None, alias="Planning Time")
    execution_time: float | None = Field(default=None, alias="Execution Time")

    @property
    def analyzed(self) -> bool:
        return self.execution_time is not None

    def nodes(self) -> list[PlanNode]:
        return list(self.plan.walk())

    @model_validator(mode="after")
    def _mark_parallel_workers(self) -> ExplainResult:
        for node in self.plan.walk():
            if node.node_type not in ("Gather", "Gather Merge"):
                continue
            participants = (
                node.workers_launched + 1
                if node.workers_launched is not None
                else max((child.loops for child in node.children), default=1)
            )
            for child in node.children:
                for below in child.walk():
                    below.in_parallel = True
                    below.parallel_participants = max(participants, 1)
        return self


def _load_json(raw: str) -> object:
    try:
        return json.loads(raw)
    except RecursionError as error:
        raise ValueError("the plan nests too deeply to parse") from error
    except ValueError:
        opening = min((i for i in (raw.find("["), raw.find("{")) if i != -1), default=-1)
        closing = max(raw.rfind("]"), raw.rfind("}"))
        if opening == -1 or closing <= opening:
            raise
        return json.loads(raw[opening : closing + 1])


def parse_explain(raw: str | list[Any] | dict[str, Any]) -> ExplainResult:
    try:
        data: object = _load_json(raw) if isinstance(raw, str) else raw
    except ValueError as error:
        raise ValueError(
            "the plan is not JSON; use EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)"
        ) from error
    if isinstance(data, list):
        items = cast("list[object]", data)
        if len(items) != 1:
            raise ValueError(f"expected exactly one EXPLAIN result, got {len(items)}")
        data = items[0]
    if not isinstance(data, dict) or "Plan" not in data:
        raise ValueError("input is not EXPLAIN (FORMAT JSON) output: missing 'Plan'")
    try:
        return ExplainResult.model_validate(data)
    except ValidationError as error:
        first = error.errors()[0]
        if first["type"] == "recursion_loop":
            raise ValueError(
                f"the plan nests more than {MAX_PLAN_DEPTH} levels deep and cannot be parsed"
            ) from error
        location = ".".join(str(part) for part in first["loc"])
        raise ValueError(f"invalid EXPLAIN JSON at {location[:120]}: {first['msg']}") from error


def plan_digest(result: ExplainResult) -> list[dict[str, Any]]:
    digest: list[dict[str, Any]] = []

    def visit(node: PlanNode, depth: int) -> None:
        ratio = node.row_estimate_ratio
        row: dict[str, Any] = {
            "depth": depth,
            "node": node.node_type,
            "relation": node.relation_name,
            "index": node.index_name,
            "planned_rows": node.plan_rows,
            "actual_rows_total": node.total_actual_rows,
            "loops": node.loops,
            "self_ms": None if node.self_time is None else round(node.self_time, 3),
            "rows_removed_by_filter": (
                None
                if node.rows_removed_by_filter is None
                else node.rows_removed_by_filter * node.loops
            ),
            "estimate_off_by": None if ratio is None else round(max(ratio, 1 / ratio), 1),
        }
        if node.subplan_name:
            row["subplan"] = node.subplan_name
        digest.append(row)
        for child in node.children:
            visit(child, depth + 1)

    visit(result.plan, 0)
    return digest
