"""The CLI operations runbook's example commands are ones the CLI accepts (M3-08, M3-09):
each `agentic ...` line in its text blocks parses (`--help` after it changes nothing)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main

RUNBOOK = Path(__file__).resolve().parents[2] / "docs" / "runbooks" / "cli-operations.md"


def example_commands() -> list[str]:
    blocks = re.findall(r"```text\n(.*?)```", RUNBOOK.read_text("utf-8"), flags=re.S)
    lines = (line.split("#", 1)[0].strip() for block in blocks for line in block.splitlines())
    return [line for line in lines if line.startswith("agentic ")]


def test_the_runbook_has_examples():
    assert len(example_commands()) >= 5


@pytest.mark.parametrize("command", example_commands())
def test_each_runbook_example_parses(home: Path, command: str, capsys):
    code = main([*command.split()[1:], "--help"])
    assert code == ExitCode.OK, capsys.readouterr().out
