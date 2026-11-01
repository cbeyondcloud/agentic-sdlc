"""The command reference documents every `agentic` command, and nothing else (M3-18): a
command added without a row, or a row left behind by a removed or renamed command, fails
here, so the docs cannot drift from the CLI."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import typer

from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import build_app, main

REFERENCE = Path(__file__).resolve().parents[2] / "docs" / "guides" / "command-reference.md"


def cli_commands() -> set[str]:
    """Every command a person can run, as `agentic <group> <command>` (groups excluded)."""
    found: set[str] = set()

    def walk(command, path: list[str]) -> None:
        children = getattr(command, "commands", None)
        if children is None:
            found.add(" ".join(path))
            return
        for name, child in children.items():
            walk(child, [*path, name])

    walk(typer.main.get_command(build_app()), ["agentic"])
    return found


def documented() -> set[str]:
    """The first cell of each reference table row that names an `agentic` command."""
    rows = re.findall(
        r"^\|\s*`(agentic [a-z][a-z0-9 -]*)`\s*\|", REFERENCE.read_text("utf-8"), re.M
    )
    return {row.strip() for row in rows}


def test_the_walk_finds_the_commands():
    assert {"agentic run", "agentic skills new", "agentic eval calibrate score"} <= cli_commands()


def test_every_command_has_a_reference_row():
    missing = sorted(cli_commands() - documented())
    assert not missing, f"add a row to {REFERENCE.name} for: {missing}"


def test_every_reference_row_is_a_command():
    unknown = sorted(documented() - cli_commands())
    assert not unknown, f"{REFERENCE.name} lists commands the CLI does not have: {unknown}"


ROOT = REFERENCE.parents[2]
GUIDES = [ROOT / "README.md", *sorted((ROOT / "docs" / "guides").glob("*.md"))]


def guide_examples() -> list[tuple[str, str]]:
    """Each `agentic ...` line in a fenced code block of the README and the guides."""
    found = []
    for page in GUIDES:
        blocks = re.findall(r"```[a-z]*\n(.*?)```", page.read_text("utf-8"), flags=re.S)
        for line in (line.strip() for block in blocks for line in block.splitlines()):
            if line.startswith("agentic "):
                found.append((page.name, line))
    return found


def test_the_guides_have_examples():
    assert len(guide_examples()) >= 30


@pytest.mark.parametrize(("page", "command"), guide_examples())
def test_each_guide_example_parses(home: Path, page: str, command: str, capsys):
    """An example with a wrong command or flag fails here (`--help` after it runs nothing)."""
    words = shlex.split(command, comments=True)
    code = main([*words[1:], "--help"])
    assert code == ExitCode.OK, f"{page}: {command}\n{capsys.readouterr().err}"
