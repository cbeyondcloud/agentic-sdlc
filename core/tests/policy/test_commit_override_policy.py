"""The commit-command override in org policy (M2-61, spec §9.2): a repository whose raw git
is unsafe names its own commit tool, and org policy decides which commands may run at all."""

import shutil
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


def test_no_override_is_allowed_by_default(policy_dir: Path):
    policy = load_policy(policy_dir)
    assert policy.commit_override.allow == []
    assert policy.commit_override.timeout_s == 600


def test_org_yaml_allows_override_commands_by_prefix(policy_dir: Path):
    _append(
        policy_dir / "org.yaml",
        "commit_override:\n  allow: ['monotool commit', 'monotool restore']\n  timeout_s: 120\n",
    )
    policy = load_policy(policy_dir)
    assert policy.commit_override.allow == ["monotool commit", "monotool restore"]
    assert policy.commit_override.timeout_s == 120


@pytest.mark.parametrize(
    "rule",
    [
        "",  # allows every command
        "   ",
        "monotool commit {message}",  # a placeholder is a value, never part of a rule
        "monotool\ncommit",
    ],
)
def test_an_unusable_allow_rule_is_refused(policy_dir: Path, rule: str):
    quoted = rule.replace("'", "''").replace("\n", "\\n")
    _append(policy_dir / "org.yaml", f'commit_override:\n  allow: ["{quoted}"]\n')
    with pytest.raises(PolicyError, match="commit_override"):
        load_policy(policy_dir)
