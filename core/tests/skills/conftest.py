"""Shared fixtures for the skills tests."""

from pathlib import Path

import pytest
from _skill_helpers import entry, make_skill, write_catalog

from agentic.definitions import load_agent
from agentic.policy import ResolveContext, ResolvedAgent, load_policy, resolve_agent
from agentic.skills import skill_digest


@pytest.fixture
def resolved(fixtures: Path) -> ResolvedAgent:
    """The fixture Developer agent, resolved for the payments team against the fixture policy."""
    policy = load_policy(fixtures / "policy" / "policy")
    agent, violations = resolve_agent(
        load_agent(fixtures / "agents" / "developer.md"), policy, ResolveContext(team="payments")
    )
    assert agent is not None, violations
    return agent


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """A small catalog folder: two agent skills, one authoring skill, one pack skill."""
    root = tmp_path / "catalog"
    specs = {
        "superpowers/test-driven-development": {"loading": "pre"},
        "superpowers/systematic-debugging": {},
        "superpowers/writing-skills": {"audience": "admins"},
        "trailofbits/differential-review": {"pack": "security-pack"},
    }
    entries = {}
    for key, overrides in specs.items():
        folder = make_skill(root, key)
        entries[key] = entry(skill_digest(folder), **overrides)
    write_catalog(root, entries)
    return root
