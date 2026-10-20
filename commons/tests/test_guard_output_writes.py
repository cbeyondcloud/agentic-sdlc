"""Regression tests for the SP18b final review, finding F3: a command an agent may run
must not write a file the file tools would have checked. git's output options
(`git diff --output=../x`) are refused for every agent; a read-only agent (no
file-writing tool, for example Code Review) may not redirect output into a file."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from sdlc_commons.guards import Guard

RUN = "r-output-1"
READ_ONLY_GIT = ["git status", "git diff", "git log", "git show"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path  # command checks need no repository content


def walls(blocks) -> set[str]:
    return {b.wall for b in blocks}


# ── git's output options: the framework floor, for every agent ────────────────


@pytest.mark.parametrize(
    "command",
    [
        "git diff --output=../escape.txt",
        "git diff --output ../escape.txt",
        "git log --output=x",
        "git show --output=x HEAD",
        "git -C . diff --output=x",
        "git diff --outp=x",  # git accepts an unambiguous abbreviation
        "git format-patch -o out HEAD~1",
        "git format-patch -oout HEAD~1",
        "git format-patch --output-directory=out HEAD~1",
        "git archive -o x.zip HEAD",
        "git archive --output=x.zip HEAD",
        "bash -c 'git diff --output=x'",
    ],
)
def test_git_output_options_are_refused_for_every_agent(repo, command):
    assert "framework" in walls(Guard(repo, RUN).command(command))


@pytest.mark.parametrize(
    "command",
    [
        "git log --oneline",
        "git diff --stat",
        "git diff --output-indicator-new=+",
        "git ls-files -o",
        "git log --grep output",
        'git commit -m "write the output"',
    ],
)
def test_other_git_options_are_not_refused(repo, command):
    assert Guard(repo, RUN).command(command) == []


# ── redirects: a read-only agent writes no file ───────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "git diff > ../escape.txt",
        "git diff >../escape.txt",
        "git diff >> notes.txt",
        "git diff 1> x",
        "git log &> x",
        "git log &>> x",
        "git log >| x",
        "git log >& x",
        "git show HEAD <> x",
        "git diff 2>&1 > src/app.py",
    ],
)
def test_a_read_only_agent_may_not_redirect_into_a_file(repo, command):
    guard = Guard(repo, RUN, allow_shell=READ_ONLY_GIT, writes_files=False)
    assert "framework" in walls(guard.command(command))


@pytest.mark.parametrize(
    "command",
    [
        "git diff",
        "git diff 2>&1",
        "git diff >&2",
        "git diff > /dev/null",
        "git diff 2>/dev/null",
        "git diff 2>nul",
        "git diff > NUL",
        "git diff > $null",
        'git log --grep "a > b"',
    ],
)
def test_a_read_only_agent_may_still_drop_or_merge_output(repo, command):
    guard = Guard(repo, RUN, allow_shell=READ_ONLY_GIT, writes_files=False)
    assert guard.command(command) == []


def test_a_redirect_an_interpreter_runs_counts(repo):
    guard = Guard(repo, RUN, writes_files=False)
    assert "framework" in walls(guard.command("bash -c 'git diff > x'"))
    assert "framework" in walls(guard.command('powershell -Command "git diff > x"'))


def test_an_agent_that_writes_files_may_redirect(repo):
    guard = Guard(repo, RUN, allow_shell=["pytest"])
    assert guard.command("pytest -q > out.txt") == []


def test_for_agent_reads_whether_the_agent_writes_files(repo):
    reviewer = SimpleNamespace(
        shell=READ_ONLY_GIT,
        ask_shell=[],
        deny_shell=[],
        deny_paths=[],
        tools=["read_files", "run_shell"],
    )
    developer = SimpleNamespace(**{**vars(reviewer), "tools": ["edit_files", "run_shell"]})
    unknown = SimpleNamespace(shell=READ_ONLY_GIT, ask_shell=[], deny_shell=[], deny_paths=[])

    assert "framework" in walls(Guard.for_agent(repo, RUN, reviewer).command("git diff > x"))
    assert Guard.for_agent(repo, RUN, developer).command("git diff > x") == []
    assert Guard.for_agent(repo, RUN, unknown).command("git diff > x") == []
