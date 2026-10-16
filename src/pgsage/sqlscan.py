from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass, field

SPECIAL = re.compile(r"--|/\*|['\"$]|[eE](?=')")
TAG = r"(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?"
UNTERMINATED_DOLLAR = re.compile(rf"(?<![A-Za-z0-9_$\u0080-\U0010ffff])\${TAG}\$")
DOLLAR_TAGS = re.compile(rf"(?=(\${TAG}\$))")
ASCII_LIMIT = 127
DIGITS = "0123456789"

Handler = Callable[[str, str], str]


def _is_identifier_char(char: str) -> bool:
    return char.isalnum() or char in "_$" or ord(char) > ASCII_LIMIT


def _end_of_quoted(sql: str, start: int, quote: str) -> int:
    position = start + 1
    while True:
        found = sql.find(quote, position)
        if found == -1:
            return -1
        if found + 1 < len(sql) and sql[found + 1] == quote:
            position = found + 2
            continue
        return found + 1


def _end_of_escape_string(sql: str, quote_at: int) -> int:
    position = quote_at + 1
    length = len(sql)
    while position < length:
        char = sql[position]
        if char == "\\":
            position += 2
        elif char == "'":
            if position + 1 < length and sql[position + 1] == "'":
                position += 2
            else:
                return position + 1
        else:
            position += 1
    return -1


@dataclass
class _State:
    comment_unclosed: bool = False
    dollar_tags: dict[str, list[int]] | None = field(default=None)

    def tag_positions(self, sql: str) -> dict[str, list[int]]:
        if self.dollar_tags is None:
            index: dict[str, list[int]] = {}
            for found in DOLLAR_TAGS.finditer(sql):
                index.setdefault(found.group(1), []).append(found.start())
            self.dollar_tags = index
        return self.dollar_tags


def _end_of_dollar_string(sql: str, start: int, state: _State) -> int:
    position = start + 1
    while position < len(sql) and (
        sql[position].isalnum() or sql[position] == "_" or ord(sql[position]) > ASCII_LIMIT
    ):
        position += 1
    if position >= len(sql) or sql[position] != "$":
        return -1
    tag = sql[start + 1 : position]
    if tag and tag[0] in DIGITS:
        return -1
    closer = f"${tag}$"
    positions = state.tag_positions(sql).get(closer, [])
    at = bisect_left(positions, position + 1)
    return positions[at] + len(closer) if at < len(positions) else -1


def _line_end(sql: str, start: int) -> int:
    ends = [found for found in (sql.find("\n", start), sql.find("\r", start)) if found != -1]
    return min(ends) if ends else len(sql)


def _lexeme(sql: str, found: re.Match[str], state: _State) -> tuple[str, int] | None:
    start, token = found.start(), found.group(0)
    after_identifier = start > 0 and _is_identifier_char(sql[start - 1])
    if token == "--":
        return "comment", _line_end(sql, start)
    if token == "/*":
        if state.comment_unclosed:
            return None
        closing = sql.find("*/", start + 2)
        if closing == -1:
            state.comment_unclosed = True
            return None
        return "comment", closing + 2
    if token == "'":
        end = _end_of_quoted(sql, start, "'")
        return ("string", end) if end != -1 else None
    if token == '"':
        end = _end_of_quoted(sql, start, '"')
        return ("ident", end) if end != -1 else None
    if token == "$" and not after_identifier:
        end = _end_of_dollar_string(sql, start, state)
        return ("string", end) if end != -1 else None
    if token in ("e", "E") and not after_identifier:
        end = _end_of_escape_string(sql, start + 1)
        return ("string", end) if end != -1 else None
    return None


def substitute(sql: str, handler: Handler) -> str:
    pieces: list[str] = []
    copied = 0
    position = 0
    state = _State()
    while (found := SPECIAL.search(sql, position)) is not None:
        lexeme = _lexeme(sql, found, state)
        if lexeme is None:
            position = found.start() + 1
            continue
        kind, end = lexeme
        pieces.append(sql[copied : found.start()])
        pieces.append(handler(kind, sql[found.start() : end]))
        copied = position = end
    pieces.append(sql[copied:])
    return "".join(pieces)


def mask(sql: str) -> str:
    def handler(kind: str, text: str) -> str:
        return {"comment": " ", "string": "''", "ident": '""'}.get(kind, text)

    return substitute(sql, handler)


def mask_keeping_names(sql: str) -> str:
    def handler(kind: str, text: str) -> str:
        if kind == "ident":
            return text[1:-1].replace('""', '"')
        return {"comment": " ", "string": "''"}.get(kind, text)

    return substitute(sql, handler)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def is_ambiguous(sql: str) -> bool:
    def handler(kind: str, text: str) -> str:
        return {"comment": " ", "string": "\x01", "ident": "\x02"}.get(kind, text)

    residue = substitute(sql, handler)
    if any(token in residue for token in ("'", '"', "/*", "*/")):
        return True
    return UNTERMINATED_DOLLAR.search(residue) is not None
