"""Run health in org policy (M3-07): `org.yaml: run_health` sets how often a running CLI
sends a heartbeat (default every 60 s) and how long the control plane waits without one
before it marks the run stale and frees its concurrency slot (default 30 minutes). The
window must span at least three beats, so one lost beat never makes a zombie."""

import shutil
from pathlib import Path

import pytest

from agentic.policy import PolicyError, load_policy
from agentic.policy.model import RunHealthPolicy


def policy_copy(fixtures: Path, tmp_path: Path, org_extra: str) -> Path:
    root = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", root)
    org = root / "org.yaml"
    org.write_text(org.read_text(encoding="utf-8") + org_extra, encoding="utf-8")
    return root


def test_run_health_defaults_to_a_beat_a_minute_and_thirty_minutes(fixtures):
    policy = load_policy(fixtures / "policy" / "policy")
    assert policy.run_health == RunHealthPolicy()
    assert (policy.run_health.heartbeat_seconds, policy.run_health.stale_minutes) == (60, 30)


def test_run_health_is_read_from_org_yaml(fixtures, tmp_path):
    org = "run_health:\n  heartbeat_seconds: 30\n  stale_minutes: 10\n"
    policy = load_policy(policy_copy(fixtures, tmp_path, org))
    assert (policy.run_health.heartbeat_seconds, policy.run_health.stale_minutes) == (30, 10)


def test_a_window_shorter_than_three_beats_is_refused(fixtures, tmp_path):
    org = "run_health:\n  heartbeat_seconds: 300\n  stale_minutes: 10\n"
    with pytest.raises(PolicyError, match=r"stale_minutes \(10\).*heartbeat_seconds \(300\)"):
        load_policy(policy_copy(fixtures, tmp_path, org))


@pytest.mark.parametrize(
    "org",
    [
        "run_health:\n  heartbeat_seconds: 5\n",
        "run_health:\n  stale_minutes: 2\n",
        "run_health:\n  stale_minutes: 2000\n",
        "run_health:\n  every: 60\n",
    ],
)
def test_out_of_range_or_unknown_run_health_is_refused(fixtures, tmp_path, org):
    with pytest.raises(PolicyError, match="run_health"):
        load_policy(policy_copy(fixtures, tmp_path, org))
