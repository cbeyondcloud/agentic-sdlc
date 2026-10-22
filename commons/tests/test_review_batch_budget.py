"""The diff-size budget per review batch, measured on a step's real checkpoints (M2-24,
spec §6.1): Zyroflo's budget over `git diff base head` of every tree of the run."""

from pathlib import Path

import pytest
from test_step_checkpoints import git, make_repo, open_run

from sdlc_commons.guards import Guard
from sdlc_commons.review_batches import ReviewBatchBudget

RUN = "r-ckpt-1"  # the run test_step_checkpoints opens


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


def step(repo: Path, files: dict[str, str], **budget):
    """One checkpointed step writing `files`; returns (budget, base, head, tree)."""
    checkpoints = open_run(repo)
    tree = checkpoints.prepare()
    base = checkpoints.begin(fresh=True)
    for rel, text in files.items():
        path = tree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    head = checkpoints.finish("agentic: develop #1")
    return ReviewBatchBudget(Guard(repo, RUN, **budget), checkpoints), base, head, tree


def test_a_change_within_budget_passes(repo):
    budget, base, head, _ = step(repo, {"a.py": "a\n", "b.py": "b\n"}, max_files=2)
    assert budget.over(base, head) == []


def test_a_change_over_budget_says_why(repo):
    budget, base, head, _ = step(repo, {f"m{i}.py": "x\n" for i in range(3)}, max_files=2)
    (reason,) = budget.over(base, head)
    assert "3 files" in reason and "budget 2" in reason


def test_changed_lines_count(repo):
    budget, base, head, _ = step(repo, {"big.py": "x\n" * 30}, max_lines=20)
    assert "30 changed lines" in " ".join(budget.over(base, head))


def test_a_lock_file_is_not_counted(repo):
    budget, base, head, _ = step(repo, {"a.py": "a\n", "uv.lock": "x\n" * 5000}, max_files=1)
    assert budget.over(base, head) == []


def test_no_change_is_never_over(repo):
    budget, base, head, _ = step(repo, {}, max_files=1)
    assert base == head and budget.over(base, head) == []


@pytest.mark.parametrize(
    "key, value",
    [
        ("diff.noprefix", "true"),  # `diff --git x x`: no path for the exemptions
        ("diff.mnemonicPrefix", "true"),  # `c/x w/x`
        ("color.ui", "always"),  # escape codes before every `+`: no line counted
        ("diff.external", "false"),  # an external tool's output instead of a diff
    ],
)
def test_repository_diff_settings_do_not_change_the_count(repo, key, value):
    git(repo, "config", key, value)
    files = {"big.py": "x\n" * 30, "uv.lock": "y\n" * 100}
    budget, base, head, _ = step(repo, files, max_files=1, max_lines=20)
    (reason,) = budget.over(base, head)  # the lock file is exempt; the lines are counted
    assert "30 changed lines" in reason


def test_attributes_in_the_agents_tree_do_not_hide_lines(repo):
    # `* -diff` would print "Binary files differ" for every file: no line counted
    files = {".gitattributes": "* -diff\n", "big.py": "x\n" * 30}
    budget, base, head, _ = step(repo, files, max_lines=20)
    assert "changed lines" in " ".join(budget.over(base, head))


def test_the_limits_read_as_a_sentence(repo):
    budget, *_ = step(repo, {}, max_files=12, max_lines=400)
    text = budget.limits()
    assert "12 files" in text and "400 changed lines" in text and "uv.lock" in text
