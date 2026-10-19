"""Tests for AgentRunner: a resolved agent run with turn, time, budget and stuck limits (M1-15)."""

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agentic.agents.metering import CostLog
from agentic.agents.runner import AgentOutcome, AgentRunner, AgentTask, PydanticAIRunner
from agentic.transport import BudgetLedger, LimitHit

TASK = AgentTask(
    instructions="You are the Developer.",
    inputs={"design": "Use a queue."},
    prompt="Implement the design.",
)


def runner(resolved, model, tmp_path: Path, **kwargs) -> PydanticAIRunner:
    step = BudgetLedger(Decimal("5")).run.open("build", "step", Decimal("1"))
    return PydanticAIRunner(resolved, model, budget=step, state_dir=tmp_path, **kwargs)


def say(text: str) -> FunctionModel:
    return FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart(text)]))


def run(r: PydanticAIRunner, task: AgentTask = TASK) -> AgentOutcome:
    return asyncio.run(r.run(task))


def busy_tool() -> Tool:
    def look(path: str) -> str:
        """Look at a file."""
        return "nothing new"

    return Tool(look)


def always_calls_the_tool(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("look", {"path": "a.py"})])


# ── completing ────────────────────────────────────────────────────────────────


def test_a_run_completes_and_records_usage(resolved, tmp_path):
    outcome = run(runner(resolved, say("done: queue added"), tmp_path))

    assert (outcome.status, outcome.output, outcome.stopped) == (
        "completed",
        "done: queue added",
        None,
    )
    assert outcome.usage.requests == 1


def test_handoffs_reach_the_model_as_data_not_chat_history(resolved, tmp_path):
    seen: dict = {}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen["instructions"] = messages[0].instructions
        seen["requests"] = len(messages)
        return ModelResponse(parts=[TextPart("ok")])

    run(runner(resolved, FunctionModel(model), tmp_path))

    assert "You are the Developer." in seen["instructions"]
    assert '<handoff kind="design">\nUse a queue.\n</handoff>' in seen["instructions"]
    assert seen["requests"] == 1  # the prompt only - no earlier agent's conversation


def test_a_runner_satisfies_the_protocol(resolved, tmp_path):
    assert isinstance(runner(resolved, say("x"), tmp_path), AgentRunner)


# ── limits ────────────────────────────────────────────────────────────────────


def test_the_turn_limit_stops_with_turns(resolved, tmp_path):
    agent = resolved.model_copy(
        update={"limits": resolved.limits.model_copy(update={"max_turns": 2})}
    )
    r = runner(agent, FunctionModel(always_calls_the_tool), tmp_path, tools=[busy_tool()])

    outcome = run(r)
    assert (outcome.status, outcome.stopped) == ("stopped", "turns")


def test_the_time_limit_stops_with_time_and_keeps_partial_output(resolved, tmp_path):
    calls = {"n": 0}

    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(
                parts=[TextPart("working on it"), ToolCallPart("look", {"path": "a.py"})]
            )
        await asyncio.sleep(5)
        return ModelResponse(parts=[TextPart("never")])

    r = runner(resolved, FunctionModel(slow), tmp_path, tools=[busy_tool()], time_limit_s=0.3)

    outcome = run(r)
    assert (outcome.status, outcome.stopped, outcome.output) == ("stopped", "time", "working on it")


def raises(hit: LimitHit) -> FunctionModel:
    def model(messages, info):
        raise hit

    return FunctionModel(model)


def test_a_child_budget_hit_stops_with_budget(resolved, tmp_path):
    hit = LimitHit("child", "reviewer", Decimal("1"), Decimal("1"))

    outcome = run(runner(resolved, raises(hit), tmp_path, is_child=True))
    assert (outcome.status, outcome.stopped) == ("stopped", "budget")


def test_a_step_budget_hit_is_raised_for_the_engine(resolved, tmp_path):
    hit = LimitHit("step", "build", Decimal("1"), Decimal("1"))

    with pytest.raises(LimitHit):
        run(runner(resolved, raises(hit), tmp_path, is_child=True))


def test_a_limit_hit_recorded_by_metering_is_honoured_even_if_swallowed(resolved, tmp_path):
    costs = CostLog()

    def model(messages, info):  # metering records the hit; the SDK swallows the error
        costs.limit_hit = LimitHit("child", "reviewer", Decimal("1"), Decimal("1.2"))
        return ModelResponse(parts=[TextPart("done")])

    outcome = run(runner(resolved, FunctionModel(model), tmp_path, is_child=True, costs=costs))
    assert outcome.stopped == "budget"


def test_an_earlier_budget_hit_does_not_spoil_a_later_run(resolved, tmp_path):  # review I4
    costs = CostLog()
    costs.limit_hit = LimitHit("step", "build", Decimal("1"), Decimal("1.2"))  # then raised

    outcome = run(runner(resolved, say("done"), tmp_path, costs=costs))
    assert (outcome.status, outcome.output) == ("completed", "done")


def test_a_stuck_check_stops_with_stuck(resolved, tmp_path):
    r = runner(
        resolved,
        FunctionModel(always_calls_the_tool),
        tmp_path,
        tools=[busy_tool()],
        stuck_check=lambda messages: "repeat" if len(messages) >= 4 else None,
    )

    outcome = run(r)
    assert (outcome.status, outcome.stopped) == ("stopped", "stuck")


def test_stuck_detection_is_on_by_default_and_says_why(resolved, tmp_path):  # review I7
    r = runner(resolved, FunctionModel(always_calls_the_tool), tmp_path, tools=[busy_tool()])

    outcome = run(r)
    assert (outcome.status, outcome.stopped) == ("stopped", "stuck")
    assert outcome.detail is not None and outcome.detail.startswith("repeat")
