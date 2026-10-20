"""Tests for skill selection: which approved skills a repo's user receives (M1-37)."""

from pathlib import Path

from agentic.definitions import AgentDefinition, DefinitionSet, load_agent, load_pipeline
from agentic.policy import ResolveContext, load_policy
from agentic.skills import load_catalog
from agentic.skills.select import (
    ADMIN_ROLES,
    pipeline_agents,
    pipeline_skills,
    select_skills,
    team_packs,
)

TDD = "superpowers/test-driven-development"
DEBUG = "superpowers/systematic-debugging"
AUTHORING = "superpowers/writing-skills"
PACK = "trailofbits/differential-review"


def test_developer_never_receives_authoring_skills(library: Path):
    """Review Focus 2: not even when an agent definition lists the authoring skill."""
    catalog = load_catalog(library)
    chosen = select_skills(catalog, [TDD, AUTHORING], roles=["developer"])
    assert AUTHORING not in chosen.names
    assert chosen.names == (TDD,)
    assert select_skills(catalog, [TDD]).names == (TDD,)  # no roles at all


def test_admin_roles_receive_authoring_skills(library: Path):
    catalog = load_catalog(library)
    assert ADMIN_ROLES == {"org-admin", "team-lead"}
    for role in sorted(ADMIN_ROLES):
        assert AUTHORING in select_skills(catalog, [TDD], roles=["developer", role]).names


def test_opt_in_pack_absent_unless_enabled(library: Path):
    """Review Focus 2: an agent listing a pack skill does not get it without the pack."""
    catalog = load_catalog(library)
    assert PACK not in select_skills(catalog, [TDD, PACK]).names
    assert PACK not in select_skills(catalog, [TDD], packs=["another-pack"]).names


def test_enabled_pack_installs_all_its_skills(library: Path):
    catalog = load_catalog(library)
    assert select_skills(catalog, [TDD], packs=["security-pack"]).names == (TDD, PACK)


def test_skill_not_in_catalog_is_missing_not_installed(library: Path):
    catalog = load_catalog(library)
    chosen = select_skills(catalog, [TDD, "someone/unvetted-skill"])
    assert chosen.names == (TDD,)
    assert chosen.missing == ("someone/unvetted-skill",)


def test_selection_is_sorted_and_unique(library: Path):
    catalog = load_catalog(library)
    assert select_skills(catalog, [TDD, DEBUG, TDD]).names == (DEBUG, TDD)


def test_pipeline_agents_in_step_order_with_loops(fixtures: Path):
    pipeline = load_pipeline(fixtures / "pipelines" / "feature-delivery.yaml")
    assert pipeline_agents(pipeline) == [
        "business-analyst",
        "design",
        "developer",
        "code-review",
        "qa",
    ]


def test_pipeline_skills_union_of_resolved_agents(fixtures: Path):
    """The resolved skills (type defaults included) of every agent the pipeline runs;
    agents that are not defined or do not resolve add nothing (preflight refuses them)."""
    policy = load_policy(fixtures / "policy" / "policy")
    developer = load_agent(fixtures / "agents" / "developer.md")
    analyst = AgentDefinition(
        name="business-analyst",
        type="business-analyst",
        version="1.0.0",
        description="Writes requirements.",
        instructions="Write the requirements.",
    )
    pipeline = load_pipeline(fixtures / "pipelines" / "feature-delivery.yaml")
    definitions = DefinitionSet(
        agents={"developer": (developer, "org"), "business-analyst": (analyst, "org")},
        pipelines={"feature-delivery": (pipeline, "org")},
    )
    skills = pipeline_skills(
        definitions, ["feature-delivery"], policy, ResolveContext(team="payments")
    )
    assert skills == [
        "addyosmani/spec-driven-development",
        "superpowers/systematic-debugging",
        "superpowers/test-driven-development",
        "superpowers/verification-before-completion",
    ]


def test_team_packs_from_team_policy(fixtures: Path):
    policy = load_policy(fixtures / "policy" / "policy")
    assert team_packs(policy, "payments") == []
    assert team_packs(policy, None) == []
    team = policy.teams["payments"].model_copy(update={"skill_packs": ["security-pack"]})
    enabled = policy.model_copy(update={"teams": {"payments": team}})
    assert team_packs(enabled, "payments") == ["security-pack"]
    assert team_packs(enabled, "unknown-team") == []
