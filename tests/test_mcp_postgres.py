import json

import pytest
from mcp import Client
from mcp.types import TextContent

from pgsage.mcp_server import server

pytestmark = pytest.mark.docker

QUERY = "SELECT * FROM orders WHERE customer_id = 7"


@pytest.fixture(autouse=True)
def configured_dsn(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGSAGE_DSN", pg_dsn)


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
async def test_get_query_plan_rejects_writes() -> None:
    async with Client(server) as client:
        result = await client.call_tool("get_query_plan", {"sql": "DELETE FROM orders"})
    assert result.is_error


@pytest.mark.anyio
async def test_tools_have_no_dsn_parameter() -> None:
    async with Client(server) as client:
        tools = await client.list_tools()
    for tool in tools.tools:
        assert "dsn" not in tool.input_schema.get("properties", {}), tool.name


@pytest.mark.anyio
async def test_tools_refuse_to_run_without_configured_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PGSAGE_DSN")
    async with Client(server) as client:
        result = await client.call_tool("get_query_plan", {"sql": QUERY})
    assert result.is_error


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
