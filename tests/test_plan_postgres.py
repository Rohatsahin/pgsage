import psycopg
import pytest

from pgsage import parse_explain
from tests.conftest import explain_json

pytestmark = pytest.mark.docker


def test_parses_real_analyze_output(pg_conn: psycopg.Connection) -> None:
    raw = explain_json(pg_conn, "SELECT * FROM orders WHERE customer_id = 7")
    result = parse_explain(raw)
    scan = result.plan
    assert result.analyzed
    assert scan.node_type == "Seq Scan"
    assert scan.relation_name == "orders"
    assert scan.total_actual_rows == 2000
    assert scan.rows_removed_by_filter == 198000
    assert scan.self_time is not None


def test_parses_real_join_with_children(pg_conn: psycopg.Connection) -> None:
    raw = explain_json(
        pg_conn,
        "SELECT * FROM orders a JOIN orders b ON a.id = b.id WHERE a.customer_id = 1",
    )
    nodes = parse_explain(raw).nodes()
    assert len(nodes) >= 3
    assert all(n.analyzed for n in nodes)


def test_parses_plain_explain(pg_conn: psycopg.Connection) -> None:
    raw = explain_json(pg_conn, "SELECT * FROM orders", analyze=False)
    assert not parse_explain(raw).analyzed
