from __future__ import annotations

import re
from collections.abc import Callable

LEXEME = re.compile(
    r"(?P<comment>--[^\n\r]*|/\*.*?\*/)"
    r"|(?P<string>(?<![\w$])[eE]'(?:[^'\\]|\\.|'')*'|'(?:[^']|'')*'"
    r"|\$(?P<tag>(?:[^\W\d]\w*)?)\$.*?\$(?P=tag)\$)"
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


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def is_ambiguous(sql: str) -> bool:
    def handler(kind: str, text: str) -> str:
        return {"comment": " ", "string": "\x01", "ident": "\x02"}.get(kind, text)

    residue = substitute(sql, handler)
    return any(token in residue for token in ("'", '"', "/*", "*/"))
