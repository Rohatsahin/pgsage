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


def test_readme_shows_the_logo_and_it_exists() -> None:
    assert "assets/logo.svg" in (ROOT / "README.md").read_text()
    svg = (ROOT / "assets" / "logo.svg").read_text()
    assert svg.startswith("<svg") and "<title" in svg


def test_readme_command_table_matches_the_real_cli() -> None:
    from typer.testing import CliRunner

    from pgsage.cli import app

    help_text = CliRunner().invoke(app, ["--help"]).output
    readme = (ROOT / "README.md").read_text()
    for command in ("explain", "tune", "verify", "top", "diff", "eval"):
        assert command in help_text
        assert f"`pgsage {command}" in readme


def test_documented_mcp_tools_match_the_server() -> None:
    import asyncio

    from mcp import Client

    from pgsage.mcp_server import server

    async def names() -> set[str]:
        async with Client(server) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    real = asyncio.run(names())
    documented = set(re.findall(r"^\| `(\w+)` \|", (ROOT / "docs" / "mcp.md").read_text(), re.M))
    assert documented == real
    readme = (ROOT / "README.md").read_text()
    assert all(name in readme for name in real)
