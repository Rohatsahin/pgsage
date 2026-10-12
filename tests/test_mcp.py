import json
import subprocess
import sys
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from pgsage.mcp_server import server

PLAN = [
    {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Filter": "(customer_id = 7)",
            "Startup Cost": 0.0,
            "Total Cost": 10.0,
            "Plan Rows": 2000,
            "Actual Rows": 2000,
            "Actual Loops": 1,
            "Actual Total Time": 20.0,
            "Rows Removed by Filter": 198000,
        },
        "Execution Time": 20.0,
    }
]


QUERY = "SELECT * FROM orders WHERE customer_id = 7"


@pytest.fixture(autouse=True)
def configured_dsn(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGSAGE_DSN", pg_dsn)


async def error_text(tool: str, arguments: dict[str, Any]) -> str:
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error
    first = result.content[0]
    assert isinstance(first, TextContent)
    return first.text


@pytest.mark.anyio
async def test_summarize_plan_tool_returns_digest() -> None:
    async with Client(server) as client:
        result = await client.call_tool("summarize_plan", {"plan_json": json.dumps(PLAN)})
    assert not result.is_error
    assert "rows_removed_by_filter" in str(result.content)


def test_pgsage_mcp_explains_a_missing_extra_instead_of_a_traceback() -> None:
    script = "import sys; sys.modules['mcp'] = None; from pgsage.mcp_main import main; main()"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 2
    assert "pip install 'pgsage[mcp]'" in result.stderr
    assert "Traceback" not in result.stderr


def test_the_module_entry_point_runs_the_cli() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pgsage", "--version"], capture_output=True, text=True
    )
    assert result.returncode == 0 and result.stdout.startswith("pgsage ")


@pytest.mark.anyio
async def test_agent_loop_plan_then_table_info_then_verify() -> None:
    async with Client(server) as client:
        plan = await client.call_tool("get_query_plan", {"sql": QUERY})
        info = await client.call_tool("get_table_info", {"table": "orders"})
        verdict = await client.call_tool(
            "verify_index_suggestion",
            {"sql": QUERY, "table": "orders", "columns": ["customer_id"]},
        )
    assert not plan.is_error and "Seq Scan" in str(plan.content)
    assert not info.is_error and "customer_id" in str(info.content)
    assert not verdict.is_error
    first = verdict.content[0]
    assert isinstance(first, TextContent)
    assert json.loads(first.text)["improved"] is True


@pytest.mark.anyio
async def test_tools_have_no_dsn_parameter() -> None:
    async with Client(server) as client:
        tools = await client.list_tools()
    for tool in tools.tools:
        assert "dsn" not in tool.input_schema.get("properties", {}), tool.name


@pytest.mark.anyio
async def test_partial_and_covering_indexes_can_be_verified_through_mcp() -> None:
    async with Client(server) as client:
        partial = await client.call_tool(
            "verify_index_suggestion",
            {
                "sql": "SELECT id FROM orders WHERE customer_id = 7",
                "table": "orders",
                "columns": ["id"],
                "where": "customer_id = 7",
            },
        )
        unsafe = await client.call_tool(
            "verify_index_suggestion",
            {
                "sql": QUERY,
                "table": "orders",
                "columns": ["id"],
                "where": "id = 1; DROP TABLE orders",
            },
        )
    first = partial.content[0]
    assert isinstance(first, TextContent)
    assert not partial.is_error
    assert json.loads(first.text)["statement"].endswith("WHERE customer_id = 7")
    assert unsafe.is_error


@pytest.mark.anyio
async def test_an_agent_is_told_why_a_call_failed() -> None:
    assert "not found" in await error_text("get_table_info", {"table": "nope"})
    assert "single read-only" in await error_text(
        "verify_query_rewrite", {"sql": QUERY, "rewritten_sql": "DELETE FROM orders"}
    )
    assert "at least 1" in await error_text("get_top_queries", {"limit": -1})
    assert "placeholders" in await error_text("get_query_plan", {"sql": "SELECT $1"})


@pytest.mark.anyio
async def test_database_errors_reach_the_agent_with_their_type() -> None:
    text = await error_text(
        "verify_index_suggestion", {"sql": QUERY, "table": "orders", "columns": ["no_such_column"]}
    )
    assert "UndefinedColumn" in text


@pytest.mark.anyio
async def test_a_missing_dsn_says_what_to_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PGSAGE_DSN")
    assert "PGSAGE_DSN" in await error_text("get_query_plan", {"sql": QUERY})
