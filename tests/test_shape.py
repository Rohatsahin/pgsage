import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from pgsage import parse_explain
from pgsage.cli import app
from pgsage.shape import diff_shapes


def plan(node: str, index: str | None = None, ms: float = 1.0) -> list[dict[str, Any]]:
    body: dict[str, Any] = {
        "Node Type": node,
        "Relation Name": "t",
        "Startup Cost": 0.0,
        "Total Cost": 1.0,
        "Plan Rows": 1,
    }
    if index:
        body["Index Name"] = index
    return [{"Plan": body, "Execution Time": ms}]


def test_unchanged_shape_ignores_timing() -> None:
    diff = diff_shapes(parse_explain(plan("Seq Scan", ms=1)), parse_explain(plan("Seq Scan", ms=5)))
    assert not diff.changed
    assert diff.slowdown == 5


def test_index_dropped_is_a_shape_change() -> None:
    diff = diff_shapes(
        parse_explain(plan("Index Scan", "t_idx")), parse_explain(plan("Seq Scan", ms=40))
    )
    assert diff.changed
    assert [n.node for n in diff.after] == ["Seq Scan"]


def test_diff_command_exit_codes(tmp_path: Path) -> None:
    base = tmp_path
    (base / "a.json").write_text(json.dumps(plan("Index Scan", "t_idx")))
    (base / "same.json").write_text(json.dumps(plan("Index Scan", "t_idx", ms=2)))
    (base / "bad.json").write_text(json.dumps(plan("Seq Scan", ms=40)))
    runner = CliRunner()
    assert (
        runner.invoke(app, ["diff", str(base / "a.json"), str(base / "same.json")]).exit_code == 0
    )
    changed = runner.invoke(app, ["diff", str(base / "a.json"), str(base / "bad.json")])
    assert changed.exit_code == 1
    assert "Index Scan > " not in changed.output
    assert "Seq Scan" in changed.output
