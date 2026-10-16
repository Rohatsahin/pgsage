import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


MARKDOWN = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    *sorted((ROOT / "docs").glob("*.md")),
]


LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


SRC_REFERENCE = re.compile(r"`((?:src|tests|scripts|docs)/[\w./-]+)`")


def test_every_markdown_file_exists() -> None:
    assert all(path.exists() for path in MARKDOWN)


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: p.name)
def test_relative_links_resolve(path: Path) -> None:
    for target in LINK.findall(path.read_text()):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        assert (path.parent / target).exists(), f"{path.name} links to missing {target}"


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: p.name)
def test_referenced_repository_paths_exist(path: Path) -> None:
    for reference in SRC_REFERENCE.findall(path.read_text()):
        if "*" in reference or "<" in reference or reference.endswith("/"):
            continue
        assert (ROOT / reference).exists(), f"{path.name} mentions missing {reference}"


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: p.name)
def test_every_test_file_a_doc_mentions_exists(path: Path) -> None:
    for name in set(re.findall(r"\b(test_[a-z_]+\.py)\b", path.read_text())):
        assert (ROOT / "tests" / name).exists(), f"{path.name} mentions missing tests/{name}"


def test_readme_shows_the_logo_and_it_exists() -> None:
    assert "assets/logo.svg" in (ROOT / "README.md").read_text()
    svg = (ROOT / "assets" / "logo.svg").read_text()
    assert svg.startswith("<svg") and "<title" in svg


COMMANDS = ("inspect", "summarize", "verify", "top", "connections", "init", "install-skill")


def test_readme_command_table_matches_the_real_cli() -> None:
    from typer.testing import CliRunner

    from pgsage.cli import app

    help_text = CliRunner().invoke(app, ["--help"]).output
    readme = (ROOT / "README.md").read_text()
    for command in COMMANDS:
        assert command in help_text
        assert f"`pgsage {command}" in readme
    for gone in ("tune", "eval", "explain"):
        assert f"`pgsage {gone}" not in readme


def test_documented_mcp_tools_match_the_server() -> None:
    real = mcp_tool_names()
    documented = set(re.findall(r"^\| `(\w+)` \|", (ROOT / "docs" / "mcp.md").read_text(), re.M))
    assert documented == real
    readme = (ROOT / "README.md").read_text()
    assert all(name in readme for name in real)


def mcp_tool_names() -> set[str]:
    import asyncio

    from mcp import Client

    from pgsage.mcp_server import server

    async def names() -> set[str]:
        async with Client(server) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    return asyncio.run(names())


SKILL = ROOT / "src" / "pgsage" / "skill" / "SKILL.md"


def skill_parts() -> tuple[dict[str, str], str]:
    text = SKILL.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    assert match, "SKILL.md must start with YAML frontmatter"
    fields = dict(re.findall(r"^(\w+): (.+)$", match.group(1), re.M))
    return fields, match.group(2)


def test_skill_has_frontmatter_with_a_name_and_a_triggering_description() -> None:
    fields, _ = skill_parts()
    assert fields["name"] == "pgsage"
    assert len(fields["description"]) > 80
    assert "slow" in fields["description"].lower()


def test_skill_mentions_every_mcp_tool_and_every_cli_command() -> None:
    _, body = skill_parts()
    for name in mcp_tool_names():
        assert f"`{name}`" in body, f"SKILL.md does not mention {name}"
    for command in (
        "pgsage inspect",
        "pgsage verify index",
        "pgsage verify statistics",
        "pgsage verify setting",
        "pgsage verify rewrite",
        "pgsage top",
        "pgsage connections",
        "pgsage init",
    ):
        assert command in body, f"SKILL.md does not mention {command}"


def test_skill_tells_the_agent_when_a_change_counts_as_proven() -> None:
    _, body = skill_parts()
    assert "improved: true" in body
    assert "1.1x" in body and "0.5 ms" in body


def test_skill_has_no_company_references() -> None:
    text = SKILL.read_text(encoding="utf-8").lower()
    for word in ("tempo", "nexus", "flyway"):
        assert not re.search(rf"\b{word}\b", text)


def test_skill_is_packaged_in_the_wheel_layout() -> None:
    from importlib.resources import files

    assert (files("pgsage") / "skill" / "SKILL.md").is_file()


def test_the_readme_uses_absolute_links_that_point_at_real_files() -> None:
    readme = (ROOT / "README.md").read_text()
    assert 'src="assets' not in readme
    blob = "https://github.com/Rohatsahin/pgsage/blob/main/"
    raw = "https://raw.githubusercontent.com/Rohatsahin/pgsage/main/"
    for target in LINK.findall(readme):
        assert target.startswith(("http://", "https://", "mailto:")), target
        if target.startswith(blob):
            assert (ROOT / target.removeprefix(blob)).exists(), target
    for logo in re.findall(r'src="(' + re.escape(raw) + r'[^"]+)"', readme):
        assert (ROOT / logo.removeprefix(raw)).exists(), logo
