from pathlib import Path

import pytest

from agentic.definitions import load_agent
from agentic.policy import OrgPolicy, ResolveContext, ResolvedAgent, load_policy, resolve_agent


@pytest.fixture
def policy(fixtures: Path) -> OrgPolicy:
    return load_policy(fixtures / "policy" / "policy")


@pytest.fixture
def resolved(fixtures: Path, policy: OrgPolicy) -> ResolvedAgent:
    """The fixture Developer agent, resolved for the payments team."""
    agent, violations = resolve_agent(
        load_agent(fixtures / "agents" / "developer.md"), policy, ResolveContext(team="payments")
    )
    assert agent is not None, violations
    return agent
