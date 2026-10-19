"""Tests for the child spawner: admin limits, narrowing only, partial results (M1-17, spec §8.4)."""

import asyncio
from decimal import Decimal

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RunUsage

from agentic.agents.children import ChildSpawner, ChildSpec
from agentic.agents.metering import CostLog
from agentic.agents.runner import AgentOutcome, AgentTask
from agentic.definitions import ChildLimits, Limits
from agentic.transport import BudgetLedger, LimitHit

D = Decimal


class FakeRunner:
    """Records what the child was given; finishes after `delay`, spending `spend`.
    With `nested`, it first spawns a grandchild through its own spawner."""

    active = 0
    peak = 0

    def __init__(
        self,
        agent,
        budget,
        spawner,
        *,
        delay=0.0,
        spend="0.01",
        stopped=None,
        status=None,
        nested=None,
    ):
        self.agent, self.budget, self.spawner = agent, budget, spawner
        self.depth = spawner.depth
        self.delay, self.spend, self.stopped = delay, D(spend), stopped
        self.status, self.nested, self.grandchild = status, nested, None

    async def run(self, task: AgentTask) -> AgentOutcome:
        FakeRunner.active += 1
        FakeRunner.peak = max(FakeRunner.peak, FakeRunner.active)
        try:
            if self.nested:
                self.grandchild = await self.spawner.spawn(spec(self.nested))
            await asyncio.sleep(self.delay)
            reservation = self.budget.reserve(self.spend)
            reservation._slice._settle(self.spend, self.spend, False)  # charge it
            reservation._done = True
        finally:
            FakeRunner.active -= 1
        status = self.status or ("stopped" if self.stopped else "completed")
        return AgentOutcome(
            status=status,
            output=f"{self.agent.definition.name} did it",
            stopped=self.stopped,
            usage=RunUsage(),
            costs=CostLog(),
        )

    async def resume(self, state_path, decisions, expected_digest):  # pragma: no cover
        raise NotImplementedError


def limits(**changes) -> ChildLimits:
    base = {
        "max_total": 3,
        "max_concurrent": 2,
        "max_depth": 1,
        "per_child": Limits(budget_usd=D("1"), max_turns=10, max_minutes=5),
    }
    return ChildLimits(**{**base, **changes})


def parent(resolved, **child_limits):
    return resolved.model_copy(
        update={
            "children": limits(**child_limits),
            "tools": ["read_files", "search", "spawn_agent"],
        }
    )


def spawner(agent, *, budget="5", **runner_kwargs) -> tuple[ChildSpawner, list]:
    made: list[FakeRunner] = []
    step = BudgetLedger(D("10")).run.open("review", "step", D(budget))

    def factory(child, slice_, child_spawner):
        runner = FakeRunner(child, slice_, child_spawner, **runner_kwargs)
        made.append(runner)
        return runner

    return ChildSpawner(agent, step, factory), made


def spec(name="security-review", **changes) -> ChildSpec:
    return ChildSpec(
        name=name, instructions="Review for security.", prompt="Check the diff.", **changes
    )


def spawn(s: ChildSpawner, child: ChildSpec = None):
    return asyncio.run(s.spawn(child or spec()))


# ── limits ────────────────────────────────────────────────────────────────────


def test_a_child_runs_and_reports_its_output_and_cost(resolved):
    s, _ = spawner(parent(resolved))
    result = spawn(s)

    assert (result.output, result.stopped, result.cost_usd) == (
        "security-review did it",
        None,
        D("0.01"),
    )


def test_max_total_is_enforced(resolved):
    s, made = spawner(parent(resolved, max_total=2))
    results = [spawn(s, spec(f"c{n}")) for n in range(3)]

    assert [r.stopped for r in results] == [None, None, "refused"]
    assert "max_total" in results[2].reason and len(made) == 2


def test_concurrent_spawns_cannot_exceed_max_total(resolved):  # Review Focus 5
    s, made = spawner(parent(resolved, max_total=3, max_concurrent=8), delay=0.05)

    async def many():
        return await asyncio.gather(*(s.spawn(spec(f"c{n}")) for n in range(8)))

    results = asyncio.run(many())
    assert sum(r.stopped is None for r in results) == 3 and len(made) == 3


def test_max_concurrent_is_enforced(resolved):
    FakeRunner.active = FakeRunner.peak = 0
    s, _ = spawner(parent(resolved, max_total=8, max_concurrent=2), delay=0.05)

    async def many():
        return await asyncio.gather(*(s.spawn(spec(f"c{n}")) for n in range(8)))

    asyncio.run(many())
    assert FakeRunner.peak == 2


def test_max_depth_is_enforced(resolved):
    s, made = spawner(parent(resolved, max_depth=0))  # no level of children allowed
    result = spawn(s)

    assert result.stopped == "refused" and "max_depth" in result.reason and made == []


def test_no_children_allowed_means_refused(resolved):
    s, _ = spawner(parent(resolved, max_total=0))

    assert spawn(s).stopped == "refused"


# ── narrowing ─────────────────────────────────────────────────────────────────


def test_per_child_budget_turns_and_time_slice_the_parent(resolved):
    s, made = spawner(parent(resolved))
    spawn(s, spec(limits=Limits(budget_usd=D("5"), max_turns=50, max_minutes=1)))

    child = made[0]
    assert child.budget.limit_usd == D("1")  # per_child caps the request
    assert (child.agent.limits.max_turns, child.agent.limits.max_minutes) == (10, 1)
    assert child.depth == 1


def test_a_child_cannot_widen_its_tools(resolved):
    s, made = spawner(parent(resolved))
    result = spawn(s, spec(tools=["read_files", "run_shell"]))

    assert result.stopped == "refused" and "run_shell" in result.reason and made == []


def test_a_child_may_narrow_its_tools(resolved):
    s, made = spawner(parent(resolved))
    spawn(s, spec(tools=["read_files"]))

    assert made[0].agent.tools == ["read_files"]


def test_child_deny_is_the_union_of_parent_and_own(resolved):
    agent = parent(resolved).model_copy(update={"deny_shell": ["git push"], "deny_paths": [".env"]})
    s, made = spawner(agent)
    spawn(s, spec(deny_shell=["curl"], deny_paths=["infra/**"]))

    assert made[0].agent.deny_shell == ["git push", "curl"]
    assert made[0].agent.deny_paths == [".env", "infra/**"]


def test_a_parent_with_no_budget_left_refuses_the_child(resolved):
    s, made = spawner(parent(resolved), budget="0.5")
    s.parent_budget.open("sibling", "child", D("0.5"))  # a sibling holds all of it
    result = spawn(s)

    assert result.stopped == "refused" and "budget" in result.reason and made == []


# ── partial results and limit hits ────────────────────────────────────────────


def test_a_child_limit_hit_returns_a_partial_result_marked_stopped(resolved):
    s, _ = spawner(parent(resolved), stopped="budget")
    result = spawn(s)

    assert (result.stopped, result.output) == ("budget", "security-review did it")


def test_a_step_limit_hit_inside_a_child_reaches_the_engine(resolved):
    s, _ = spawner(parent(resolved))

    async def boom(task):
        raise LimitHit("step", "review", D("1"), D("1.5"))

    async def run():
        result = s.spawn(spec())
        return await result

    s.runner_factory = lambda child, slice_, spawner: type("R", (), {"run": staticmethod(boom)})()
    with pytest.raises(LimitHit):
        asyncio.run(run())


# ── the spawn_agent tool ──────────────────────────────────────────────────────


def test_spawn_agent_is_offered_only_when_allowed(resolved):
    allowed, _ = spawner(parent(resolved))
    no_tool, _ = spawner(parent(resolved).model_copy(update={"tools": ["read_files"]}))
    no_children, _ = spawner(parent(resolved, max_total=0))

    assert [t.name for t in allowed.tools()] == ["spawn_agent"]
    assert no_tool.tools() == [] and no_children.tools() == []


def test_the_spawn_agent_tool_returns_the_childs_result_to_the_model(resolved):
    s, _ = spawner(parent(resolved))

    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if returns:
            return ModelResponse(parts=[TextPart(str(returns[-1].content))])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "spawn_agent",
                    {
                        "name": "perf-review",
                        "instructions": "Look at performance.",
                        "prompt": "Go.",
                    },
                )
            ]
        )

    output = asyncio.run(Agent(FunctionModel(model), tools=s.tools()).run("review")).output
    assert "perf-review did it" in output and "completed" in output


# ── review fixes: paused children (I5), failed factories (I5), per-tree caps (M4), names (M12)


def test_a_paused_child_is_reported_as_paused_not_completed(resolved):
    s, _ = spawner(parent(resolved), status="paused")
    result = spawn(s)

    assert result.stopped == "paused" and "approval" in result.reason


def test_a_failing_factory_leaks_no_slice_and_no_spawn(resolved):
    s, made = spawner(parent(resolved, max_total=1), budget="1")
    good = s.runner_factory

    def broken(child, slice_, child_spawner):
        raise RuntimeError("cannot build the child")

    s.runner_factory = broken
    with pytest.raises(RuntimeError):
        spawn(s)
    s.runner_factory = good

    assert spawn(s).stopped is None and len(made) == 1  # the failed spawn did not count
    s.parent_budget.open("all", "child", D("0.99"))  # and its slice was given back


def test_max_total_holds_across_the_whole_tree(resolved):
    s, made = spawner(parent(resolved, max_total=2, max_depth=2), nested="grand")
    spawn(s, spec("c0"))  # c0 and its grandchild: two agents in the tree

    assert spawn(s, spec("c1")).stopped == "refused" and len(made) == 2


def test_max_depth_holds_for_grandchildren(resolved):
    s, made = spawner(parent(resolved, max_depth=1), nested="grand")
    spawn(s)

    assert made[0].grandchild.stopped == "refused" and "max_depth" in made[0].grandchild.reason


def test_a_nested_spawn_is_refused_rather_than_waiting_for_a_slot(resolved):
    s, made = spawner(parent(resolved, max_concurrent=1, max_depth=2), nested="grand")

    async def bounded():
        return await asyncio.wait_for(s.spawn(spec()), 5)  # waiting would deadlock

    assert asyncio.run(bounded()).stopped is None
    assert made[0].grandchild.stopped == "refused"
    assert "max_concurrent" in made[0].grandchild.reason


@pytest.mark.parametrize("name", ["../escape", "Security Review", "a" * 41, "", "-lead"])
def test_the_model_cannot_choose_any_child_name(resolved, name):
    s, made = spawner(parent(resolved))
    result = spawn(s, spec(name))

    assert result.stopped == "refused" and "name" in result.reason and made == []
