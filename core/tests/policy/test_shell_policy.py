"""Tests for the shell policy fields: allow / ask / forbid prefixes and the sandbox policy
(M1-31, DF-04; D31: restrictions merge as a union; D38: sandbox)."""

import shutil
from pathlib import Path

import pytest

from agentic.definitions import AgentDefinition
from agentic.definitions.agent import DenyRules
from agentic.policy import OrgPolicy, PolicyError, ResolveContext, load_policy, resolve_agent
from agentic.policy.resolve import _covers


@pytest.fixture
def policy(fixtures: Path) -> OrgPolicy:
    return load_policy(fixtures / "policy" / "policy")


def developer(**settings) -> AgentDefinition:
    return AgentDefinition.model_validate(
        {
            "name": "dev",
            "type": "developer",
            "version": "1.0.0",
            "description": "x",
            "instructions": "Do it.",
            **settings,
        }
    )


def policy_copy(fixtures: Path, tmp_path: Path, org_extra: str) -> Path:
    root = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", root)
    org = root / "org.yaml"
    org.write_text(org.read_text(encoding="utf-8") + org_extra, encoding="utf-8")
    return root


# --- _covers: word by word, alternatives, program spelling (DF-04) ---


def test_covers_reads_alternatives():
    assert _covers("git push|fetch", "git push origin main")
    assert _covers("git push|fetch", "git fetch")
    assert not _covers("git push|fetch", "git pull")


def test_covers_ignores_spacing_and_program_spelling():
    assert _covers("git  push", "git push")
    assert _covers("git push", "GIT.exe push --force")
    assert not _covers("git push", "git")
    assert not _covers("git push", "git pushy")


def test_deny_with_alternatives_drops_the_allow_prefix(policy):
    policy.deny = DenyRules(shell=["git push|fetch"])
    resolved, violations = resolve_agent(
        developer(shell=["git push", "pytest"]), policy, ResolveContext()
    )
    assert violations == []
    assert resolved.shell == ["pytest"]


# --- the ask list: a restriction, merged like the deny lists (D31) ---


def test_ask_lists_merge_as_a_union(policy):
    policy.deny = DenyRules(ask=["npm publish"])
    policy.teams["payments"].deny = DenyRules(ask=["pip install"])
    agent = developer(deny={"ask": ["npm publish"]})
    resolved, violations = resolve_agent(agent, policy, ResolveContext(team="payments"))
    assert violations == []
    assert resolved.ask_shell == ["npm publish", "pip install"]


def test_a_denied_command_is_not_also_an_ask(policy):
    policy.deny = DenyRules(shell=["npm"], ask=["npm publish"])
    resolved, _ = resolve_agent(developer(), policy, ResolveContext())
    assert resolved.ask_shell == []


# --- the sandbox policy (D38) ---


def test_sandbox_policy_defaults(policy):
    assert policy.sandbox.required_for == ["team", "regulated"]  # DF-72: owner decision
    assert policy.sandbox.srt_version == "0.0.78"
    assert policy.sandbox.effective_deny_read[0] == "~/.ssh"


def test_sandbox_deny_read_adds_to_the_floor(fixtures, tmp_path):
    root = policy_copy(fixtures, tmp_path, "sandbox:\n  deny_read: ['~/secrets', '~/.ssh']\n")
    policy = load_policy(root)
    reads = policy.sandbox.effective_deny_read
    assert reads[0] == "~/.ssh" and reads[-1] == "~/secrets"
    assert reads.count("~/.ssh") == 1


@pytest.mark.parametrize("bad", ["/", "~", "~/", "~/.ssh", "~/.ssh/keys", "~/cache/*"])
def test_allow_write_may_not_open_home_root_or_a_denied_read(fixtures, tmp_path, bad):
    root = policy_copy(fixtures, tmp_path, f"sandbox:\n  allow_write: ['{bad}']\n")
    with pytest.raises(PolicyError, match="allow_write entry .* may not"):
        load_policy(root)


def test_allow_write_accepts_a_cache_folder(fixtures, tmp_path):
    root = policy_copy(fixtures, tmp_path, "sandbox:\n  allow_write: ['~/.cache/uv']\n")
    assert load_policy(root).sandbox.allow_write == ["~/.cache/uv"]
