"""The training material (M3-23, docs/training/): every `agentic` example in it is a real
command with real flags whose arguments the CLI's own parser accepts, and the deck's
sections add up to its 30 minutes."""

import re
import shlex
from pathlib import Path

import pytest
import typer
from typer.core import TyperGroup

from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import build_app, main

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


def parse(words: list[str]):
    """`words` through the CLI's click parser down to the command, as `main` would parse
    them, without running anything: a missing argument, an unknown or misspelt option, a
    value of the wrong type or an option given to the wrong command fails (DF-997)."""
    ctx = typer.main.get_command(build_app()).make_context("agentic", list(words))
    while isinstance(ctx.command, TyperGroup):
        rest = [*ctx._protected_args, *ctx.args]
        assert rest, f"{' '.join(words)!r} names a group, not a command"
        name, command, rest = ctx.command.resolve_command(ctx, rest)
        ctx = command.make_context(name, rest, parent=ctx)
    return ctx


@pytest.mark.parametrize(("page", "command"), examples())
def test_each_training_example_parses_with_its_real_arguments(home: Path, page, command):
    words = shlex.split(command, comments=True)[1:]
    try:
        parse(words)
    except typer.TyperException as error:
        pytest.fail(f"{page}: {command}\n{error}")


@pytest.mark.parametrize(
    "words",
    [
        ["resume", "r1", "--extra-minutes", "ten"],  # a value of the wrong type
        ["resume", "r1", "--extra-minutes", "-5"],  # a value out of range
        ["onboarding", "now"],  # an extra argument
    ],
)
def test_the_parser_check_catches_what_help_does_not(home: Path, words):
    assert main([*words, "--help"]) == ExitCode.OK  # --help stops before these are checked
    with pytest.raises(typer.TyperException):
        parse(words)


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
