"""The diff-size budget per review batch in org policy (M2-24, spec §6.1)."""

import shutil
from pathlib import Path

import pytest

from agentic.policy import PolicyError, ReviewBatchPolicy, load_policy


def policy_copy(fixtures: Path, tmp_path: Path, org_extra: str) -> Path:
    root = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", root)
    org = root / "org.yaml"
    org.write_text(org.read_text(encoding="utf-8") + org_extra, encoding="utf-8")
    return root


def test_the_default_leaves_the_guards_budget(fixtures):
    policy = load_policy(fixtures / "policy" / "policy")
    assert policy.review_batch == ReviewBatchPolicy()
    assert (policy.review_batch.max_files, policy.review_batch.max_lines) == (None, None)
    assert policy.review_batch.exempt is None


def test_the_budget_loads_from_org_yaml(fixtures, tmp_path):
    extra = "review_batch:\n  max_files: 12\n  max_lines: 400\n  exempt: ['**/*.snap']\n"
    batch = load_policy(policy_copy(fixtures, tmp_path, extra)).review_batch
    assert (batch.max_files, batch.max_lines, batch.exempt) == (12, 400, ["**/*.snap"])


@pytest.mark.parametrize(
    "extra",
    [
        "review_batch:\n  max_files: 0\n",  # 0 would read as "no limit" downstream
        "review_batch:\n  max_lines: -5\n",
        "review_batch:\n  max_files: 10\n  max_size: 3\n",
        "review_batch:\n  exempt: ['']\n",
    ],
)
def test_a_budget_that_would_mean_no_budget_is_an_error(fixtures, tmp_path, extra):
    with pytest.raises(PolicyError, match="review_batch"):
        load_policy(policy_copy(fixtures, tmp_path, extra))


@pytest.mark.parametrize(
    ("extra", "named"),
    [
        ("review_batch:\n  max_files: 100\n", "max_files may be at most 40"),
        ("review_batch:\n  max_lines: 5000\n", "max_lines may be at most 1500"),
    ],
)
def test_a_looser_budget_than_the_ceiling_is_an_error(fixtures, tmp_path, extra, named):
    """It would have no effect - the guard's own budget is a ceiling - so the admin is
    told at load instead of agents splitting work at 40 files under an org file that
    says 100 (F4)."""
    with pytest.raises(PolicyError, match=named):
        load_policy(policy_copy(fixtures, tmp_path, extra))


def test_the_ceiling_itself_is_allowed(fixtures, tmp_path):
    extra = "review_batch:\n  max_files: 40\n  max_lines: 1500\n"
    batch = load_policy(policy_copy(fixtures, tmp_path, extra)).review_batch
    assert (batch.max_files, batch.max_lines) == (40, 1500)
