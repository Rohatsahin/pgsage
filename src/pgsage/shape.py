from __future__ import annotations

from pydantic import BaseModel

from pgsage.plan import ExplainResult, PlanNode


class ShapeNode(BaseModel):
    depth: int
    node: str
    relation: str | None = None
    index: str | None = None


class ShapeDiff(BaseModel):
    changed: bool
    before: list[ShapeNode]
    after: list[ShapeNode]
    slowdown: float | None = None


def plan_shape(result: ExplainResult) -> list[ShapeNode]:
    shape: list[ShapeNode] = []

    def visit(node: PlanNode, depth: int) -> None:
        shape.append(
            ShapeNode(
                depth=depth, node=node.node_type, relation=node.relation_name, index=node.index_name
            )
        )
        for child in node.children:
            visit(child, depth + 1)

    visit(result.plan, 0)
    return shape


def diff_shapes(before: ExplainResult, after: ExplainResult) -> ShapeDiff:
    old, new = plan_shape(before), plan_shape(after)
    slowdown = None
    if before.execution_time and after.execution_time:
        slowdown = after.execution_time / before.execution_time
    return ShapeDiff(changed=old != new, before=old, after=new, slowdown=slowdown)
