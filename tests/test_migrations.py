from datetime import UTC, datetime
from pathlib import Path

from pgsage.migrations import migration_sql, write_migrations
from pgsage.verify import IndexSuggestion, PlanSummary, StatsSuggestion, VerifyResult

NOW = datetime(2026, 10, 12, 9, 30, 0, tzinfo=UTC)


def result(improved: bool, kind: str = "index") -> VerifyResult:
    fast = PlanSummary(total_cost=1.0, execution_ms=1.0, node_types=[], uses_new_index=True)
    slow = PlanSummary(total_cost=9.0, execution_ms=9.0, node_types=[])
    return VerifyResult(
        kind=kind,  # pyright: ignore[reportArgumentType]
        statement="x",
        before=slow if improved else fast,
        after=fast if improved else slow,
    )


def test_index_migration_is_idempotent_sql() -> None:
    sql = migration_sql(IndexSuggestion(table="orders", columns=["customer_id"]))
    assert (
        sql == 'CREATE INDEX IF NOT EXISTS "idx_orders_customer_id" ON "orders" ("customer_id");\n'
    )


def test_statistics_migration_is_idempotent_sql() -> None:
    sql = migration_sql(StatsSuggestion(table="orders", columns=["a", "b"]))
    assert sql.startswith('CREATE STATISTICS IF NOT EXISTS "stats_orders_a_b"')


def test_only_proven_changes_are_written_with_ordered_flyway_versions(tmp_path: Path) -> None:
    proven = [
        (IndexSuggestion(table="orders", columns=["customer_id"]), result(True)),
        (IndexSuggestion(table="orders", columns=["created_at"]), result(False)),
        (IndexSuggestion(table="users", columns=["email"]), result(True)),
    ]
    paths = write_migrations(tmp_path / "db" / "migration", proven, now=NOW)
    assert [p.name for p in paths] == [
        "V20261012093000__idx_orders_customer_id.sql",
        "V20261012093001__idx_users_email.sql",
    ]
    assert paths[0].read_text().startswith("CREATE INDEX IF NOT EXISTS")


def test_running_twice_in_the_same_second_never_overwrites(tmp_path: Path) -> None:
    proven = [(IndexSuggestion(table="orders", columns=["customer_id"]), result(True))]
    first = write_migrations(tmp_path, proven, now=NOW)
    second = write_migrations(tmp_path, proven, now=NOW)
    assert first != second
    assert len(list(tmp_path.glob("V*.sql"))) == 2


def test_lossy_or_long_names_cannot_collide() -> None:
    spaced = IndexSuggestion(table="t", columns=["a b"])
    underscored = IndexSuggestion(table="t", columns=["a_b"])
    assert spaced.suggested_name != underscored.suggested_name
    long_one = IndexSuggestion(table="t", columns=["c" * 70 + "1"])
    long_two = IndexSuggestion(table="t", columns=["c" * 70 + "2"])
    assert long_one.suggested_name != long_two.suggested_name
    assert len(long_one.suggested_name) <= 63


def test_statistics_are_created_in_the_tables_schema() -> None:
    stats = StatsSuggestion(table="orders", columns=["a", "b"], schema_name="app")
    assert 'CREATE STATISTICS "app"."stats_orders_a_b"' in stats.ddl


def test_identifier_quotes_are_escaped_in_migrations() -> None:
    sql = migration_sql(IndexSuggestion(table="t", columns=['x"); DROP TABLE v; --']))
    assert '"x""); DROP TABLE v; --"' in sql
