"""Versions in org policy (M2-15, spec §12.5) and org-required steps (M2-23, spec §6.1).

`policy/rollout.yaml` rolls a new version of an agent or pipeline out team by team and
deprecates old versions; `policy/teams/<team>.yaml` may pin a version; `org.yaml` names the
steps and gates no overlay may remove."""

import shutil
from datetime import date
from pathlib import Path

import pytest

from agentic.policy import PolicyError, load_policy

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "policy" / "policy"


@pytest.fixture
def policy_dir(tmp_path: Path) -> Path:
    target = tmp_path / "policy"
    shutil.copytree(FIXTURE, target)
    return target


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


ROLLOUT = """\
rollout:
  agents:
    developer: {version: 1.3.0, previous: 1.2.0, teams: [payments]}
  pipelines:
    feature-delivery: {version: 2.0.0, previous: 1.0.0, teams: []}
deprecated:
  agents:
    developer:
      "1.1.0": {blocked_from: 2026-12-01, note: use 1.2.0 or later}
  skills:
    superpowers/brainstorming:
      "1.0.0": {blocked_from: 2027-01-15}
"""


def test_rollout_file_loads(policy_dir: Path):
    (policy_dir / "rollout.yaml").write_text(ROLLOUT, encoding="utf-8")
    policy = load_policy(policy_dir)
    entry = policy.rollout.agents["developer"]
    assert (entry.version, entry.previous, entry.teams) == ("1.3.0", "1.2.0", ["payments"])
    assert policy.rollout.pipelines["feature-delivery"].teams == []
    gone = policy.deprecated.agents["developer"]["1.1.0"]
    assert gone.blocked_from == date(2026, 12, 1) and gone.note == "use 1.2.0 or later"
    assert policy.deprecated.skills["superpowers/brainstorming"]["1.0.0"].note == ""


def test_no_rollout_file_means_no_rollout(policy_dir: Path):
    policy = load_policy(policy_dir)
    assert policy.rollout.agents == {} and policy.deprecated.pipelines == {}
    assert policy.required_steps == {}


def test_rollout_names_unknown_team_refused(policy_dir: Path):
    (policy_dir / "rollout.yaml").write_text(
        "rollout:\n  agents:\n    developer: {version: 2.0.0, previous: 1.0.0, teams: [ghosts]}\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyError, match="unknown team 'ghosts'"):
        load_policy(policy_dir)


def test_rollout_previous_must_differ(policy_dir: Path):
    (policy_dir / "rollout.yaml").write_text(
        "rollout:\n  agents:\n    developer: {version: 1.0.0, previous: 1.0.0, teams: []}\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyError, match="'previous' must differ from 'version'"):
        load_policy(policy_dir)


def test_unknown_rollout_kind_refused(policy_dir: Path):
    """Skills are deprecated by version, but not rolled out per team (no second installed
    copy of a skill yet): a `skills:` rollout must be an error, never silently ignored."""
    (policy_dir / "rollout.yaml").write_text(
        "rollout:\n  skills:\n    a/b: {version: 2.0.0, previous: 1.0.0, teams: []}\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyError, match="skills"):
        load_policy(policy_dir)


def test_versions_must_be_plain_names(policy_dir: Path):
    """A version becomes a file name in the archive (versions/<name>/<version>.md)."""
    (policy_dir / "rollout.yaml").write_text(
        "rollout:\n  agents:\n    developer: {version: ../x, previous: 1.0.0, teams: []}\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyError, match="plain name"):
        load_policy(policy_dir)


def test_team_pins_load(policy_dir: Path):
    _append(policy_dir / "teams" / "payments.yaml", "pins:\n  agents: {developer: 1.2.0}\n")
    policy = load_policy(policy_dir)
    assert policy.teams["payments"].pins.agents == {"developer": "1.2.0"}
    assert policy.teams["payments"].pins.pipelines == {}


def test_checkpoint_exclusions_extend_from_org_yaml(policy_dir: Path):
    """DF-315/DF-317: org policy adds generated folders a checkpoint leaves out (it cannot
    remove the framework's); git pathspec magic is refused, so an entry cannot re-include."""
    _append(policy_dir / "org.yaml", "checkpoint_exclude: ['**/target/**', 'build/**']\n")
    assert load_policy(policy_dir).checkpoint_exclude == ["**/target/**", "build/**"]
    _append(policy_dir / "org.yaml", "checkpoint_exclude: [':(top)src/**']\n")
    with pytest.raises(PolicyError, match="checkpoint_exclude"):
        load_policy(policy_dir)


def test_required_steps_load_from_org_yaml(policy_dir: Path):
    _append(
        policy_dir / "org.yaml",
        "required_steps:\n  feature-delivery: [build-review, review-approval]\n",
    )
    policy = load_policy(policy_dir)
    assert policy.required_steps == {"feature-delivery": ["build-review", "review-approval"]}
