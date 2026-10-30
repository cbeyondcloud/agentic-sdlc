"""The guard on an in-place component (M2-61): with its commit-command override accepted,
the component's own checkout is where the run's agents work, except the harness's folders."""

from pathlib import Path

import pytest
from in_place_support import ALLOW, ROLLBACKS, RUN, make_in_place_repo, open_ledger

from sdlc_commons.commit_override import CommitOverride
from sdlc_commons.guards import Guard
from sdlc_commons.worktrees import StepCheckpoints


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("VCS_TOOL_LOG", str(tmp_path / "tool.log"))
    repo = make_in_place_repo(tmp_path / "repo")
    open_ledger(repo)
    return repo


def accept(repo: Path) -> Path:
    override = CommitOverride(repo, RUN, allow=ALLOW, rollback_commands=ROLLBACKS)
    return StepCheckpoints(repo, RUN, override=override).prepare()


def walls(blocks) -> list[str]:
    return [block.wall for block in blocks]


def test_without_an_accepted_override_the_checkout_is_outside_the_run(repo):
    assert "worktree" in walls(Guard(repo, RUN).writes([repo / "app.py"]))


def test_an_accepted_override_lets_agents_write_the_checkout(repo):
    home = accept(repo)
    guard = Guard(repo, RUN)
    assert guard.writes([home / "app.py", home / "src" / "new.py"]) == []
    assert guard.changes([home / "app.py"]) == []


@pytest.mark.parametrize(
    "rel",
    [
        ".sdlc/profile.yml",  # where the commit command is declared
        ".sdlc-worktrees/other-run/x.py",  # other runs' trees
        ".agentic/config.yaml",  # where the rollback command is declared
        ".agentic/local/runs/r-inplace-1/vcs-override.json",
        ".git/config",
    ],
)
def test_the_harness_folders_in_the_checkout_stay_unwritable(repo, rel):
    home = accept(repo)
    guard = Guard(repo, RUN)
    assert guard.writes([home / rel]) != []
    assert guard.changes([home / rel]) != []


def test_the_sandbox_denies_the_harness_folders_in_the_checkout(repo):
    home = accept(repo)
    denied = Guard(repo, RUN).write_denials(home).paths
    assert home / ".sdlc" in denied and home / ".sdlc-worktrees" in denied
    assert home / ".agentic" in denied and home / ".git" in denied
