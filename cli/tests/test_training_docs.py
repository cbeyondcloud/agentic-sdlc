"""The training material (M3-23, docs/training/): every `agentic` example in it is a real
command with real flags, and the deck's sections add up to its 30 minutes."""

import re
import shlex
from pathlib import Path

import pytest

from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main

DOCS = Path(__file__).resolve().parents[2] / "docs"
PAGES = [DOCS / "index.md", *sorted((DOCS / "training").glob("*.md"))]
DECK = DOCS / "training" / "deck.md"


def examples() -> list[tuple[str, str]]:
    found = []
    for page in PAGES:
        # shell blocks only: a ```text block is a template people fill in
        blocks = re.findall(r"```(?:bash|sh)?\n(.*?)```", page.read_text("utf-8"), flags=re.S)
        for line in (line.strip() for block in blocks for line in block.splitlines()):
            if line.startswith("agentic "):
                found.append((page.name, line))
    return found


def test_the_training_pages_have_examples():
    assert len(examples()) >= 20


@pytest.mark.parametrize(("page", "command"), examples())
def test_each_training_example_parses(home: Path, page: str, command: str, capsys):
    words = shlex.split(command, comments=True)
    code = main([*words[1:], "--help"])
    assert code == ExitCode.OK, f"{page}: {command}\n{capsys.readouterr().err}"


def agenda() -> list[tuple[str, int]]:
    text = DECK.read_text("utf-8")
    table = text.split("## Agenda", 1)[1].split("\n---", 1)[0]
    rows = re.findall(r"^\| (\d+) \| ([^|]+?) \| (\d+) \|$", table, flags=re.M)
    return [(f"{n}. {title}", int(minutes)) for n, title, minutes in rows]


def test_the_deck_takes_30_minutes():
    sections = agenda()
    assert len(sections) == 11
    assert sum(minutes for _, minutes in sections) == 30
    total = re.search(r"\*\*Total\*\* \| \*\*(\d+)\*\*", DECK.read_text("utf-8"))
    assert total and int(total.group(1)) == 30


def test_each_agenda_section_is_a_slide_with_its_time():
    text = DECK.read_text("utf-8")
    for title, minutes in agenda():
        assert f"## {title} ({minutes} min)" in text, title


def test_the_deck_is_marp_markdown():
    text = DECK.read_text("utf-8")
    assert text.startswith("---\nmarp: true\n")
    assert text.count("\n---\n") >= 12  # a slide per section, the title and the agenda
