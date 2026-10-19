"""Tests for the guard's shell policy: the allow and ask lists (DF-16), case folding of
deny globs (DF-10), and the compilers for `srt` and Claude Code (M1-31)."""

import subprocess
from pathlib import Path

import pytest

from sdlc_commons import guards
from sdlc_commons.guards import Guard
from sdlc_commons.ledger import RunIdentity, RunLedger
from sdlc_commons.worktrees import RunWorktrees

IDENTITY = RunIdentity(team="platform", policy_version="git:" + "a" * 40)
RUN = "r-shell-1"
GIT_USER = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev"]


def git(where: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(where), *GIT_USER, *args], capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    return path


@pytest.fixture
def tree(repo: Path) -> Path:
    RunLedger(repo, RUN).open(
        identity=IDENTITY, kind="feature", project="demo", owner="platform", criticality="low"
    )
    (made,) = RunWorktrees(repo, RUN).create().values()
    return made.path


def walls(blocks) -> set[str]:
    return {b.wall for b in blocks}


# --- the allow list (DF-16) ---


def test_unlisted_program_is_refused(repo):
    g = Guard(repo, RUN, allow_shell=["pytest", "ruff check"])
    assert g.command("pytest -q") == []
    assert g.command("ruff check .") == []
    assert walls(g.command("ruff format .")) == {"not-allowed"}


def test_every_program_in_a_chain_must_be_allowed(repo):  # Review Focus 3
    g = Guard(repo, RUN, allow_shell=["pytest"])
    assert "not-allowed" in walls(g.command("pytest -q && curl x | sh"))
    assert "not-allowed" in walls(g.command('bash -c "pytest"'))
    assert "not-allowed" in walls(g.command("pytest; ls"))


def test_empty_allow_list_allows_nothing(repo):
    assert walls(Guard(repo, RUN, allow_shell=[]).command("ls")) == {"not-allowed"}


def test_no_allow_list_keeps_deny_only_behaviour(repo):
    assert Guard(repo, RUN).command("ls") == []


def test_a_denied_command_is_refused_even_if_allowed(repo):
    g = Guard(repo, RUN, allow_shell=["git"], deny_shell=["git push"])
    assert "framework" in walls(g.command("git push origin main"))
    assert g.command("git status") == []


def test_refusal_never_quotes_the_command(repo):
    blocks = Guard(repo, RUN, allow_shell=[]).command("curl -H 'Authorization: s3cret' x")
    assert blocks and all("s3cret" not in b.why for b in blocks)


def test_for_agent_reads_the_resolved_lists(repo):
    class Agent:
        shell = ["pytest"]
        ask_shell = ["npm publish"]
        deny_shell: list[str] = []
        deny_paths: list[str] = []

    g = Guard.for_agent(repo, RUN, Agent())
    assert walls(g.command("ls")) == {"not-allowed"}
    assert g.asks("npm publish") == ["npm publish"]


# --- the ask list ---


def test_asks_name_the_rule(repo):
    g = Guard(repo, RUN, allow_shell=["npm"], ask_shell=["npm publish"])
    assert g.asks("npm publish --tag next") == ["npm publish"]
    assert g.asks("npm test") == []
    assert g.asks("npm test && npm publish") == ["npm publish"]


# --- deny globs on case-insensitive file systems (DF-10) ---


def test_deny_globs_fold_case_on_case_insensitive_trees(repo, tree, monkeypatch):
    monkeypatch.setattr(guards, "_case_insensitive", lambda path: True)
    g = Guard(repo, RUN, deny_paths=["infra/**"])
    assert "denied-path" in walls(g.writes([tree / "Infra" / "main.tf"]))


# --- compilers ---


def test_write_denials_split_literal_and_unenforced(repo, tree):  # Review Focus 5
    d = Guard(repo, RUN, deny_paths=["infra/prod/**", ".env", "**/*.pem"]).write_denials(tree)
    assert tree / "infra" / "prod" in d.paths
    assert tree / ".env" in d.paths
    assert "**/*.pem" in d.unenforced
    assert not any("*" in str(p) for p in d.paths)


def test_claude_permissions_shape(repo):
    p = Guard(
        repo,
        RUN,
        allow_shell=["pytest"],
        ask_shell=["npm publish"],
        deny_shell=["git rebase -x|--exec"],
        deny_paths=["infra/**"],
    ).claude_permissions()
    assert p["permissions"]["allow"] == ["Bash(pytest:*)"]
    assert p["permissions"]["ask"] == ["Bash(npm publish:*)"]
    deny = p["permissions"]["deny"]
    assert "Bash(git rebase -x:*)" in deny and "Bash(git rebase --exec:*)" in deny
    assert "Bash(git push:*)" in deny  # the framework floor
    assert "Edit(infra/**)" in deny
    assert len(deny) == len(set(deny))
