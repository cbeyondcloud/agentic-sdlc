"""The diff budget's exemptions are floored by org policy (M2-24, DF-13): a repository's
own policy file may count more paths, never exempt more."""

from pathlib import Path

import pytest
from test_guards import RUN, make_repo, walls
from tools.sdlc import policy as zyroflo

from sdlc_commons.guards import Guard, GuardError


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


def repo_policy(repo: Path, text: str) -> None:
    (repo / ".sdlc").mkdir(exist_ok=True)
    (repo / ".sdlc" / "policy.yml").write_text(text, encoding="utf-8")


def diff_of(*paths: str) -> str:
    return "".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n+x\n" for p in paths)


def test_the_policy_models_ceiling_is_zyroflos_budget():  # F4
    """Core cannot import Zyroflo, so its policy model names the ceiling an org's
    review_batch may not exceed; it must stay the guard's own budget."""
    from agentic.policy.model import REVIEW_BATCH_CEILING

    assert REVIEW_BATCH_CEILING == {
        "max_files": zyroflo.DEFAULTS["max_files_per_batch"],
        "max_lines": zyroflo.DEFAULTS["max_lines_per_batch"],
    }


def test_zyroflos_exemptions_apply_by_default(repo):
    guard = Guard(repo, RUN, max_files=2)
    assert guard.policy["budget_exempt"] == zyroflo.DEFAULTS["budget_exempt"]
    assert guard.diff(diff_of("a.py", "b.py", "uv.lock")) == []  # the lock file is free


def test_a_repo_cannot_add_budget_exemptions(repo):  # Review Focus 5
    repo_policy(repo, "budget_exempt: ['**', '**/*.py', '**/uv.lock']\n")
    guard = Guard(repo, RUN, max_files=2)
    assert guard.policy["budget_exempt"] == ["**/uv.lock"]
    assert walls(guard.diff(diff_of("a.py", "b.py", "c.py"))) == {"budget"}


def test_a_repo_may_count_what_would_be_exempt(repo):
    repo_policy(repo, "budget_exempt: []\n")
    guard = Guard(repo, RUN, max_files=2)
    assert guard.policy["budget_exempt"] == []
    assert walls(guard.diff(diff_of("a.py", "b.py", "uv.lock"))) == {"budget"}


def test_the_orgs_list_replaces_zyroflos(repo):
    guard = Guard(repo, RUN, max_files=1, budget_exempt=["**/*.snap"])
    assert guard.policy["budget_exempt"] == ["**/*.snap"]
    assert guard.diff(diff_of("a.py", "ui/__snapshots__/x.snap")) == []
    assert walls(guard.diff(diff_of("a.py", "uv.lock"))) == {"budget"}


def test_a_repo_narrows_the_orgs_list_only(repo):
    repo_policy(repo, "budget_exempt: ['**/*.snap', '**/*.py']\n")
    guard = Guard(repo, RUN, budget_exempt=["**/*.snap", "**/go.sum"])
    assert guard.policy["budget_exempt"] == ["**/*.snap"]


@pytest.mark.parametrize("value", ["**/*.lock", [""], [7]])
def test_an_exemption_list_that_is_not_a_list_of_patterns_is_refused(repo, value):
    with pytest.raises(GuardError):
        Guard(repo, RUN, budget_exempt=value)
