"""Tests for resolve_agent: what an agent asks for, checked against org and team policy."""

from pathlib import Path

import pytest

from agentic.definitions import AgentDefinition, load_agent
from agentic.policy import OrgPolicy, ResolveContext, load_policy, resolve_agent


@pytest.fixture
def policy(fixtures: Path) -> OrgPolicy:
    return load_policy(fixtures / "policy" / "policy")


@pytest.fixture
def developer(fixtures: Path) -> AgentDefinition:
    return load_agent(fixtures / "agents" / "developer.md")


def agent_with(base: AgentDefinition, **changes) -> AgentDefinition:
    """A validated copy of an agent with some settings changed."""
    return AgentDefinition.model_validate({**base.model_dump(), **changes})


def violation_fields(violations) -> set[str]:
    return {violation.field for violation in violations}


# --- models and tiers ---


def test_tier_maps_to_model_with_tier_defaults(policy, developer):
    agent = agent_with(developer, effort=None)

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert violations == []
    assert resolved.model == "anthropic/claude-sonnet"
    assert resolved.effort == "medium"  # from the balanced tier, since the agent left it unset
    assert resolved.classification == "internal"  # org default


def test_team_override_maps_tier(policy, developer):
    policy.teams["payments"].allowed_tiers = None  # isolate the override from tier narrowing
    agent = agent_with(developer, tier="deep-reasoning")

    resolved, violations = resolve_agent(agent, policy, ResolveContext(team="payments"))

    assert violations == []
    assert resolved.model == "openai/gpt-5"


def test_team_cannot_loosen(policy, developer):
    agent = agent_with(developer, tier="deep-reasoning")  # payments allows only balanced, fast

    resolved, violations = resolve_agent(agent, policy, ResolveContext(team="payments"))

    assert resolved is None
    assert "tier" in violation_fields(violations)


def test_explicit_model_must_be_approved(policy, developer):
    agent = agent_with(developer, model="someone/unvetted-model")

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert resolved is None
    assert "model" in violation_fields(violations)


def test_explicit_approved_model_is_used(policy, developer):
    agent = agent_with(developer, model="google/gemini-flash")  # a model the org's tiers use

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert violations == []
    assert resolved.model == "google/gemini-flash"


# --- repo classification ---


def test_restricted_classification_blocks_backend(policy, developer):
    agent = agent_with(developer, tier="local", backend="claude-code")

    resolved, violations = resolve_agent(
        agent, policy, ResolveContext(repo_classification="restricted")
    )

    assert resolved is None
    assert violation_fields(violations) == {"backend"}


def test_restricted_classification_allows_local_model(policy, developer):
    agent = agent_with(developer, tier="local")

    resolved, violations = resolve_agent(
        agent, policy, ResolveContext(repo_classification="restricted")
    )

    assert violations == []
    assert resolved.model == "ollama/qwen-coder"


# --- tools ---


def test_forbidden_tool_violation(policy, developer):
    agent = agent_with(developer, tools=["read_files", "web_fetch"])

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert resolved is None
    assert [v.requested for v in violations if v.field == "tools"] == ["web_fetch"]


def test_tool_outside_catalog_violation(policy, developer):
    agent = agent_with(developer, tools=["read_files", "teleport"])

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert resolved is None
    assert "tools" in violation_fields(violations)


# --- limits ---


def test_limits_clamped_to_role_max(policy, developer):
    agent = agent_with(developer, limits={"budget_usd": 50, "max_turns": 80, "max_minutes": 60})

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert violations == []  # a too-high number is clamped, not refused
    assert resolved.limits.budget_usd == 5.0  # developer role maximum


def test_missing_limit_takes_role_max(policy, developer):
    agent = agent_with(developer, limits={})

    resolved, _ = resolve_agent(agent, policy, ResolveContext())

    assert resolved.limits.budget_usd == 5.0
    assert resolved.limits.max_turns == 80


def test_team_limit_narrows_further(policy, developer):
    resolved, _ = resolve_agent(developer, policy, ResolveContext(team="payments"))

    assert resolved.limits.budget_usd == 3.0  # payments caps the developer at $3


def test_unknown_agent_role_uses_default_limits(policy, developer):
    agent = agent_with(developer, name="docs-writer", limits={})

    resolved, _ = resolve_agent(agent, policy, ResolveContext())

    assert resolved.limits.budget_usd == 2.0  # policy default role limits


def test_children_clamped(policy, developer):
    children = {"max_total": 10, "max_concurrent": 5, "max_depth": 4, "per_child": {}}
    agent = agent_with(developer, children=children)

    resolved, _ = resolve_agent(agent, policy, ResolveContext())

    assert (resolved.children.max_total, resolved.children.max_concurrent) == (5, 2)
    assert resolved.children.max_depth == 2
    assert resolved.children.per_child.budget_usd == 1.0


# --- deny lists (D31) ---


def test_deny_lists_are_merged(policy, developer):
    resolved, _ = resolve_agent(developer, policy, ResolveContext(team="payments"))

    assert resolved.deny_shell == [
        "git push --force",  # org
        "rm -rf /",  # org
        "kubectl",  # team payments
        "git push",  # agent
        "git commit --amend",  # agent
    ]
    assert resolved.deny_paths == [".git/**", ".github/**", "infra/**"]


def test_denied_command_removed_from_allowed(policy, developer):
    agent = agent_with(developer, shell=["pytest", "git push origin main", "git"])

    resolved, _ = resolve_agent(agent, policy, ResolveContext())

    # "git push origin main" is covered by the agent's deny "git push": dropped.
    # "git" stays: the deny list narrows it at run time instead.
    assert resolved.shell == ["pytest", "git"]


# --- several problems at once ---


def test_all_violations_reported_together(policy, developer):
    agent = agent_with(developer, tier="deep-reasoning", tools=["web_fetch"], backend="codex")

    resolved, violations = resolve_agent(
        agent, policy, ResolveContext(team="payments", repo_classification="restricted")
    )

    assert resolved is None
    assert {"tier", "tools", "backend"} <= violation_fields(violations)
    assert all(violation.reason for violation in violations)  # each says why
