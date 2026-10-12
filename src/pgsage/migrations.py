from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pgsage.verify import IndexSuggestion, StatsSuggestion, VerifyResult


def migration_sql(suggestion: IndexSuggestion | StatsSuggestion) -> str:
    if isinstance(suggestion, IndexSuggestion):
        ddl = suggestion.create_ddl(suggestion.suggested_name, if_not_exists=True)
        return f"{ddl};\n"
    ddl = suggestion.ddl.replace("CREATE STATISTICS ", "CREATE STATISTICS IF NOT EXISTS ", 1)
    return f"{ddl};\n"


def _next_version(directory: Path, floor: int) -> int:
    version = floor
    while any(directory.glob(f"V{version}__*")):
        version += 1
    return version


def write_migrations(
    directory: Path,
    proven: Sequence[tuple[IndexSuggestion | StatsSuggestion, VerifyResult]],
    now: datetime | None = None,
) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    version = int((now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S"))
    written: list[Path] = []
    for suggestion, result in proven:
        if not result.improved:
            continue
        version = _next_version(directory, version)
        path = directory / f"V{version}__{suggestion.suggested_name}.sql"
        path.write_text(migration_sql(suggestion))
        written.append(path)
        version += 1
    return written
