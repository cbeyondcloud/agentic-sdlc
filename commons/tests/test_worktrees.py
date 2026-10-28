"""Tests for the worktree adapter over Zyroflo's run trees (M1-09)."""

import subprocess
from pathlib import Path

import pytest
from zyroflo.sdlc import worktree as zyroflo_worktree

from sdlc_commons.guards import Guard
from sdlc_commons.ledger import RunIdentity, RunLedger
from sdlc_commons.worktrees import RunWorktrees, WorktreeError

IDENTITY = RunIdentity(team="platform", policy_version="git:" + "a" * 40)

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


def git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *GIT_USER, *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    return path


def open_run(target: Path, run_id: str = "r1") -> RunWorktrees:
    RunLedger(target, run_id).open(
        identity=IDENTITY, kind="feature", project="demo", owner="platform", criticality="low"
    )
    return RunWorktrees(target, run_id)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def estate(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    for name in ("api", "lib"):
        make_repo(root / name)
    (root / ".sdlc").mkdir(parents=True, exist_ok=True)
    (root / ".sdlc" / "profile.yml").write_text(ESTATE, encoding="utf-8")
    return root


# ── create ────────────────────────────────────────────────────────────────────


def test_a_single_repo_gets_one_tree_on_the_run_branch(repo: Path):
    trees = open_run(repo).create()

    (tree,) = trees.values()
    assert tree.component == "repo" and tree.mode == "worktree"
    assert tree.branch == "sdlc/r1"
    assert tree.base == git(repo, "rev-parse", "HEAD")
    assert (tree.path / "app.py").is_file()
    assert git(repo, "status", "--short", "--untracked-files=no") == ""  # checkout untouched


def test_the_tree_is_where_the_guard_lets_the_run_write(repo: Path):
    (tree,) = open_run(repo).create().values()

    assert Guard(repo, "r1").writes([tree.path / "src" / "new.py"]) == []


def test_each_component_gets_its_own_tree(estate: Path):
    trees = open_run(estate).create()

    assert sorted(trees) == ["api", "lib"]
    assert trees["api"].path != trees["lib"].path
    assert all((t.path / "app.py").is_file() for t in trees.values())
    assert git(estate / "lib", "branch", "--list", "sdlc/r1") != ""


def test_two_runs_get_separate_trees(repo: Path):
    (first,) = open_run(repo, "r1").create().values()
    (second,) = open_run(repo, "r2").create().values()

    assert first.path != second.path
    assert (first.branch, second.branch) == ("sdlc/r1", "sdlc/r2")


def test_creating_again_keeps_the_same_trees(repo: Path):
    worktrees = open_run(repo)
    first = worktrees.create()

    assert worktrees.create() == first


def test_trees_lists_what_the_run_has(estate: Path):
    worktrees = open_run(estate)
    made = worktrees.create()

    assert RunWorktrees(estate, "r1").trees() == made


def test_only_the_named_components_are_created(estate: Path):
    trees = open_run(estate).create(["api"])

    assert sorted(trees) == ["api"]


def test_an_unknown_component_is_refused_and_nothing_is_made(estate: Path):
    worktrees = open_run(estate)

    with pytest.raises(WorktreeError, match="nope"):
        worktrees.create(["api", "nope"])
    assert worktrees.trees() == {}
    assert git(estate / "api", "branch", "--list", "sdlc/r1") == ""


def test_a_run_must_be_open_before_its_trees_are_made(repo: Path):
    with pytest.raises(WorktreeError, match="open"):
        RunWorktrees(repo, "r-never-opened").create()


def test_an_untracked_run_folder_in_the_checkout_does_not_block_trees(repo: Path):
    # The run folder (.agentic/local/runs/r1) is untracked in the developer's
    # checkout; Zyroflo's clean-checkout check exempts it wherever it lives (Z3).
    trees = open_run(repo).create()

    assert sorted(trees) == ["repo"]
    assert git(repo, "status", "--short", "--untracked-files=all").startswith("?? .agentic/")


def test_other_uncommitted_work_still_blocks_trees(repo: Path):
    (repo / "app.py").write_text("x = 2  # the developer's own edit\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="uncommitted"):
        open_run(repo).create()


def test_plan_names_the_trees_without_making_them(estate: Path):
    worktrees = open_run(estate)
    planned = worktrees.plan()

    assert sorted(planned) == ["api", "lib"]
    assert not any(t.path.exists() for t in planned.values())
    assert worktrees.trees() == {}


# ── rollback on a failure part-way ────────────────────────────────────────────


def test_a_failure_part_way_takes_back_the_trees_already_made(estate: Path, monkeypatch):
    worktrees = open_run(estate)
    planned = worktrees.plan()
    real = zyroflo_worktree.git

    def fail_on_lib(where, *args, **kwargs):
        if args[:2] == ("worktree", "add") and Path(where).name == "lib":
            raise zyroflo_worktree.WorktreeError("git worktree add in lib failed: simulated")
        return real(where, *args, **kwargs)

    monkeypatch.setattr(zyroflo_worktree, "git", fail_on_lib)
    with pytest.raises(WorktreeError, match="removed"):
        worktrees.create()

    assert not planned["api"].path.exists()
    assert git(estate / "api", "branch", "--list", "sdlc/r1") == ""
    assert worktrees.trees() == {}


# ── remove ────────────────────────────────────────────────────────────────────


def test_remove_takes_the_trees_away(estate: Path):
    worktrees = open_run(estate)
    made = worktrees.create()

    worktrees.remove()

    assert not any(t.path.exists() for t in made.values())
    assert worktrees.trees() == {}


def test_remove_refuses_to_lose_uncommitted_work_unless_forced(repo: Path):
    worktrees = open_run(repo)
    (tree,) = worktrees.create().values()
    (tree.path / "app.py").write_text("x = 2  # an agent's unfinished edit\n", encoding="utf-8")

    with pytest.raises(WorktreeError):
        worktrees.remove()
    assert tree.path.exists()

    worktrees.remove(force=True)
    assert not tree.path.exists()


# ── inputs and in-place components (SP2 review I7) ────────────────────────────

IN_PLACE = """scope: estate
components:
  - name: "api"
    path: "api"
    role: authoritative
    vcs:
      deny_raw_git: true
      notes: "two git dirs"
  - name: "lib"
    path: "lib"
    role: authoritative
"""


def test_an_in_place_component_is_planned_and_made_without_a_tree(estate: Path):
    (estate / ".sdlc" / "profile.yml").write_text(IN_PLACE, encoding="utf-8")
    worktrees = open_run(estate)

    planned = worktrees.plan()
    made = worktrees.create()

    assert planned["api"].mode == made["api"].mode == "in-place"
    assert made["api"].path == estate / "api"  # the component's own checkout
    assert made["lib"].mode == "worktree"


def test_one_component_name_as_a_string_is_refused(estate: Path):
    with pytest.raises(WorktreeError, match="list"):
        open_run(estate).create("api")


@pytest.mark.parametrize("run_id", ["../evil", "r1:x", "r 1"])
def test_a_bad_run_id_is_a_worktree_error(repo: Path, run_id):
    with pytest.raises(WorktreeError, match="run id"):
        RunWorktrees(repo, run_id)
