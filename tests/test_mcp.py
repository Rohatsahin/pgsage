import json

import pytest
from mcp import Client

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


@pytest.mark.anyio
async def test_lists_all_tools() -> None:
    async with Client(server) as client:
        tools = await client.list_tools()
    assert {t.name for t in tools.tools} == {
        "get_query_plan",
        "summarize_plan",
        "get_table_info",
        "get_top_queries",
        "verify_index_suggestion",
        "verify_query_rewrite",
    }


@pytest.mark.anyio
async def test_summarize_plan_tool_returns_digest() -> None:
    async with Client(server) as client:
        result = await client.call_tool("summarize_plan", {"plan_json": json.dumps(PLAN)})
    assert not result.is_error
    assert "rows_removed_by_filter" in str(result.content)
