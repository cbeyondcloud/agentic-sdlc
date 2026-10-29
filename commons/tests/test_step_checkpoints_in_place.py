"""Per-step checkpoints on a component Zyroflo makes in place (M2-61; DF-62, DF-287, DF-360):
the step's work is committed and rolled back by the component's own commands (the
commit-command override), and the framework runs no git write on it."""

from pathlib import Path

import pytest
from in_place_support import ALLOW, ROLLBACKS, RUN, git, make_in_place_repo, open_ledger

from sdlc_commons import worktrees
from sdlc_commons.commit_override import CommitOverride, OverrideError
from sdlc_commons.worktrees import StepCheckpoints, WorktreeError, change_since_base

#: The git subcommands that write: none of them may run on an in-place component.
GIT_WRITES = {"commit", "reset", "clean", "add", "push", "worktree", "checkout", "stash"}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("VCS_TOOL_LOG", str(tmp_path / "tool.log"))
    return make_in_place_repo(tmp_path / "repo")


@pytest.fixture
def git_calls(monkeypatch) -> list[tuple[str, ...]]:
    """Every git call the framework makes (the tool's own git is its business)."""
    calls: list[tuple[str, ...]] = []
    real = worktrees.zyroflo.git

    def spy(where, *args, check=True):
        calls.append(tuple(args))
        return real(where, *args, check=check)

    monkeypatch.setattr(worktrees.zyroflo, "git", spy)
    return calls


def checkpoints(repo: Path, **kwargs) -> StepCheckpoints:
    open_ledger(repo)
    settings = {"allow": ALLOW, "rollback_commands": ROLLBACKS} | kwargs
    return StepCheckpoints(repo, RUN, override=CommitOverride(repo, RUN, **settings))


def tool_calls(repo: Path) -> list[str]:
    log = repo.parent / "tool.log"
    return log.read_text(encoding="utf-8").split() if log.is_file() else []


def written(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [c for c in calls if GIT_WRITES & set(c)]


def test_a_code_step_checkpoints_and_rolls_back_through_the_override(repo, git_calls):
    steps = checkpoints(repo)
    assert steps.prepare() == repo  # agents work in the component's own checkout
    base = steps.begin(fresh=True)
    assert base == {"repo": git(repo, "rev-parse", "HEAD")}

    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "new.py").write_text("y = 1\n", encoding="utf-8")
    assert sorted(p.name for p in steps.changed()) == ["app.py", "new.py"]
    head = steps.finish("build: step 1")
    assert head["repo"] != base["repo"] and head["repo"] == git(repo, "rev-parse", "HEAD")
    assert git(repo, "log", "-1", "--format=%s") == "build: step 1"
    assert "+x = 2" in steps.diff(base, head)

    steps.begin(fresh=True)
    (repo / "app.py").write_text("x = 3\n", encoding="utf-8")
    steps.finish("build: step 2")
    steps.rollback(base)  # the reviewer rejected: back to step 1's base
    assert git(repo, "rev-parse", "HEAD") == base["repo"]
    assert (repo / "app.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (repo / "new.py").exists()

    assert tool_calls(repo) == ["commit", "commit", "restore"]
    assert written(git_calls) == []  # raw git is never run on it


def test_the_reviewer_reads_the_in_place_change_since_the_runs_base(repo):  # M2-48 pre-pass
    assert change_since_base(repo, RUN) == []  # no override accepted yet: nothing in place
    steps = checkpoints(repo)
    steps.prepare()
    steps.begin(fresh=True)
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    steps.finish("build: step 1")
    [(name, where, diff)] = change_since_base(repo, RUN)
    assert (name, where) == ("repo", repo) and "+x = 2" in diff


def test_a_fresh_attempt_discards_through_the_rollback_command(repo, git_calls):
    steps = checkpoints(repo)
    steps.prepare()
    base = steps.begin(fresh=True)
    (repo / "half-done.py").write_text("z = 1\n", encoding="utf-8")
    assert steps.begin(fresh=True) == base  # the attempt starts again
    assert not (repo / "half-done.py").exists()
    assert tool_calls(repo) == ["restore"]
    assert written(git_calls) == []


def test_a_clean_fresh_attempt_runs_no_command(repo):
    steps = checkpoints(repo)
    steps.prepare()
    steps.begin(fresh=True)
    steps.begin(fresh=True)
    assert tool_calls(repo) == []


def test_a_step_that_changed_nothing_commits_nothing(repo):
    steps = checkpoints(repo)
    steps.prepare()
    base = steps.begin(fresh=True)
    assert steps.finish("nothing") == base
    assert tool_calls(repo) == []


def test_a_commit_command_that_leaves_changes_is_refused(repo, monkeypatch):
    """Review F1: work the tool did not commit would be missing from the review and the pull
    request, and the next rollback would discard it - the step is not checkpointed."""
    steps = checkpoints(repo)
    steps.prepare()
    steps.begin(fresh=True)
    monkeypatch.setenv("VCS_TOOL_MODE", "lazy")  # commits tracked files only
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "new_feature.py").write_text("y = 1\n", encoding="utf-8")
    with pytest.raises(WorktreeError, match="new_feature.py") as refused:
        steps.finish("build: step 1")
    assert "commit every change" in str(refused.value)


def test_a_rollback_command_that_leaves_changes_is_refused(repo, monkeypatch):
    """Review F2: HEAD at the commit is half the contract; the rejected work must go too."""
    steps = checkpoints(repo)
    steps.prepare()
    base = steps.begin(fresh=True)
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    steps.finish("build: step 1")
    monkeypatch.setenv("VCS_TOOL_MODE", "lazy")  # moves HEAD back, keeps the work
    with pytest.raises(WorktreeError, match="left changes") as refused:
        steps.rollback(base)
    assert "app.py" in str(refused.value)


def test_a_fresh_attempt_whose_discard_leaves_changes_is_refused(repo, monkeypatch):
    steps = checkpoints(repo)
    steps.prepare()
    steps.begin(fresh=True)
    (repo / "half-done.py").write_text("z = 1\n", encoding="utf-8")
    monkeypatch.setenv("VCS_TOOL_MODE", "lazy")
    with pytest.raises(WorktreeError, match="left changes"):
        steps.begin(fresh=True)


def test_rollback_refuses_a_commit_outside_the_checkouts_history(repo):
    steps = checkpoints(repo)
    steps.prepare()
    with pytest.raises(WorktreeError, match="not part of"):
        steps.rollback({"repo": "0" * 40})
    assert tool_calls(repo) == []


def test_without_the_override_the_refusal_names_the_settings(repo):
    open_ledger(repo)
    with pytest.raises(WorktreeError) as refused:
        StepCheckpoints(repo, RUN).prepare()
    message = str(refused.value)
    assert "vcs.deny_raw_git" in message and "vcs.commit_command" in message
    assert "rollback_commands" in message and "commit_override.allow" in message


def test_without_org_allow_the_run_is_refused(repo):
    with pytest.raises(OverrideError, match=r"commit_override\.allow"):
        checkpoints(repo, allow=()).prepare()


def test_a_run_mixing_in_place_and_tree_components_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    make_in_place_repo(root / "api")
    lib = root / "lib"
    lib.mkdir()
    git(lib, "init", "-q", "-b", "main")
    (lib / "x.py").write_text("x = 1\n", encoding="utf-8")
    git(lib, "add", "-A")
    git(lib, "commit", "-qm", "base")
    (root / ".sdlc").mkdir()
    (root / ".sdlc" / "profile.yml").write_text(
        "scope: estate\ncomponents:\n"
        '  - name: "api"\n    path: "api"\n    role: authoritative\n'
        "    vcs:\n      deny_raw_git: true\n"
        '  - name: "lib"\n    path: "lib"\n    role: authoritative\n',
        encoding="utf-8",
    )
    with pytest.raises(WorktreeError, match="api") as refused:
        checkpoints(root).prepare()
    assert "a tree" in str(refused.value)
