"""An allow rule covers a program by NAME only (DF-68): an agent can write an executable
anywhere in its tree, so `./pytest` or `/tmp/x/git status` must never borrow the
`pytest` / `git status` rules. A rule that itself names a path covers that path."""

import subprocess
from pathlib import Path

import pytest

from sdlc_commons.guards import Guard

RUN = "r-paths-1"
GIT_USER = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), *GIT_USER, "commit", "-qm", "base"], check=True)
    return path


@pytest.mark.parametrize(
    "command",
    [
        "./pytest -q",
        "/tmp/x/git status",
        "bin/pytest",
        "bin\\pytest.cmd -q",
        '"C:\\tools\\pytest.exe" -q',
        ".\\pytest.exe",
        "pytest -q && ./pytest",
        "~/bin/pytest",
    ],
)
def test_a_program_given_by_path_is_not_covered_by_a_name_rule(repo, command):
    guard = Guard(repo, RUN, allow_shell=["pytest", "git status"])
    blocks = guard.command(command)
    assert {b.wall for b in blocks} == {"not-allowed"}
    assert any("by name" in b.why for b in blocks)


@pytest.mark.parametrize("command", ["pytest -q", "pytest.exe -q", "PYTEST -q", "git status"])
def test_a_program_called_by_name_is_still_allowed(repo, command):
    assert Guard(repo, RUN, allow_shell=["pytest", "git status"]).command(command) == []


def test_a_rule_naming_a_path_covers_exactly_that_path(repo):
    guard = Guard(repo, RUN, allow_shell=[".venv/bin/pytest"])
    assert guard.command(".venv/bin/pytest -q") == []
    assert guard.command("./.venv/bin/pytest -q") == []
    assert {b.wall for b in guard.command("other/bin/pytest -q")} == {"not-allowed"}
