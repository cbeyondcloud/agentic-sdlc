"""Tests for the built-in agents in `library/agents/` (M1-60)."""

import asyncio
import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from _library_support import LIBRARY, VENDOR_NAMES
from pydantic_ai.models.test import TestModel

from agentic.agents.runner import AgentTask, PydanticAIRunner
from agentic.handoffs import parse_handoff
from agentic.handoffs.contract import HandoffContract
from agentic.handoffs.schema import render_template
from agentic.policy import ResolveContext, resolve_agent
from agentic.transport import BudgetLedger
from sdlc_commons.guards import Guard

#: agent file -> (agent type, the handoff kind it writes)
BUILTINS = {
    "business-analyst": ("business-analyst", "requirements"),
    "design": ("design", "design"),
    "developer": ("developer", "dev-report"),
    "code-review": ("code-review", "review"),
    "qa": ("qa", "test-report"),
    "qa-baseline": ("qa", "test-report"),  # the baseline test run at intake (Zyroflo P0/P5)
}
VENDORS = re.compile(VENDOR_NAMES, re.IGNORECASE)
FIXED = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def resolved(library, policy, name: str):
    agent, violations = resolve_agent(library.agent(name), policy, ResolveContext())
    assert agent is not None, [f"{v.field}: {v.reason}" for v in violations]
    return agent


def test_library_holds_exactly_the_builtin_agents(library):
    assert set(library.agents) == set(BUILTINS)


@pytest.mark.parametrize("name", sorted(BUILTINS))
def test_every_builtin_agent_resolves_against_the_standalone_policy(
    library, standalone_policy, name
):
    agent = resolved(library, standalone_policy, name)
    assert (agent.type, agent.produces) == BUILTINS[name]
    assert agent.definition.status == "active"


@pytest.mark.parametrize("name", sorted(BUILTINS))
def test_testmodel_smoke_produces_a_valid_handoff(library, standalone_policy, name, tmp_path):
    """Each agent, resolved, run with its real instructions and its step's output
    contract on a TestModel that answers with its kind's template: the answer is
    finalised into a handoff that parses as that kind."""
    agent = resolved(library, standalone_policy, name)
    schema = library.handoffs.schema(agent.produces)
    contract = HandoffContract(
        schema, "r-smoke-1", name, f"{name}@{agent.definition.version}", clock=lambda: FIXED
    )
    model = TestModel(custom_output_text=render_template(schema), call_tools=[])
    budget = BudgetLedger(Decimal("5")).run.open(name, "step", Decimal("1"))
    runner = PydanticAIRunner(agent, model, budget=budget, state_dir=tmp_path, contract=contract)
    task = AgentTask(instructions=agent.definition.instructions, inputs={}, prompt="Go.")

    outcome = asyncio.run(runner.run(task))

    assert outcome.status == "completed", outcome.errors
    handoff = parse_handoff(outcome.output, library.handoffs)
    assert handoff.envelope.kind == agent.produces
    assert handoff.envelope.agent == f"{name}@{agent.definition.version}"


def test_no_vendor_names_in_agents():
    found = {
        path.name: VENDORS.findall(path.read_text(encoding="utf-8"))
        for path in (LIBRARY / "agents").glob("*.md")
    }
    assert {name: hits for name, hits in found.items() if hits} == {}


def test_agents_ask_for_their_type_defaults_only(library, standalone_policy):
    """Built-ins stay portable: they leave tier, model and backend to the type, so an
    org's mapping (or a team override) applies to them unchanged."""
    for name in BUILTINS:
        agent = library.agent(name)
        assert (agent.tier, agent.model, agent.backend) == (None, None, None), name


@pytest.mark.parametrize(
    "command",
    [
        "git diff --output=../escape.txt",
        "git log --output=../escape.txt",
        "git show --output=x HEAD",
        "git diff > ../escape.txt",
        "git log >> src/app.py",
    ],
)
def test_the_reviewer_cannot_write_files_through_its_shell(
    library, standalone_policy, tmp_path, command
):
    """Review F3: Code Review reads untrusted diffs, so its read-only git commands must
    not write files (git's output options, shell redirects)."""
    guard = Guard.for_agent(
        tmp_path, "r-review", resolved(library, standalone_policy, "code-review")
    )
    assert guard.command("git diff HEAD~1") == []
    assert guard.command(command) != []


def test_review_loop_and_baseline_rules_are_in_the_instructions(library):
    review = library.agent("code-review").instructions
    assert "open_blocking" in review and "never" in review.lower()  # score never gates
    assert "baseline_failures" in library.agent("qa").instructions
    assert "baseline" in library.agent("qa-baseline").instructions.lower()
