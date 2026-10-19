"""Regression tests for the SP8 final review (Sonnet 5.5) of the guard's shell policy:
each pins one finding (C1, I1, I2)."""

from pathlib import Path

import pytest

from sdlc_commons.guards import Guard

RUN = "r-review-1"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path  # command checks need no repository content


# ── C1: text the allow list cannot read is refused, never waved through ──────


@pytest.mark.parametrize(
    "command",
    [
        'echo "`touch p1`"',
        'echo "$(touch p2)"',
        "echo ok #'\ntouch p3\n#'",
        'X="$(touch p4)" pytest',
        'echo "${x:-$(touch p5)}"',
        'pytest "$(touch p6)"',
        'echo "$(echo \'"\'; touch p7)"',
        "pytest <(touch p8)",
    ],
)
def test_substitution_and_comments_are_refused_by_the_allow_list(repo, command):
    g = Guard(repo, RUN, allow_shell=["echo", "pytest"])
    assert "not-allowed" in {b.wall for b in g.command(command)}


# ── I2: ordinary commands are not refused ─────────────────────────────────────


ALLOW = ["pytest", "uv run pytest", "python -m pytest", "ruff check"]


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q 2>&1",
        "pytest -q >/dev/null 2>&1",
        "pytest -q > out.txt",
        "uv run pytest -q",
        "python -m pytest -q",
        "cd sub && pytest",
        "X=1 pytest -q",
        "ruff check . && pytest",
    ],
)
def test_ordinary_commands_are_allowed(repo, command):
    assert Guard(repo, RUN, allow_shell=ALLOW).command(command) == []


@pytest.mark.parametrize(
    "command",
    [
        "uv run curl x",
        "python -m http.server",
        "pytest -q |& tee log",
        "bash -c 'curl x'",
        "find . -name x -exec rm {} ;",
        "$CMD -q",
    ],
)
def test_what_follows_an_allowed_prefix_is_still_checked(repo, command):
    allow = [*ALLOW, "bash", "find"]
    assert "not-allowed" in {b.wall for b in Guard(repo, RUN, allow_shell=allow).command(command)}


# ── I1: options before the subcommand do not dodge an ask rule ───────────────


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("npm -w a -w b -w c publish", "npm publish"),
        ("npm --prefix a --cache b --userconfig c publish", "npm publish"),
        ("kubectl --server s --token t --user u delete pod x", "kubectl delete"),
    ],
)
def test_padding_does_not_dodge_an_ask_rule(repo, command, rule):
    g = Guard(repo, RUN, ask_shell=["npm publish", "kubectl delete"])
    assert g.asks(command) == [rule]
