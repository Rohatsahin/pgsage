import io
import sys
from pathlib import Path

import pytest

from pgsage import cli
from pgsage.cli import MAX_QUERY_BYTES, read_query

QUERY = "SELECT *\nFROM orders\nWHERE customer_id = 7"


class FakeStdin(io.TextIOWrapper):
    def __init__(self, data: bytes) -> None:
        super().__init__(io.BytesIO(data), encoding="utf-8")


def set_tty(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    def is_tty() -> bool:
        return value

    monkeypatch.setattr(cli, "_stdin_is_tty", is_tty)


def fake_editor(monkeypatch: pytest.MonkeyPatch, typed: str | None) -> None:
    def edit(text: str, extension: str) -> str | None:
        return None if typed is None else text + typed

    monkeypatch.setattr(cli.click, "edit", edit)


def pipe(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", FakeStdin(data))
    set_tty(monkeypatch, False)


def test_inline_sql_is_returned_trimmed() -> None:
    assert read_query(sql="  SELECT 1  \n") == "SELECT 1"


def test_a_file_keeps_its_newlines(tmp_path: Path) -> None:
    path = tmp_path / "q.sql"
    path.write_text(QUERY)
    assert read_query(file=path) == QUERY


def test_a_utf8_bom_from_a_windows_editor_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "q.sql"
    path.write_bytes(b"\xef\xbb\xbfSELECT 1")
    assert read_query(file=path) == "SELECT 1"


def test_dash_reads_the_query_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe(monkeypatch, QUERY.encode())
    assert read_query(file=Path("-")) == QUERY


def test_a_piped_query_is_used_when_no_source_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe(monkeypatch, QUERY.encode())
    assert read_query() == QUERY


def test_a_terminal_with_no_source_opens_the_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    set_tty(monkeypatch, True)
    fake_editor(monkeypatch, "SELECT 42\n")
    assert read_query() == "SELECT 42"


def test_the_edit_flag_opens_the_editor_even_when_input_is_piped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipe(monkeypatch, b"ignored")
    fake_editor(monkeypatch, "SELECT 7")
    assert read_query(edit=True) == "SELECT 7"


def test_the_editor_header_never_reaches_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_editor(monkeypatch, "SELECT 1")
    assert "save and close" not in read_query(edit=True)


def test_closing_the_editor_without_saving_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_editor(monkeypatch, None)
    with pytest.raises(ValueError, match="without saving"):
        read_query(edit=True)


@pytest.mark.parametrize("combination", [("sql", "file"), ("sql", "edit"), ("file", "edit")])
def test_two_sources_at_once_is_ambiguous(combination: tuple[str, str], tmp_path: Path) -> None:
    kwargs = {"sql": "SELECT 1", "file": tmp_path / "q.sql", "edit": True}
    chosen = {key: kwargs[key] for key in combination}
    with pytest.raises(ValueError, match="only one of"):
        read_query(**chosen)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("empty", ["", "   ", "\n\n\t\n"])
def test_an_empty_query_is_rejected(empty: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        read_query(sql=empty)


def test_a_binary_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "q.bin"
    path.write_bytes(b"SELECT \x00\x01\x02")
    with pytest.raises(ValueError, match="binary"):
        read_query(file=path)


def test_a_file_that_is_not_utf8_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "q.sql"
    path.write_bytes(b"SELECT '\xff\xfe'")
    with pytest.raises(ValueError, match="UTF-8"):
        read_query(file=path)


def test_an_enormous_file_is_refused_before_it_is_read(tmp_path: Path) -> None:
    path = tmp_path / "big.sql"
    path.write_bytes(b"x" * (MAX_QUERY_BYTES + 1))
    with pytest.raises(ValueError, match="larger than"):
        read_query(file=path)


def test_an_enormous_stdin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe(monkeypatch, b"x" * (MAX_QUERY_BYTES + 1))
    with pytest.raises(ValueError, match="larger than"):
        read_query(file=Path("-"))


def test_a_missing_file_is_an_os_error_the_cli_reports(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        read_query(file=tmp_path / "nope.sql")
