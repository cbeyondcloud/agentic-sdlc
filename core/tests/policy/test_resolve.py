"""Tests for resolve_agent: an agent's requests checked against its type, org and team policy."""

from pathlib import Path

import pytest

from agentic.definitions import AgentDefinition, load_agent
from agentic.handoffs.schema import BUILTIN_CATALOG
from agentic.policy import OrgPolicy, ResolveContext, load_policy, resolve_agent
from agentic.policy.resolve import kind_violations


@pytest.fixture
def policy(fixtures: Path) -> OrgPolicy:
    return load_policy(fixtures / "policy" / "policy")


@pytest.fixture
def developer(fixtures: Path) -> AgentDefinition:
    return load_agent(fixtures / "agents" / "developer.md")


def agent_with(base: AgentDefinition, **changes) -> AgentDefinition:
    """A validated copy of an agent with some settings changed."""
    return AgentDefinition.model_validate({**base.model_dump(), **changes})


def minimal(name: str = "helper", type: str = "general", **settings) -> AgentDefinition:
    return AgentDefinition.model_validate(
        {"name": name, "type": type, "version": "1.0.0", "description": "x",
         "instructions": "Do it.", **settings}
    )  # fmt: skip


def violation_fields(violations) -> set[str]:
    return {violation.field for violation in violations}


# --- agent types: inheritance and ceilings (D35) ---


def test_unset_settings_are_inherited_from_the_type(policy):
    resolved, violations = resolve_agent(minimal(type="developer"), policy, ResolveContext())

    assert violations == []
    assert resolved.type == "developer"
    assert resolved.tier == "balanced" and resolved.model == "anthropic/claude-sonnet"
    assert resolved.backend == "native"  # the type's first backend
    assert "edit_files" in resolved.tools
    assert resolved.produces == "dev-report"
    assert resolved.consumes == ["design", "review"]
    assert resolved.limits.budget_usd == 5.0  # the type's cap
    assert resolved.children.max_total == 5


def test_unknown_type_is_refused(policy):
    resolved, violations = resolve_agent(minimal(type="wizard"), policy, ResolveContext())

    assert resolved is None
    assert violation_fields(violations) == {"type"}


def test_tool_beyond_the_type_ceiling_is_refused(policy):
    agent = minimal(type="general", tools=["read_files", "run_shell"])  # general has no run_shell

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert resolved is None
    assert [v.requested for v in violations if v.field == "tools"] == ["run_shell"]


def test_shell_beyond_the_type_ceiling_is_refused(policy, developer):
    agent = agent_with(developer, shell=["pytest", "docker run"])  # docker not in developer's list

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert resolved is None
    assert [v.requested for v in violations if v.field == "shell"] == ["docker run"]


def test_shell_entry_narrower_than_a_type_prefix_is_allowed(policy, developer):
    agent = agent_with(developer, shell=["pytest -q", "git status"])  # within "pytest" and "git"

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert violations == []
    assert resolved.shell == ["pytest -q", "git status"]


def test_backend_beyond_the_type_ceiling_is_refused(policy):
    agent = minimal(type="general", backend="claude-code")  # general allows native only

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert resolved is None
    assert "backend" in violation_fields(violations)


def test_skill_beyond_the_type_ceiling_is_refused(policy, developer):
    agent = agent_with(developer, skills=["someone/unvetted-skill"])

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert resolved is None
    assert "skills" in violation_fields(violations)


def test_consumes_beyond_the_type_ceiling_is_refused(policy, developer):
    agent = agent_with(developer, consumes=["design", "test-report"])

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert resolved is None
    assert "consumes" in violation_fields(violations)


def test_produces_must_match_the_type(policy, developer):
    agent = agent_with(developer, produces="review")  # developer type produces dev-report

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert resolved is None
    assert "produces" in violation_fields(violations)


def test_name_does_not_choose_the_caps(policy):
    # Review finding 6: calling an agent "developer" must not earn developer caps.
    agent = minimal(name="developer", type="general", limits={"budget_usd": 50})

    resolved, _ = resolve_agent(agent, policy, ResolveContext())

    assert resolved.limits.budget_usd == 1.0  # the general type's cap


# --- limits: min(request, type cap, team cap) ---


def test_limits_clamped_to_the_type_cap(policy, developer):
    agent = agent_with(developer, limits={"budget_usd": 50, "max_turns": 80, "max_minutes": 60})

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert violations == []  # a too-high number is clamped, not refused
    assert resolved.limits.budget_usd == 5.0


def test_team_limit_narrows_the_type_cap(policy, developer):
    resolved, _ = resolve_agent(developer, policy, ResolveContext(team="payments"))

    assert resolved.limits.budget_usd == 3.0  # payments lowers developer to $3


def test_children_clamped_to_the_type_cap(policy, developer):
    children = {"max_total": 10, "max_concurrent": 5, "max_depth": 4, "per_child": {}}
    agent = agent_with(developer, children=children)

    resolved, _ = resolve_agent(agent, policy, ResolveContext())

    assert (resolved.children.max_total, resolved.children.max_concurrent) == (5, 2)
    assert resolved.children.max_depth == 2
    assert resolved.children.per_child.budget_usd == 1.0


def test_type_without_children_cannot_spawn(policy):
    agent = minimal(type="general", children={"max_total": 3})

    resolved, _ = resolve_agent(agent, policy, ResolveContext())

    assert resolved.children.max_total == 0  # general's cap is 0


# --- models and tiers ---


def test_tier_maps_to_model_with_tier_defaults(policy, developer):
    resolved, violations = resolve_agent(
        agent_with(developer, effort=None), policy, ResolveContext()
    )

    assert violations == []
    assert resolved.model == "anthropic/claude-sonnet"
    assert resolved.effort == "medium"  # from the balanced tier
    assert resolved.classification == "internal"


def test_team_override_maps_tier(policy, developer):
    policy.teams["payments"].allowed_tiers = None  # isolate the override from tier narrowing

    resolved, violations = resolve_agent(
        agent_with(developer, tier="deep-reasoning"), policy, ResolveContext(team="payments")
    )

    assert violations == []
    assert resolved.model == "openai/gpt-5"


def test_team_cannot_loosen(policy, developer):
    agent = agent_with(developer, tier="deep-reasoning")  # payments allows only balanced, fast

    resolved, violations = resolve_agent(agent, policy, ResolveContext(team="payments"))

    assert resolved is None
    assert "tier" in violation_fields(violations)


def test_explicit_model_cannot_dodge_team_tiers(policy, developer):
    # Review finding 1: payments may not use deep-reasoning, so naming its model is refused.
    agent = agent_with(developer, model="anthropic/claude-opus")

    resolved, violations = resolve_agent(agent, policy, ResolveContext(team="payments"))

    assert resolved is None
    assert "model" in violation_fields(violations)


def test_explicit_model_cannot_borrow_another_teams_override(policy, developer):
    # Review finding 1: gpt-5 is payments' private override, not an org model.
    agent = agent_with(developer, model="openai/gpt-5")

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert resolved is None
    assert "model" in violation_fields(violations)


def test_explicit_model_of_an_allowed_tier_is_used(policy, developer):
    agent = agent_with(developer, model="google/gemini-flash")  # the fast tier's model

    resolved, violations = resolve_agent(agent, policy, ResolveContext(team="payments"))

    assert violations == []
    assert resolved.model == "google/gemini-flash"


def test_explicit_model_does_not_take_another_models_tier_defaults(policy, developer):
    agent = agent_with(developer, model="google/gemini-flash", effort=None)

    resolved, _ = resolve_agent(agent, policy, ResolveContext())

    assert resolved.effort is None  # balanced's "medium" belongs to claude-sonnet, not gemini


# --- teams and repo classes ---


def test_unknown_team_is_refused(policy, developer):
    # Review finding 2: a misspelt team used to drop every team restriction.
    resolved, violations = resolve_agent(developer, policy, ResolveContext(team="paymnets"))

    assert resolved is None
    assert violation_fields(violations) == {"team"}


def test_restricted_classification_blocks_backend(policy, developer):
    agent = agent_with(developer, tier="local", backend="claude-code")

    resolved, violations = resolve_agent(
        agent, policy, ResolveContext(repo_classification="restricted")
    )

    assert resolved is None
    assert violation_fields(violations) == {"backend"}


def test_restricted_classification_allows_local_model(policy, developer):
    resolved, violations = resolve_agent(
        agent_with(developer, tier="local"),
        policy,
        ResolveContext(repo_classification="restricted"),
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
    resolved, violations = resolve_agent(
        agent_with(developer, tools=["read_files", "teleport"]), policy, ResolveContext()
    )

    assert resolved is None
    assert "tools" in violation_fields(violations)


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

    assert resolved.shell == ["pytest", "git"]


# --- several problems at once ---


def test_all_violations_reported_together(policy, developer):
    agent = agent_with(developer, tier="deep-reasoning", tools=["web_fetch"], backend="codex")

    resolved, violations = resolve_agent(
        agent, policy, ResolveContext(team="payments", repo_classification="restricted")
    )

    assert resolved is None
    assert {"tier", "tools", "backend"} <= violation_fields(violations)
    assert all(violation.reason for violation in violations)


def test_kind_violations_cover_kinds_from_the_type(policy, developer):  # D37
    resolved, _ = resolve_agent(developer, policy, ResolveContext(team="payments"))
    agent = resolved.model_copy(update={"produces": "threat-model", "consumes": ["design", "poem"]})

    found = [(v.field, v.requested) for v in kind_violations(agent, BUILTIN_CATALOG)]
    assert found == [("produces", "threat-model"), ("consumes", "poem")]
    assert kind_violations(resolved, BUILTIN_CATALOG) == []


def test_an_explicit_model_does_not_inherit_tier_effort_or_temperature(policy):  # DF-01
    policy.tiers["balanced"] = policy.tiers["balanced"].model_copy(update={"temperature": 0.3})
    agent = minimal(type="developer", tier="balanced", model="google/gemini-flash")

    resolved, violations = resolve_agent(agent, policy, ResolveContext())

    assert violations == []
    assert (resolved.model, resolved.effort, resolved.temperature) == (
        "google/gemini-flash",
        None,
        None,
    )
