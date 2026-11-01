"""The command reference documents every `python -m agentic_cp` operator command, and
nothing else (M3-18), so the admin docs cannot drift from the parser."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from agentic_cp.__main__ import build_parser

PREFIX = "python -m agentic_cp"
REFERENCE = Path(__file__).resolve().parents[2] / "docs" / "guides" / "command-reference.md"


def parser_commands() -> set[str]:
    """Every runnable command path (sub-command groups excluded)."""
    found: set[str] = set()

    def walk(parser: argparse.ArgumentParser, path: list[str]) -> None:
        groups = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        if not groups:
            found.add(" ".join(path))
            return
        for group in groups:
            for name, child in group.choices.items():
                walk(child, [*path, name])

    walk(build_parser(), [PREFIX])
    return found


def documented() -> set[str]:
    pattern = rf"^\|\s*`({re.escape(PREFIX)} [a-z][a-z0-9 -]*)`\s*\|"
    return {row.strip() for row in re.findall(pattern, REFERENCE.read_text("utf-8"), re.M)}


def test_the_walk_finds_the_commands():
    assert {f"{PREFIX} serve", f"{PREFIX} admin tenant create"} <= parser_commands()


def test_every_operator_command_has_a_reference_row():
    missing = sorted(parser_commands() - documented())
    assert not missing, f"add a row to {REFERENCE.name} for: {missing}"


def test_every_reference_row_is_an_operator_command():
    unknown = sorted(documented() - parser_commands())
    assert not unknown, f"{REFERENCE.name} lists commands agentic_cp does not have: {unknown}"
