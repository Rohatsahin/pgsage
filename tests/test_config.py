import json
import os
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pgsage.cli import app
from pgsage.config import ConfigError, load_config, resolve_target, user_config_path

runner = CliRunner()


def write_project(directory: Path, body: str) -> Path:
    path = directory / "pgsage.toml"
    path.write_text(body)
    return path


def write_user(body: str) -> Path:
    path = user_config_path()
    path.parent.mkdir(parents=True)
    path.write_text(body)
    return path


def test_project_file_is_found_in_a_parent_directory(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    write_project(tmp_path, '[connections.a]\nhost = "localhost"\n')
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    assert "a" in load_config(nested).connections


def test_project_connections_add_to_the_user_config_and_inherit_its_default(
    tmp_path: Path,
) -> None:
    write_user('default_connection = "one"\n[connections.one]\nhost = "user-host"\n')
    write_project(tmp_path, '[connections.two]\nhost = "localhost"\n')
    config = load_config(tmp_path)
    assert set(config.connections) == {"one", "two"}
    assert config.default_connection == "one"
    assert len(config.files) == 2


def test_a_project_file_cannot_redefine_a_user_connection_to_drop_protection(
    tmp_path: Path,
) -> None:
    write_user('[connections.prod]\nhost = "prod.example"\nprotected = true\n')
    write_project(tmp_path, '[connections.prod]\nhost = "localhost"\n')
    with pytest.raises(ConfigError, match="already defined in your user config"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        '[connections.a]\nhost = "evil.example"\npassword_env = "AWS_SECRET_ACCESS_KEY"\n',
        '[connections.a]\nhost = "${SECRET_TOKEN}.example"\n',
        '[connections.a]\ndsn = "postgresql://u:${DB_PASSWORD}@h/d"\n',
    ],
)
def test_a_project_file_cannot_read_arbitrary_environment_variables(
    tmp_path: Path, body: str
) -> None:
    write_project(tmp_path, body)
    with pytest.raises(ConfigError, match="start with PGSAGE_"):
        load_config(tmp_path)


def test_the_user_config_may_read_any_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_user('[connections.a]\nhost = "h"\npassword_env = "DB_PASSWORD"\n')
    monkeypatch.setenv("DB_PASSWORD", "pw")
    assert "password=pw" in load_config().connections["a"].conninfo()


def test_explicit_config_env_replaces_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_project(tmp_path, '[connections.project]\nhost = "localhost"\n')
    explicit = tmp_path / "other.toml"
    explicit.write_text('[connections.explicit]\nhost = "y"\n')
    monkeypatch.setenv("PGSAGE_CONFIG", str(explicit))
    assert set(load_config(tmp_path).connections) == {"explicit"}
    monkeypatch.setenv("PGSAGE_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[connections.a]\npassword = "hunter2"\n', "never in the file"),
        ('[connections.a]\nhots = "typo"\n', "unknown key hots"),
        ('[connections.a]\ndsn = "postgresql://h/d"\nhost = "h"\n', "either dsn or separate"),
        ('[connections.a]\npassword_env = "X"\npassword_command = ["x"]\n', "not both"),
        ('[connections.a]\nprotected = "yes"\n', "true or false"),
        ("[connections.a]\nport = true\n", "string or number"),
        ("connections = 3\n", "must be a table"),
        ("surprise = 1\n", "unknown key surprise"),
        ("[defaults]\nmodel = 'x'\n", "unknown key defaults"),
        ("not toml [", "invalid TOML"),
    ],
)
def test_invalid_config_is_rejected_with_a_precise_message(
    tmp_path: Path, body: str, message: str
) -> None:
    write_project(tmp_path, body)
    with pytest.raises(ConfigError, match=message) as error:
        load_config(tmp_path)
    assert "hunter2" not in str(error.value)


def test_password_command_is_refused_in_a_project_file(tmp_path: Path) -> None:
    write_project(tmp_path, '[connections.a]\nhost = "h"\npassword_command = ["echo", "x"]\n')
    with pytest.raises(ConfigError, match="only allowed in your user config"):
        load_config(tmp_path)


def test_password_command_runs_without_a_shell_in_the_user_config() -> None:
    write_user(
        '[connections.a]\nhost = "db.internal"\ndbname = "app"\nuser = "u"\n'
        f'password_command = ["{Path(sys.executable).as_posix()}", "-c", "print(\'s3cret\')"]\n'
    )
    connection = load_config().connections["a"]
    assert "password=s3cret" in connection.conninfo()
    assert "s3cret" not in connection.describe()


def test_password_command_failures_never_echo_output() -> None:
    script = "import sys; print('leaky-secret'); sys.exit(3)"
    write_user(
        f'[connections.a]\nhost = "h"\npassword_command = ["{Path(sys.executable).as_posix()}", "-c", "{script}"]\n'
    )
    with pytest.raises(ConfigError, match="exited with 3") as error:
        load_config().connections["a"].conninfo()
    assert "leaky-secret" not in str(error.value)


def test_password_command_missing_program_and_empty_output() -> None:
    write_user('[connections.a]\nhost = "h"\npassword_command = ["no-such-program-pgsage"]\n')
    with pytest.raises(ConfigError, match="not found"):
        load_config().connections["a"].conninfo()
    write_user_again = user_config_path()
    write_user_again.write_text(
        f'[connections.a]\nhost = "h"\npassword_command = ["{Path(sys.executable).as_posix()}", "-c", "pass"]\n'
    )
    with pytest.raises(ConfigError, match="printed nothing"):
        load_config().connections["a"].conninfo()


def test_password_command_must_be_a_list_of_strings() -> None:
    write_user('[connections.a]\nhost = "h"\npassword_command = "op read x"\n')
    with pytest.raises(ConfigError, match="must be a list"):
        load_config()


def test_password_env_is_read_at_connect_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_project(
        tmp_path, '[connections.a]\nhost = "localhost"\nuser = "u"\npassword_env = "PGSAGE_DB_PW"\n'
    )
    connection = load_config(tmp_path).connections["a"]
    with pytest.raises(ConfigError, match="PGSAGE_DB_PW is not set"):
        connection.conninfo()
    monkeypatch.setenv("PGSAGE_DB_PW", "pw 1")
    info = connection.conninfo()
    assert "host=localhost" in info and "user=u" in info and "'pw 1'" in info
    assert "PGSAGE_DB_PW" not in connection.describe()


def test_dsn_expands_environment_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_project(tmp_path, '[connections.ci]\ndsn = "${PGSAGE_DATABASE_URL}"\n')
    connection = load_config(tmp_path).connections["ci"]
    with pytest.raises(ConfigError, match="PGSAGE_DATABASE_URL is not set"):
        connection.conninfo()
    monkeypatch.setenv("PGSAGE_DATABASE_URL", "postgresql://svc:pw@localhost:6543/appdb")
    assert connection.conninfo() == "postgresql://svc:pw@localhost:6543/appdb"
    assert connection.describe() == "dsn (from the environment)"


def test_a_project_dsn_that_expands_to_a_remote_host_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_project(tmp_path, '[connections.ci]\ndsn = "${PGSAGE_DATABASE_URL}"\n')
    monkeypatch.setenv("PGSAGE_DATABASE_URL", "postgresql://svc:pw@evil.example:6543/appdb")
    with pytest.raises(ConfigError, match="may only name a local host"):
        load_config(tmp_path).connections["ci"].conninfo()
    monkeypatch.setenv("PGSAGE_DATABASE_URL", "postgresql://svc:pw@localhost/appdb")
    assert load_config(tmp_path).connections["ci"].conninfo()


def test_the_user_config_may_name_any_host_and_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_user(
        '[connections.remote]\nhost = "db.internal"\noptions = "-c search_path=app"\n'
        '[connections.svc]\nservice = "staging_clone"\n'
    )
    config = load_config()
    assert "db.internal" in config.connections["remote"].conninfo()
    assert config.connections["svc"].describe() == "service staging_clone"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[connections.a]\nhost = "db.example.com"\n', "may only name a local host"),
        ('[connections.a]\ndsn = "postgresql://u@db.example.com/d"\n', "may only name a local"),
        ('[connections.a]\ndsn = "postgresql://u:secret@localhost/d"\n', "password inside dsn"),
        ('[connections.a]\nservice = "staging"\n', "service is not allowed in a project file"),
        ('[connections.a]\nhost = "localhost"\noptions = "-c x=1"\n', "options is not allowed"),
        ('[connections.a]\nuser = "u"\n', None),
    ],
)
def test_a_project_file_cannot_choose_where_credentials_go(
    tmp_path: Path, body: str, message: str | None
) -> None:
    write_project(tmp_path, body)
    if message is None:
        connection = load_config(tmp_path).connections["a"]
        with pytest.raises(ConfigError, match="may only name a local host"):
            connection.conninfo()
        return
    with pytest.raises(ConfigError, match=message) as error:
        load_config(tmp_path)
    assert "secret" not in str(error.value)


def test_a_project_default_connection_never_overrides_the_users(tmp_path: Path) -> None:
    write_user('default_connection = "mine"\n[connections.mine]\nhost = "db.internal"\n')
    write_project(
        tmp_path, 'default_connection = "theirs"\n[connections.theirs]\nhost = "localhost"\n'
    )
    assert load_config(tmp_path).default_connection == "mine"


def test_a_project_default_connection_applies_when_the_user_has_none(tmp_path: Path) -> None:
    write_project(
        tmp_path, 'default_connection = "theirs"\n[connections.theirs]\nhost = "localhost"\n'
    )
    assert load_config(tmp_path).default_connection == "theirs"


def test_a_dsn_label_never_shows_expanded_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_user('[connections.x]\ndsn = "host=localhost user=${HUNTER_SECRET} dbname=scratch"\n')
    monkeypatch.setenv("HUNTER_SECRET", "hunter2")
    assert "hunter2" not in load_config().connections["x"].describe()


def test_the_project_search_stops_at_the_repository_root_and_skips_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    above = tmp_path / "above"
    repo = above / "repo"
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    write_project(above, '[connections.stray]\nhost = "localhost"\n')
    assert load_config(nested).connections == {}
    write_project(repo, '[connections.inside]\nhost = "localhost"\n')
    assert set(load_config(nested).connections) == {"inside"}
    plain = tmp_path / "plain" / "child"
    plain.mkdir(parents=True)
    write_project(plain.parent, '[connections.parent]\nhost = "localhost"\n')
    assert load_config(plain).connections == {}
    monkeypatch.setattr(Path, "home", lambda: repo)
    assert load_config(nested).connections == {}


def test_service_connections_describe_without_a_host(tmp_path: Path) -> None:
    write_user('[connections.s]\nservice = "staging_clone"\n')
    assert load_config().connections["s"].describe() == "service staging_clone"


def test_bad_values_surface_as_config_errors(tmp_path: Path) -> None:
    write_user('[connections.a]\ndsn = "this is ==="\n')
    with pytest.raises(ConfigError, match="connection 'a'"):
        load_config().connections["a"].conninfo()


def test_resolution_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_project(
        tmp_path,
        'default_connection = "main"\n[connections.main]\nhost = "localhost"\n'
        '[connections.prod]\nhost = "127.0.0.1"\nprotected = true\n',
    )
    monkeypatch.chdir(tmp_path)
    assert resolve_target("postgresql://explicit/db", None).conninfo == "postgresql://explicit/db"
    assert resolve_target(None, None).name == "main"
    chosen = resolve_target(None, "prod")
    assert chosen.protected and chosen.name == "prod"
    with pytest.raises(ConfigError, match="either --dsn or --connection"):
        resolve_target("postgresql://x/d", "main")
    with pytest.raises(ConfigError, match=r"no connection named 'nope' \(known: main, prod\)"):
        resolve_target(None, "nope")


def test_libpq_environment_is_the_last_resort(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="pgsage init"):
        resolve_target(None, None)
    monkeypatch.setenv("PGHOST", "somewhere")
    target = resolve_target(None, None)
    assert target.conninfo == "" and "PG*" in target.label


def test_init_writes_a_private_starter_file_once() -> None:
    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0
    path = Path.cwd() / "pgsage.toml"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_config().default_connection == "scratch"
    again = runner.invoke(app, ["init"])
    assert again.exit_code == 2 and "already exists" in again.output


def test_init_user_writes_the_user_config() -> None:
    assert runner.invoke(app, ["init", "--user"]).exit_code == 0
    assert user_config_path().is_file()


def test_connections_command_lists_names_flags_and_sources() -> None:
    assert json.loads(runner.invoke(app, ["connections"]).stdout) == []
    write_project(
        Path.cwd(),
        'default_connection = "a"\n[connections.a]\nhost = "localhost"\nuser = "u"\ndbname = "d"\n'
        '[connections.b]\nhost = "127.0.0.1"\nprotected = true\n',
    )
    first, second = json.loads(runner.invoke(app, ["connections"]).stdout)
    assert (first["name"], first["target"], first["default"]) == ("a", "u@localhost:5432/d", True)
    assert (second["name"], second["target"], second["protected"]) == (
        "b",
        "?@127.0.0.1:5432/?",
        True,
    )
    assert first["source"].endswith("pgsage.toml")


def test_connections_command_reports_a_broken_config() -> None:
    write_project(Path.cwd(), "oops = 1\n")
    result = runner.invoke(app, ["connections"])
    assert result.exit_code == 2 and "unknown key oops" in result.output


def test_protection_is_reported_before_any_secret_is_fetched() -> None:
    marker = Path.cwd() / "ran"
    write_user(
        '[connections.prod]\nhost = "h"\nprotected = true\n'
        f'password_command = ["{Path(sys.executable).as_posix()}", "-c", '
        f"\"open(r'{marker}', 'w')\"]\n"
    )
    result = runner.invoke(
        app,
        ["verify", "index", "-c", "prod", "--table", "t", "--columns", "a", "--sql", "SELECT 1"],
    )
    assert result.exit_code == 2 and "is protected" in result.output
    assert not marker.exists()
