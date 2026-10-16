from __future__ import annotations

from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pydantic import ValidationError

CONNECT_TIMEOUT = 10
APPLICATION_NAME = "pgsage"
MAX_MESSAGE_LENGTH = 500


def _conninfo(dsn: str) -> str:
    try:
        params = conninfo_to_dict(dsn)
    except psycopg.ProgrammingError as error:
        raise ValueError("invalid connection string (value not shown)") from error
    params.setdefault("connect_timeout", CONNECT_TIMEOUT)
    params.setdefault("application_name", APPLICATION_NAME)
    return make_conninfo("", **params)


def connect(dsn: str) -> psycopg.Connection[tuple[Any, ...]]:
    return psycopg.connect(_conninfo(dsn))


def connect_dicts(dsn: str) -> psycopg.Connection[dict[str, Any]]:
    return psycopg.Connection[dict[str, Any]].connect(_conninfo(dsn), row_factory=dict_row)


def describe_error(error: Exception) -> str:
    if isinstance(error, psycopg.Error):
        text = error.diag.message_primary or (str(error).splitlines() or [""])[0]
        message = f"{type(error).__name__}: {text}"
    elif isinstance(error, ValidationError):
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        message = f"{where}: {first['msg']}" if where else str(first["msg"])
    else:
        message = (str(error).splitlines() or [""])[0] or type(error).__name__
    return message[:MAX_MESSAGE_LENGTH]
