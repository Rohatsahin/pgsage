import pytest

from pgsage import plan_digest
from pgsage.context import get_plan, table_info, tables_in_plan
from pgsage.verify import UnsafeStatementError

pytestmark = pytest.mark.docker


def test_get_plan_returns_analyzed_plan(pg_dsn: str) -> None:
    result = get_plan(pg_dsn, "SELECT * FROM orders WHERE customer_id = 7")
    assert result.analyzed
    assert tables_in_plan(result) == [("public", "orders")]
    digest = plan_digest(result)
    assert digest[0]["relation"] == "orders"
    assert digest[0]["rows_removed_by_filter"] == 198000


def test_get_plan_rejects_writes(pg_dsn: str) -> None:
    with pytest.raises(UnsafeStatementError):
        get_plan(pg_dsn, "DELETE FROM orders")


def test_table_info_reports_columns_size_and_stats(pg_dsn: str) -> None:
    info = table_info(pg_dsn, "orders")
    assert [c["column_name"] for c in info["columns"]] == ["id", "customer_id", "created_at"]
    assert info["size"]["estimated_rows"] == 200000
    stats = {s["column_name"]: s for s in info["column_stats"]}
    assert stats["customer_id"]["n_distinct"] == 100


def test_table_info_unknown_table(pg_dsn: str) -> None:
    with pytest.raises(ValueError, match="not found"):
        table_info(pg_dsn, "nope")
