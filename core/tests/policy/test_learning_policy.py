"""Learning-loop thresholds in org policy (M3-13): `org.yaml: learning` sets how many similar
corrections, in how many days, make a proposal (default 3 in 30) and how alike two lessons
must be; a team's `teams/<team>.yaml: learning` may only tighten them (a higher threshold,
a shorter window)."""

import shutil
from pathlib import Path

import pytest

from agentic.learning.clustering import LearningSettings
from agentic.policy import PolicyError, load_policy
from agentic.policy.model import LearningPolicy


def policy_copy(fixtures: Path, tmp_path: Path, org_extra: str = "", team_extra: str = "") -> Path:
    root = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", root)
    for path, extra in (
        (root / "org.yaml", org_extra),
        (root / "teams" / "payments.yaml", team_extra),
    ):
        path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return root


def test_learning_defaults_to_three_in_thirty_days(fixtures):
    policy = load_policy(fixtures / "policy" / "policy")
    assert policy.learning == LearningPolicy()
    assert policy.learning.settings_for(None) == LearningSettings(3, 30, 0.5)
    assert policy.learning_settings("payments") == LearningSettings(3, 30, 0.5)


def test_team_may_tighten_learning(fixtures, tmp_path):
    org = "learning:\n  threshold: 4\n  window_days: 21\n  similarity: 0.6\n"
    team = "learning:\n  threshold: 5\n  window_days: 14\n"
    policy = load_policy(policy_copy(fixtures, tmp_path, org, team))
    assert policy.learning_settings("payments") == LearningSettings(5, 14, 0.6)
    assert policy.learning_settings("unknown-team") == LearningSettings(4, 21, 0.6)


@pytest.mark.parametrize(
    ("team", "named"),
    [
        ("learning:\n  threshold: 2\n", "teams.payments.learning.threshold"),
        ("learning:\n  window_days: 60\n", "teams.payments.learning.window_days"),
    ],
)
def test_team_may_only_tighten_learning(fixtures, tmp_path, team, named):
    with pytest.raises(PolicyError, match=named):
        load_policy(policy_copy(fixtures, tmp_path, "", team))


@pytest.mark.parametrize(
    "extra",
    [
        "learning:\n  threshold: 1\n",  # one correction is `learn propose --from`, not a pattern
        "learning:\n  window_days: 0\n",
        "learning:\n  similarity: 0\n",
        "learning:\n  similarity: 1.5\n",
    ],
)
def test_nonsense_learning_settings_are_refused(fixtures, tmp_path, extra):
    with pytest.raises(PolicyError, match="learning"):
        load_policy(policy_copy(fixtures, tmp_path, extra))
