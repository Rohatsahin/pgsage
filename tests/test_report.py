import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pgsage.ai import Explanation
from pgsage.report import (
    build_script,
    change_statements,
    render_json,
    render_markdown,
    write_report,
)
from pgsage.tune import TuneReport, VerifiedChange
from pgsage.verify import IndexSuggestion, PlanSummary, StatsSuggestion, VerifyResult

NOW = datetime(2026, 10, 12, 9, 30, 0, tzinfo=UTC)


def summary(ms: float, nodes: list[str], uses_index: bool = False) -> PlanSummary:
    return PlanSummary(
        total_cost=100.0, execution_ms=ms, node_types=nodes, uses_new_index=uses_index
    )


def proven_index(**extra: Any) -> VerifiedChange:
    suggestion = IndexSuggestion(table="orders", columns=["customer_id"], **extra)
    result = VerifyResult(
        kind="index",
        statement=suggestion.ddl,
        before=summary(14.0, ["Seq Scan"]),
        after=summary(1.0, ["Index Scan"], True),
    )
    return VerifiedChange(
        description="Index the filter column",
        confidence="high",
        expected_effect="avoids reading every row",
        index=suggestion,
        result=result,
    )


def proven_statistics() -> VerifiedChange:
    suggestion = StatsSuggestion(table="orders", columns=["a", "b"])
    result = VerifyResult(
        kind="statistics",
        statement=suggestion.ddl,
        before=summary(40.0, ["Nested Loop"]),
        after=summary(10.0, ["Hash Join"]),
    )
    return VerifiedChange(description="Correlated columns", statistics=suggestion, result=result)


def proven_rewrite() -> VerifiedChange:
    result = VerifyResult(
        kind="rewrite",
        statement="SELECT * FROM t WHERE a >= 1",
        before=summary(30.0, ["Seq Scan"]),
        after=summary(2.0, ["Index Scan"]),
        equivalent=True,
    )
    return VerifiedChange(description="Make it sargable", result=result)


def unused_index() -> VerifiedChange:
    suggestion = IndexSuggestion(table="orders", columns=["created_at"])
    result = VerifyResult(
        kind="index",
        statement=suggestion.ddl,
        before=summary(14.0, ["Seq Scan"]),
        after=summary(14.1, ["Seq Scan"], False),
    )
    return VerifiedChange(description="Wrong column", index=suggestion, result=result)


def failed_rewrite() -> VerifiedChange:
    result = VerifyResult.failed("rewrite", "SELECT nope FROM t", "UndefinedColumn: nope")
    return VerifiedChange(description="Hallucinated column", result=result)


def make_report(*changes: VerifiedChange, **explanation: Any) -> TuneReport:
    return TuneReport(
        explanation=Explanation(
            summary=explanation.get("summary", "A sequential scan dominates."),
            root_cause=explanation.get("root_cause", "No index on customer_id."),
            evidence=explanation.get("evidence", ["Seq Scan on orders: 300000 rows read"]),
            actions=[],
            rejected=explanation.get("rejected", []),
        ),
        verified=list(changes),
        rounds=2,
        query="SELECT * FROM orders WHERE customer_id = 7",
        baseline_ms=14.0,
        hotspots=[
            {
                "node": "Seq Scan",
                "relation": "orders",
                "index": None,
                "self_ms": 13.2,
                "estimate_off_by": 12.0,
                "rows_removed_by_filter": 299700.0,
            }
        ],
    )


def test_the_script_is_plain_idempotent_sql_that_builds_indexes_concurrently() -> None:
    script = build_script(make_report(proven_index()), NOW)
    assert script is not None
    assert (
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS "idx_orders_customer_id" '
        'ON "orders" ("customer_id");'
    ) in script
    assert "2026-10-12 09:30:00 UTC" in script
    assert "outside a transaction block" in script


def test_the_script_can_avoid_concurrently() -> None:
    script = build_script(make_report(proven_index()), NOW, concurrently=False)
    assert script is not None
    assert "CONCURRENTLY" not in script
    assert 'CREATE INDEX IF NOT EXISTS "idx_orders_customer_id"' in script


def test_statistics_are_idempotent_and_followed_by_analyze() -> None:
    statements = change_statements(proven_statistics())
    assert statements[0].startswith('CREATE STATISTICS IF NOT EXISTS "stats_orders_a_b"')
    assert statements[1] == 'ANALYZE "orders";'


def test_partial_and_covering_indexes_keep_their_shape_in_the_script() -> None:
    change = proven_index(where="status = 'open'", include=["created_at"])
    (statement,) = change_statements(change)
    assert statement.endswith("""INCLUDE ("created_at") WHERE status = 'open';""")


def test_only_proven_ddl_is_scripted_and_rewrites_are_not() -> None:
    script = build_script(make_report(proven_index(), unused_index(), proven_rewrite()), NOW)
    assert script is not None
    assert "created_at" not in script
    assert "SELECT * FROM t" not in script
    assert build_script(make_report(unused_index(), proven_rewrite()), NOW) is None


def test_identifier_quotes_are_escaped_in_the_script() -> None:
    suggestion = IndexSuggestion(table="t", columns=['x"); DROP TABLE v; --'])
    change = proven_index()
    change = change.model_copy(update={"index": suggestion})
    (statement,) = change_statements(change)
    assert '"x""); DROP TABLE v; --"' in statement


def test_markdown_has_every_section_for_a_mixed_run() -> None:
    text = render_markdown(
        make_report(
            proven_index(), unused_index(), failed_rewrite(), rejected=["action 3 rejected"]
        ),
        NOW,
    )
    for heading in (
        "# pgsage tuning report",
        "## Query",
        "## Diagnosis",
        "## Results",
        "## Proven changes",
        "## Apply",
        "## Not proven",
        "## Model output that was ignored",
        "## Caveats",
    ):
        assert heading in text, heading
    assert "✅ 1 proven change(s), ❌ 2 not proven" in text
    assert "Baseline execution time: **14.00 ms**" in text
    assert "| Seq Scan | orders | 13.20 ms | 12.0x | 299,700 |" in text
    assert "| 1 | index |" in text and "✅ proven" in text and "14.0x" in text
    assert "⚠️ failed" in text and "❌ not faster" in text
    assert "The planner did not use the new index." in text
    assert "UndefinedColumn: nope" in text


def test_a_run_with_nothing_to_fix_says_so() -> None:
    text = render_markdown(make_report(), NOW)
    assert "no change was proposed" in text
    assert "## Proven changes" not in text and "## Apply" not in text


def test_a_run_where_nothing_was_proven_says_so() -> None:
    text = render_markdown(make_report(unused_index()), NOW)
    assert "no change was proven faster" in text
    assert "## Apply" not in text


def test_rewrites_are_reported_but_pointed_at_application_code() -> None:
    text = render_markdown(make_report(proven_rewrite()), NOW)
    assert "SELECT * FROM t WHERE a >= 1" in text
    assert "Equivalence: same columns and same rows on this data." in text
    assert "not part of the SQL script" in text
    assert "## Apply" not in text


def test_caveats_are_specific_to_what_was_proven() -> None:
    index_only = render_markdown(make_report(proven_index()), NOW)
    stats_only = render_markdown(make_report(proven_statistics()), NOW)
    assert "CREATE INDEX CONCURRENTLY" in index_only
    assert "Extended statistics only take effect" not in index_only
    assert "Extended statistics only take effect after `ANALYZE`" in stats_only
    assert "write" in index_only.split("## Caveats")[1].lower()


def test_untrusted_model_text_cannot_inject_html_or_links_into_the_report() -> None:
    hostile = "<script>alert(1)</script> [click](http://evil.example) `code` *bold*"
    text = render_markdown(
        make_report(
            proven_index(),
            summary=hostile,
            root_cause=hostile,
            evidence=[hostile],
            rejected=[hostile],
        ),
        NOW,
    )
    assert "<script>" not in text
    assert "[click](http://evil.example)" not in text
    assert "\\[click\\]" in text
    assert "&lt;script&gt;" in text


def test_statements_with_pipes_and_backticks_cannot_break_the_table_or_fences() -> None:
    result = VerifyResult(
        kind="rewrite",
        statement="SELECT a || b, '`x`' FROM t",
        before=summary(30.0, ["Seq Scan"]),
        after=summary(2.0, ["Index Scan"]),
        equivalent=True,
    )
    change = VerifiedChange(description="d", result=result)
    text = render_markdown(make_report(change), NOW)
    row = next(line for line in text.splitlines() if line.startswith("| 1 |"))
    assert row.count("|") - row.count("\\|") == 8
    assert "````" in text or "```sql\nSELECT a || b" in text


def test_the_query_is_fenced_even_if_it_contains_a_fence() -> None:
    report = make_report(proven_index()).model_copy(update={"query": "SELECT '```' AS x"})
    text = render_markdown(report, NOW)
    assert "````sql\nSELECT '```' AS x\n````" in text


def test_the_report_never_contains_connection_details() -> None:
    text = render_markdown(make_report(proven_index()), NOW) + render_json(
        make_report(proven_index()), NOW
    )
    for word in ("postgresql://", "password", "PGSAGE_DSN"):
        assert word not in text


def test_json_is_complete_machine_readable_and_versioned() -> None:
    data = json.loads(
        render_json(make_report(proven_index(), unused_index(), failed_rewrite()), NOW)
    )
    assert data["schema_version"] == 1
    assert data["generated_at"].startswith("2026-10-12T09:30:00")
    assert data["baseline_ms"] == 14.0 and data["rounds"] == 2
    assert data["diagnosis"]["root_cause"] == "No index on customer_id."
    first, second, third = data["changes"]
    assert first["proven"] is True and first["speedup"] == pytest.approx(14.0)
    assert first["sql"] == [
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS "idx_orders_customer_id" ON "orders" ("customer_id");'
    ]
    assert second["proven"] is False and second["sql"] == []
    assert third["error"] == "UndefinedColumn: nope"
    assert data["script"].count("CREATE INDEX") == 1
    assert data["hotspots"][0]["node"] == "Seq Scan"


def test_json_script_is_null_when_nothing_was_proven() -> None:
    assert json.loads(render_json(make_report(unused_index()), NOW))["script"] is None


def test_write_report_creates_three_files_when_something_is_proven(tmp_path: Path) -> None:
    paths = write_report(tmp_path / "out" / "reports", make_report(proven_index()), NOW)
    assert [p.suffix for p in paths] == [".md", ".json", ".sql"]
    assert all(p.name.startswith("pgsage-20261012-093000") for p in paths)
    assert "CREATE INDEX" in paths[2].read_text()


def test_write_report_skips_the_script_when_nothing_was_proven(tmp_path: Path) -> None:
    paths = write_report(tmp_path, make_report(unused_index()), NOW)
    assert [p.suffix for p in paths] == [".md", ".json"]


def test_write_report_never_overwrites_an_earlier_run(tmp_path: Path) -> None:
    first = write_report(tmp_path, make_report(proven_index()), NOW)
    second = write_report(tmp_path, make_report(proven_index()), NOW)
    assert not {p.name for p in first} & {p.name for p in second}
    assert len(list(tmp_path.glob("*.md"))) == 2
    assert first[0].read_text() != "" and second[0].name.startswith("pgsage-20261012-093000-2")
