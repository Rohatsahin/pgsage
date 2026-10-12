from __future__ import annotations

from typing import Any


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": _STRING}

_INDEX = _object(
    {
        "table": _STRING,
        "columns": _STRINGS,
        "schema_name": _nullable(_STRING),
        "method": {"type": "string", "enum": ["btree", "hash", "gin", "brin"]},
        "include": _STRINGS,
        "where": _nullable(_STRING),
    }
)

_STATISTICS = _object(
    {
        "table": _STRING,
        "columns": _STRINGS,
        "kinds": {
            "type": "array",
            "items": {"type": "string", "enum": ["dependencies", "ndistinct", "mcv"]},
        },
        "schema_name": _nullable(_STRING),
    }
)

_ACTION = _object(
    {
        "description": _STRING,
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "expected_effect": _nullable(_STRING),
        "index": _nullable(_INDEX),
        "statistics": _nullable(_STATISTICS),
        "rewrite": _nullable(_STRING),
        "sql": _nullable(_STRING),
    }
)

EXPLANATION_SCHEMA: dict[str, Any] = _object(
    {
        "summary": _STRING,
        "root_cause": _STRING,
        "evidence": _STRINGS,
        "actions": {"type": "array", "items": _ACTION},
    }
)
