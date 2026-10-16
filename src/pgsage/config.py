from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from psycopg.conninfo import conninfo_to_dict, make_conninfo

PROJECT_FILE = "pgsage.toml"
CONFIG_ENV = "PGSAGE_CONFIG"
CONNECTION_ENV = "PGSAGE_CONNECTION"
DSN_ENV = "PGSAGE_DSN"
LIBPQ_ENV = ("PGSERVICE", "PGHOST", "PGHOSTADDR", "PGDATABASE", "PGUSER")
PASSWORD_COMMAND_TIMEOUT = 30

CONNECTION_STRINGS = (
    "dsn",
    "service",
    "host",
    "port",
    "dbname",
    "user",
    "sslmode",
    "sslrootcert",
    "sslcert",
    "sslkey",
    "application_name",
    "connect_timeout",
    "options",
    "password_env",
    "description",
)
CONNECTION_KEYS = (*CONNECTION_STRINGS, "password_command", "protected")
LIBPQ_PARAMS = tuple(
    key for key in CONNECTION_STRINGS if key not in ("dsn", "password_env", "description")
)
TOP_LEVEL_KEYS = ("default_connection", "connections")
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
PROJECT_ENV_PREFIX = "PGSAGE_"

TEMPLATE = """\
default_connection = "scratch"

[connections.scratch]
description = "local scratch database"
host = "localhost"
port = 5432
dbname = "app"
user = "app"
password_env = "PGSAGE_SCRATCH_PASSWORD"

# [connections.ci]
# dsn = "${DATABASE_URL}"

# [connections.staging-clone]
# service = "staging_clone"
# protected = true
"""


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Connection:
    name: str
    values: dict[str, str]
    password_command: tuple[str, ...] | None
    protected: bool
    source: Path

    def conninfo(self) -> str:
        values = dict(self.values)
        dsn = _expand(values.pop("dsn", ""), self.name)
        values.pop("description", None)
        password_env = values.pop("password_env", None)
        extra = {key: _expand(value, self.name) for key, value in values.items()}
        password = self._password(password_env)
        if password is not None:
            extra["password"] = password
        try:
            return make_conninfo(dsn, **extra)
        except Exception as error:
            raise ConfigError(
                f"connection {self.name!r}: invalid connection settings (values not shown)"
            ) from error

    def _password(self, password_env: str | None) -> str | None:
        if password_env:
            password = os.environ.get(password_env)
            if password is None:
                raise ConfigError(
                    f"connection {self.name!r}: environment variable {password_env} is not set"
                )
            return password
        if self.password_command:
            return _run_password_command(self.name, self.password_command)
        return None

    def describe(self) -> str:
        values = {**self.values}
        info: dict[str, Any] = {}
        if values.get("dsn"):
            try:
                info = conninfo_to_dict(_expand(values["dsn"], self.name))
            except Exception:
                info = {}
        info.update(
            {key: values[key] for key in ("host", "port", "dbname", "user") if key in values}
        )
        if "service" in values and not info.get("host"):
            return f"service {values['service']}"
        user = info.get("user") or "?"
        host = info.get("host") or "?"
        port = info.get("port") or 5432
        return f"{user}@{host}:{port}/{info.get('dbname') or '?'}"


@dataclass(frozen=True)
class Config:
    connections: dict[str, Connection] = field(default_factory=lambda: {})
    default_connection: str | None = None
    files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class Target:
    conninfo: str
    label: str
    protected: bool = False
    name: str | None = None


def _expand(value: str, owner: str) -> str:
    def replace(match: re.Match[str]) -> str:
        variable = match.group(1)
        found = os.environ.get(variable)
        if found is None:
            raise ConfigError(f"connection {owner!r}: environment variable {variable} is not set")
        return found

    return VARIABLE.sub(replace, value)


def _run_password_command(name: str, argv: tuple[str, ...]) -> str:
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PASSWORD_COMMAND_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as error:
        raise ConfigError(f"connection {name!r}: password_command {argv[0]!r} not found") from error
    except OSError as error:
        raise ConfigError(
            f"connection {name!r}: password_command {argv[0]!r} cannot run ({error.strerror})"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise ConfigError(
            f"connection {name!r}: password_command timed out after {PASSWORD_COMMAND_TIMEOUT}s"
        ) from error
    if done.returncode != 0:
        raise ConfigError(
            f"connection {name!r}: password_command {argv[0]!r} exited with {done.returncode}"
        )
    password = done.stdout.rstrip("\r\n")
    if not password:
        raise ConfigError(f"connection {name!r}: password_command printed nothing")
    return password


def user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "pgsage" / "config.toml"


def find_project_file(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / PROJECT_FILE
        if candidate.is_file():
            return candidate
    return None


def _read(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: invalid TOML: {error}") from error
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error.strerror or error}") from error


def _check_keys(where: str, found: dict[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(found) - set(allowed))
    if unknown:
        hint = " (put passwords in password_env or password_command, never in the file)"
        extra = hint if "password" in unknown else ""
        raise ConfigError(
            f"{where}: unknown key {', '.join(unknown)}; allowed: {', '.join(allowed)}{extra}"
        )


def _as_table(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a table")
    return {str(k): v for k, v in cast("dict[Any, Any]", value).items()}


def _check_project_env(where: str, values: dict[str, str]) -> None:
    used = [values["password_env"]] if "password_env" in values else []
    for value in values.values():
        used.extend(VARIABLE.findall(value))
    for variable in used:
        if not variable.startswith(PROJECT_ENV_PREFIX):
            raise ConfigError(
                f"{where}: a project file may only read environment variables that start with "
                f"{PROJECT_ENV_PREFIX} (not {variable}); define the connection in your user config "
                f"({user_config_path()}) to use it"
            )


def _connection(name: str, raw: object, path: Path, trusted: bool) -> Connection:
    where = f"{path}: connections.{name}"
    table = _as_table(raw, where)
    _check_keys(where, table, CONNECTION_KEYS)
    values: dict[str, str] = {}
    for key in CONNECTION_STRINGS:
        if key in table:
            value = table[key]
            if isinstance(value, bool) or not isinstance(value, str | int):
                raise ConfigError(f"{where}.{key} must be a string or number")
            values[key] = str(value)
    if "dsn" in values and any(key in values for key in LIBPQ_PARAMS):
        raise ConfigError(f"{where}: use either dsn or separate fields, not both")
    if "password_env" in values and "password_command" in table:
        raise ConfigError(f"{where}: use password_env or password_command, not both")
    command = table.get("password_command")
    parsed: tuple[str, ...] | None = None
    if command is not None:
        if not trusted:
            raise ConfigError(
                f"{where}: password_command runs a program, so it is only allowed in your "
                f"user config ({user_config_path()}), not in a project file"
            )
        parts = cast("list[Any]", command) if isinstance(command, list) else []
        if not parts or not all(isinstance(part, str) for part in parts):
            raise ConfigError(f'{where}.password_command must be a list like ["op", "read", "..."]')
        parsed = tuple(str(part) for part in parts)
    if not trusted:
        _check_project_env(where, values)
    protected = table.get("protected", False)
    if not isinstance(protected, bool):
        raise ConfigError(f"{where}.protected must be true or false")
    return Connection(name, values, parsed, protected, path)


def _load_file(path: Path, trusted: bool) -> Config:
    raw = _read(path)
    _check_keys(str(path), raw, TOP_LEVEL_KEYS)
    section = _as_table(raw.get("connections", {}), f"{path}: connections")
    connections = {name: _connection(name, body, path, trusted) for name, body in section.items()}
    default = raw.get("default_connection")
    if default is not None and not isinstance(default, str):
        raise ConfigError(f"{path}: default_connection must be a string")
    return Config(connections, default, (path,))


def _merge(base: Config, over: Config) -> Config:
    return Config(
        {**base.connections, **over.connections},
        over.default_connection or base.default_connection,
        (*base.files, *over.files),
    )


def load_config(cwd: Path | None = None) -> Config:
    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"{CONFIG_ENV} points to {path}, which does not exist")
        return _load_file(path, trusted=True)
    config = Config()
    user = user_config_path()
    if user.is_file():
        config = _load_file(user, trusted=True)
    project = find_project_file((cwd or Path.cwd()).resolve())
    if project is not None and project != user:
        loaded = _load_file(project, trusted=False)
        shadowed = sorted(set(config.connections) & set(loaded.connections))
        if shadowed:
            raise ConfigError(
                f"{project}: connection(s) {', '.join(shadowed)} are already defined in your "
                "user config; a project file cannot redefine them, so rename them"
            )
        config = _merge(config, loaded)
    return config


def resolve_target(
    dsn: str | None,
    connection: str | None,
    writes: bool = False,
    config: Config | None = None,
) -> Target:
    if dsn and connection:
        raise ConfigError("use either --dsn or --connection, not both")
    dsn = dsn or (None if connection else os.environ.get(DSN_ENV))
    connection = connection or (None if dsn else os.environ.get(CONNECTION_ENV))
    if dsn:
        return Target(dsn, "the connection string you gave")
    config = config or load_config()
    name = connection or config.default_connection
    if name:
        found = config.connections.get(name)
        if found is None:
            known = ", ".join(sorted(config.connections)) or "none defined"
            raise ConfigError(f"no connection named {name!r} (known: {known})")
        if writes and found.protected:
            raise ConfigError(
                f"connection {name!r} is protected: proofs create indexes and run your query. "
                "Point pgsage at a scratch clone"
            )
        return Target(found.conninfo(), found.describe(), found.protected, name)
    if any(os.environ.get(variable) for variable in LIBPQ_ENV):
        return Target("", "the PG* environment variables")
    raise ConfigError(
        "no database to connect to: pass --connection NAME or --dsn, set PGSAGE_DSN or "
        "PGSAGE_CONNECTION, use the PG* environment variables, or run `pgsage init`"
    )
