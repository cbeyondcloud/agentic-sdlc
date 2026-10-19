"""Tests for the production step-runner factory (SP6 Task 7, DF-28)."""

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from agentic.agents.models import GatewaySettings
from agentic.definitions import load_agent
from agentic.engine.engine import StepContext
from agentic.engine.factory import PydanticStepRunners
from agentic.policy import ResolveContext, load_policy, resolve_agent
from agentic.transport import BudgetLedger


@pytest.fixture
def developer(fixtures: Path):
    policy = load_policy(fixtures / "policy" / "policy")
    agent, violations = resolve_agent(
        load_agent(fixtures / "agents" / "developer.md"), policy, ResolveContext(team="payments")
    )
    assert agent is not None, violations
    return agent


def factory(tmp_path: Path, **changes) -> PydanticStepRunners:
    settings = {
        "identity_for": lambda ctx: None,
        "gateway": GatewaySettings(base_url="https://tokenlean.example", api_key="k"),
        "prices": lambda model: None,
        "fallback_estimate_usd": Decimal("0.01"),
        "tools_for": lambda ctx: None,
        "state_dir_for": lambda ctx: tmp_path / ctx.step,
        "model_for": lambda ctx: TestModel(),
    }
    return PydanticStepRunners(**{**settings, **changes})


def context(agent, step="develop") -> StepContext:
    budget = BudgetLedger(Decimal("5")).run.open(f"{step}-1", "step", Decimal("2"))
    return StepContext(
        run_id="r-1", step=step, iteration=1, agent=agent, budget=budget, contract=None
    )


def test_an_agent_allowed_to_spawn_gets_the_spawn_agent_tool(developer, tmp_path):  # DF-28
    runner = factory(tmp_path)(context(developer))

    assert "spawn_agent" in [tool.name for tool in runner.tools]
    assert runner.state_dir == tmp_path / "develop" and runner.run_id == "r-1"


def test_an_agent_without_spawn_agent_gets_no_spawn_tool(developer, tmp_path):
    no_spawn = developer.model_copy(update={"tools": ["read_files"]})

    assert factory(tmp_path)(context(no_spawn)).tools == []


def test_a_child_runner_has_no_contract_and_its_own_state(developer, tmp_path):
    runners = factory(tmp_path)
    ctx = context(developer)
    root = runners.root_spawner(developer, ctx)
    child_budget = ctx.budget.open("security-review", "child", Decimal("0.5"))
    child_spawner = root.for_child(developer, child_budget)

    child = runners.child_runner(developer, child_budget, child_spawner, ctx)
    assert child.contract is None and child_spawner.depth == 1
    assert child.state_dir == tmp_path / "develop" / "children" / "developer"
