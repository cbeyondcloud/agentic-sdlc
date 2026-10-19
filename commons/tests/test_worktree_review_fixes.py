"""Regression tests for the SP8 final review (Sonnet 5.5) of step checkpoints: each pins
one finding (I4 nested repositories, I5 hardening)."""

import subprocess
from pathlib import Path

import pytest

from sdlc_commons.ledger import RunIdentity, RunLedger
from sdlc_commons.worktrees import StepCheckpoints, WorktreeError

IDENTITY = RunIdentity(team="platform", policy_version="git:" + "a" * 40)
RUN = "r-review-2"
GIT_USER = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev"]


def git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *GIT_USER, *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    RunLedger(path, RUN).open(
        identity=IDENTITY, kind="feature", project="demo", owner="platform", criticality="low"
    )
    return path


def nested_repo(tree: Path, name: str = "sub", commit: bool = False) -> Path:
    inner = tree / name
    inner.mkdir()
    git(inner, "init", "-q")
    (inner / "lib.py").write_text("y = 1\n", encoding="utf-8")
    if commit:
        git(inner, "add", "-A")
        git(inner, "commit", "-qm", "inner")
    return inner


# ── I4: a nested repository never jams the checkpoints ───────────────────────


@pytest.mark.parametrize("commit", [False, True])
def test_a_nested_repository_is_refused_by_name_not_committed(repo, commit):
    checkpoints = StepCheckpoints(repo, RUN)
    tree = checkpoints.prepare()
    checkpoints.begin(fresh=True)
    nested_repo(tree, commit=commit)

    with pytest.raises(WorktreeError, match="sub"):
        checkpoints.finish("step")
    assert git(tree, "log", "-1", "--format=%s") == "base"  # nothing half-committed


def test_a_fresh_begin_removes_a_nested_repository(repo):
    checkpoints = StepCheckpoints(repo, RUN)
    tree = checkpoints.prepare()
    nested_repo(tree, commit=True)

    checkpoints.begin(fresh=True)

    assert not (tree / "sub").exists()
    (tree / "ok.py").write_text("z = 1\n", encoding="utf-8")
    checkpoints.finish("step")  # the retry works again


def test_rollback_removes_a_nested_repository(repo):
    checkpoints = StepCheckpoints(repo, RUN)
    tree = checkpoints.prepare()
    base = checkpoints.begin(fresh=True)
    nested_repo(tree)

    checkpoints.rollback(base)

    assert not (tree / "sub").exists()


# ── I5: the engine's git calls never run an fsmonitor command ─────────────────


def test_checkpoints_never_run_an_fsmonitor_command(repo):
    checkpoints = StepCheckpoints(repo, RUN)
    tree = checkpoints.prepare()
    marker = repo.parent / "fsmonitor-ran"
    script = repo.parent / "monitor.sh"
    script.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", encoding="utf-8")
    script.chmod(0o755)
    git(repo, "config", "core.fsmonitor", script.as_posix())
    (tree / "x.py").write_text("x\n", encoding="utf-8")

    base = checkpoints.begin(fresh=False)
    checkpoints.finish("step")
    checkpoints.rollback(base)

    assert not marker.exists()
