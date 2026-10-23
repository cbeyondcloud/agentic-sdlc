"""Tests for per-step checkpoints on the run's tree (M1-30; spec §9.2: the step's diff is
the reviewable artefact, rollback = back to the step's base)."""

import subprocess
from pathlib import Path

import pytest

from sdlc_commons.guards import Guard
from sdlc_commons.ledger import RunIdentity, RunLedger
from sdlc_commons.worktrees import StepCheckpoints, WorktreeError

IDENTITY = RunIdentity(team="platform", policy_version="git:" + "a" * 40)
RUN = "r-ckpt-1"
GIT_USER = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev"]
ESTATE = """scope: estate
components:
  - name: "api"
    path: "api"
    role: authoritative
  - name: "lib"
    path: "lib"
    role: authoritative
"""
IN_PLACE = ESTATE.replace(
    '  - name: "lib"', '    vcs:\n      deny_raw_git: true\n  - name: "lib"', 1
)


def git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *GIT_USER, *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    return path


def open_run(target: Path) -> StepCheckpoints:
    RunLedger(target, RUN).open(
        identity=IDENTITY, kind="feature", project="demo", owner="platform", criticality="low"
    )
    return StepCheckpoints(target, RUN)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def estate(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    for name in ("api", "lib"):
        make_repo(root / name)
    (root / ".sdlc").mkdir()
    (root / ".sdlc" / "profile.yml").write_text(ESTATE, encoding="utf-8")
    return root


def test_prepare_returns_a_single_repos_own_tree(repo):
    checkpoints = open_run(repo)
    tree = checkpoints.prepare()
    assert tree == Guard(repo, RUN).trees_root / "repo"
    assert (tree / "app.py").is_file()
    assert checkpoints.prepare() == tree  # making the trees again is harmless


def test_prepare_returns_the_folder_holding_an_estates_trees(estate):
    tree = open_run(estate).prepare()
    assert tree == Guard(estate, RUN).trees_root
    assert (tree / "api" / "app.py").is_file() and (tree / "lib" / "app.py").is_file()


def test_an_in_place_component_is_refused(estate):
    (estate / ".sdlc" / "profile.yml").write_text(IN_PLACE, encoding="utf-8")
    with pytest.raises(WorktreeError, match="api"):
        open_run(estate).prepare()


def test_finish_commits_each_step_on_the_run_branch(repo):
    checkpoints = open_run(repo)
    tree = checkpoints.prepare()
    base = checkpoints.begin(fresh=True)
    (tree / "new.py").write_text("y = 2\n", encoding="utf-8")
    commit = checkpoints.finish("agentic: develop #1 (r-ckpt-1)")

    assert commit["repo"] != base["repo"]
    assert git(tree, "rev-parse", "--abbrev-ref", "HEAD") == f"sdlc/{RUN}"
    assert [
        line
        for line in git(tree, "show", "--name-only", "--format=%s", commit["repo"]).splitlines()
        if line
    ] == [
        "agentic: develop #1 (r-ckpt-1)",
        "new.py",
    ]


def test_changed_lists_what_a_step_wrote_since_its_base(estate):  # M2-60, DF-443
    checkpoints = open_run(estate)
    root = checkpoints.prepare()
    checkpoints.begin(fresh=True)
    (root / "api" / "app.py").write_text("x = 2\n", encoding="utf-8")  # changed
    (root / "lib" / ".github" / "workflows").mkdir(parents=True)
    (root / "lib" / ".github" / "workflows" / "ci.yml").write_text("on: push\n", "utf-8")
    (root / "lib" / "app.py").unlink()  # deleted
    (root / "api" / ".venv").mkdir()
    (root / "api" / ".venv" / "ignored.txt").write_text("x", encoding="utf-8")  # git ignores it
    (root / "api" / "with space.txt").write_text("x", encoding="utf-8")

    changed = sorted(checkpoints.changed())

    assert changed == sorted(
        [
            root / "api" / "app.py",
            root / "api" / "with space.txt",
            root / "lib" / ".github" / "workflows" / "ci.yml",
            root / "lib" / "app.py",
        ]
    )


def test_nothing_changed_lists_nothing(repo):
    checkpoints = open_run(repo)
    checkpoints.prepare()
    checkpoints.begin(fresh=True)
    assert checkpoints.changed() == []


def test_finish_leaves_test_run_caches_out_of_the_checkpoint(repo):  # M1-68 finding
    """Running the tests in the tree writes bytecode and tool caches; a repository with no
    .gitignore for them must not get them committed on the run branch (and in its PR)."""
    checkpoints = open_run(repo)
    tree = checkpoints.prepare()
    git(tree, "rm", "-q", "--cached", ".gitignore")  # a repository that ignores nothing
    git(tree, *["-c", "commit.gpgsign=false"], "commit", "-qm", "no ignores")
    (tree / ".gitignore").unlink()
    (tree / "vendor" / "node_modules").mkdir(parents=True)
    (tree / "vendor" / "node_modules" / "kept.js").write_text("v = 1\n", encoding="utf-8")
    git(tree, "add", "-A")
    git(tree, *["-c", "commit.gpgsign=false"], "commit", "-qm", "vendored on purpose")
    checkpoints.begin(fresh=True)

    (tree / "split.py").write_text("y = 2\n", encoding="utf-8")
    for junk in (
        "__pycache__/app.cpython-312.pyc",
        "pkg/__pycache__/mod.cpython-312.pyc",
        "legacy.pyc",
        ".pytest_cache/v/cache/lastfailed",
        ".mypy_cache/3.12/app.meta.json",
        ".ruff_cache/0.7/x",
        ".hypothesis/examples/1",
        ".coverage",
        "web/node_modules/left-pad/index.js",
    ):
        (tree / junk).parent.mkdir(parents=True, exist_ok=True)
        (tree / junk).write_bytes(b"\x00generated")
    (tree / "vendor" / "node_modules" / "kept.js").write_text("v = 2\n", encoding="utf-8")
    commit = checkpoints.finish("agentic: qa #1 (r-ckpt-1)")

    changed = git(tree, "show", "--name-only", "--format=", commit["repo"]).split()
    assert sorted(changed) == ["split.py", "vendor/node_modules/kept.js"]  # tracked: kept


def test_a_checkpoint_leaves_the_tree_clean_so_the_run_can_be_removed(repo):  # review F2
    """Caches left out of the checkpoint must not leave the tree dirty: removing the run's
    trees at closure refuses any uncommitted change. Ignored files (.venv/) are kept."""
    checkpoints = open_run(repo)
    tree = checkpoints.prepare()
    (tree / "split.py").write_text("y = 2\n", encoding="utf-8")
    for junk in ("__pycache__/split.cpython-312.pyc", "pkg/__pycache__/m.pyc", ".coverage"):
        (tree / junk).parent.mkdir(parents=True, exist_ok=True)
        (tree / junk).write_bytes(b"\x00generated")
    (tree / ".venv").mkdir()
    (tree / ".venv" / "kept.txt").write_text("ignored\n", encoding="utf-8")
    checkpoints.finish("agentic: qa #1 (r-ckpt-1)")

    assert git(tree, "status", "--porcelain") == ""
    assert (tree / ".venv" / "kept.txt").is_file()
    checkpoints.worktrees.remove()  # not forced: nothing of value is left uncommitted
    assert not tree.exists()


def test_finish_with_no_change_returns_head_unchanged(repo):
    checkpoints = open_run(repo)
    checkpoints.prepare()
    base = checkpoints.begin(fresh=True)
    assert checkpoints.finish("nothing") == base


def test_fresh_begin_discards_leftovers_but_keeps_ignored_files(repo):  # Review Focus 4
    checkpoints = open_run(repo)
    tree = checkpoints.prepare()
    (tree / "app.py").write_text("x = 'half-done'\n", encoding="utf-8")
    (tree / "stray.py").write_text("junk\n", encoding="utf-8")
    (tree / ".venv").mkdir()
    (tree / ".venv" / "keep").write_text("env\n", encoding="utf-8")

    checkpoints.begin(fresh=True)

    assert (tree / "app.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (tree / "stray.py").exists()
    assert (tree / ".venv" / "keep").is_file()


def test_resume_begin_keeps_work_in_progress(repo):
    checkpoints = open_run(repo)
    tree = checkpoints.prepare()
    (tree / "wip.py").write_text("w = 1\n", encoding="utf-8")
    checkpoints.begin(fresh=False)
    assert (tree / "wip.py").is_file()


def test_rollback_returns_to_the_step_base(repo):
    checkpoints = open_run(repo)
    tree = checkpoints.prepare()
    base = checkpoints.begin(fresh=True)
    (tree / "step1.py").write_text("1\n", encoding="utf-8")
    checkpoints.finish("step 1")
    (tree / "uncommitted.py").write_text("2\n", encoding="utf-8")

    checkpoints.rollback(base)

    assert git(tree, "rev-parse", "HEAD") == base["repo"]
    assert not (tree / "step1.py").exists() and not (tree / "uncommitted.py").exists()


def test_rollback_refuses_a_commit_outside_the_branch(repo):
    checkpoints = open_run(repo)
    tree = checkpoints.prepare()
    git(repo, "checkout", "-q", "-b", "elsewhere")
    (repo / "other.py").write_text("o\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "unrelated")
    foreign = git(repo, "rev-parse", "HEAD")

    with pytest.raises(WorktreeError, match="not part of"):
        checkpoints.rollback({"repo": foreign})
    assert (tree / "app.py").is_file()


def test_checkpoint_never_runs_repo_hooks(repo):  # Review Focus 2
    checkpoints = open_run(repo)
    tree = checkpoints.prepare()
    hooks = repo / "my-hooks"
    hooks.mkdir()
    marker = repo / "hook-ran"
    for name in ("pre-commit", "post-commit", "commit-msg"):
        script = hooks / name
        script.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", encoding="utf-8")
        script.chmod(0o755)
    git(repo, "config", "core.hooksPath", str(hooks))
    checkpoints.begin(fresh=True)
    (tree / "x.py").write_text("x\n", encoding="utf-8")

    checkpoints.finish("step")

    assert not marker.exists()
    assert git(tree, "log", "-1", "--format=%s") == "step"
