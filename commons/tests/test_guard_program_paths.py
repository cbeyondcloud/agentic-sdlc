"""An allow rule covers a program by NAME only (DF-68): an agent can write an executable
anywhere in its tree, so `./pytest` or `/tmp/x/git status` must never borrow the
`pytest` / `git status` rules. A rule that itself names a path covers that path."""

import subprocess
from pathlib import Path

import pytest

from sdlc_commons import guards
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


@pytest.mark.parametrize("windows", [True, False])
@pytest.mark.parametrize(
    ("rule", "command"),
    [
        (".venv/bin/pytest", ".venv/bin/pytest.sh -q"),
        (".venv/bin/pytest", ".venv/bin/pytest.ps1 -q"),
        (".venv/bin/pytest.exe", ".venv/bin/pytest.cmd -q"),
        (".venv/bin/pytest.exe", ".venv/bin/pytest -q"),
    ],
)
def test_a_path_rule_does_not_cover_a_sibling_file(repo, monkeypatch, windows, rule, command):
    """`pytest.sh` beside `pytest` is a different file the agent can write (final review F2)."""
    monkeypatch.setattr(guards, "_WINDOWS", windows, raising=False)
    blocks = Guard(repo, RUN, allow_shell=[rule]).command(command)
    assert {b.wall for b in blocks} == {"not-allowed"}


@pytest.mark.parametrize("command", [".venv/bin/pytest.bat -q", ".venv\\Scripts\\pytest.cmd -q"])
def test_off_windows_a_path_rule_covers_no_suffixed_spelling(repo, monkeypatch, command):
    monkeypatch.setattr(guards, "_WINDOWS", False, raising=False)
    guard = Guard(repo, RUN, allow_shell=[".venv/bin/pytest", ".venv/Scripts/pytest"])
    assert {b.wall for b in guard.command(command)} == {"not-allowed"}


def test_off_windows_a_path_rule_keeps_its_case(repo, monkeypatch):
    monkeypatch.setattr(guards, "_WINDOWS", False, raising=False)
    guard = Guard(repo, RUN, allow_shell=["./Make"])
    assert guard.command("./Make all") == []
    assert {b.wall for b in guard.command("./make all")} == {"not-allowed"}


@pytest.mark.parametrize(
    "command",
    [".venv/Scripts/pytest -q", ".venv/Scripts/pytest.exe -q", ".VENV/scripts/PYTEST.CMD"],
)
def test_on_windows_a_suffixless_path_rule_covers_what_cmd_would_run(repo, monkeypatch, command):
    """cmd already runs `pytest` as any of .com/.exe/.bat/.cmd and ignores case, so those
    spellings add nothing a suffix-less Windows rule does not already allow."""
    monkeypatch.setattr(guards, "_WINDOWS", True, raising=False)
    assert Guard(repo, RUN, allow_shell=[".venv/Scripts/pytest"]).command(command) == []


# ── a name rule trusts PATH, so the command may not change it (final review F3) ──


@pytest.mark.parametrize(
    "command",
    [
        "PATH=./bin:$PATH pytest",
        "export PATH=./bin:$PATH && pytest",
        "export path=bin; pytest",
        "PATH+=:./bin pytest",
        'set "PATH=bin" && pytest',
        "set PATH=bin && git status",
        "PATHEXT=.SH pytest",
        "COMSPEC=./x.exe pytest",
        "BASH_ENV=./x.sh pytest",
        "LD_PRELOAD=./x.so git status",
        "export LD_LIBRARY_PATH=./lib && pytest",
        "DYLD_INSERT_LIBRARIES=./x.dylib pytest",
        "$env:PATH = './bin'; pytest",
    ],
)
def test_a_command_may_not_change_the_program_search_under_an_allow_list(repo, command):
    blocks = Guard(repo, RUN, allow_shell=["pytest", "git status"]).command(command)
    assert {b.wall for b in blocks} == {"not-allowed"}
    assert any("PATH" in b.why for b in blocks)


@pytest.mark.parametrize(
    "command",
    ["PYTHONPATH=src pytest", "MANPATH=x pytest", "pytest --rootdir=PATH", "FOO_PATH=x pytest"],
)
def test_other_variables_and_words_still_pass(repo, command):
    assert Guard(repo, RUN, allow_shell=["pytest"]).command(command) == []


def test_without_an_allow_list_a_path_change_is_left_to_the_deny_rules(repo):
    """With no allow list the agent may run any program by any path, so PATH protects
    nothing there; the check belongs to the allow list."""
    assert Guard(repo, RUN).command("PATH=/opt/tools/bin:$PATH make") == []
