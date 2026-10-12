from __future__ import annotations

import re
from collections.abc import Callable

LEXEME = re.compile(
    r"(?P<comment>--[^\n]*|/\*.*?\*/)"
    r"|(?P<string>[eE]'(?:[^'\\]|\\.|'')*'|'(?:[^']|'')*'|\$(?P<tag>\w*)\$.*?\$(?P=tag)\$)"
    r"|(?P<ident>\"(?:[^\"]|\"\")*\")"
    r"|(?P<number>(?<![\w\"$])-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?![\w\"]))",
    re.DOTALL,
)

Handler = Callable[[str, str], str]


def substitute(sql: str, handler: Handler) -> str:
    def replace(match: re.Match[str]) -> str:
        kind = match.lastgroup
        if kind == "tag":
            kind = "string"
        assert kind is not None
        return handler(kind, match.group(0))

    return LEXEME.sub(replace, sql)


def mask(sql: str) -> str:
    def handler(kind: str, text: str) -> str:
        return {"comment": " ", "string": "''", "ident": '""'}.get(kind, text)

    return substitute(sql, handler)


def redact_condition(text: str) -> str:
    def handler(kind: str, _: str) -> str:
        return " " if kind == "comment" else "?" if kind in ("string", "number") else _

    return substitute(text, handler)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
