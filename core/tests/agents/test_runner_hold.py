"""The native runner's turn guard (M2-19, M2-10): before each model request it asks the kill
switch; a pause finishes the turn in progress, saves the conversation and holds, and the
saved conversation resumes without repeating a model call; a kill raises at once."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from agentic.agents.runner import AgentTask, PydanticAIRunner
from agentic.killswitch import RUN, RunKilled, Verdict
from agentic.transport import BudgetLedger

TASK = AgentTask(instructions="You are the Developer.", inputs={}, prompt="Do the work.")
PAUSE = Verdict("pause", reason="maintenance", by="admin@acme.example", switch="s1")
KILL = Verdict("kill", reason="incident", by="admin@acme.example", switch="s2")


class Guard:
    """Answers each turn check from a list (the last answer repeats); counts the checks."""

    def __init__(self, *answers: Verdict) -> None:
        self.answers, self.asked = list(answers), 0

    async def __call__(self) -> Verdict:
        self.asked += 1
        return self.answers[min(self.asked - 1, len(self.answers) - 1)]


class Model:
    """A tool call first, then the final answer once the tool's result is in the history."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_return = False

    def __call__(self, messages, info) -> ModelResponse:
        self.calls += 1
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            self.seen_return = True
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart("look", {"path": "a.py"})])


def look(path: str) -> str:
    """Look at a file."""
    return f"{path}: nothing new"


def make(resolved, model, tmp_path: Path, guard, **kwargs) -> PydanticAIRunner:
    step = BudgetLedger(Decimal("5")).run.open("build", "step", Decimal("1"))
    return PydanticAIRunner(
        resolved,
        FunctionModel(model),
        budget=step,
        state_dir=tmp_path,
        tools=[Tool(look)],
        turn_guard=guard,
        run_id="r-1",
        stuck_check=lambda messages: None,
        **kwargs,
    )


def test_a_pause_holds_before_the_next_request_and_resumes_without_loss(resolved, tmp_path):
    model, guard = Model(), Guard(RUN, PAUSE)
    held = asyncio.run(make(resolved, model, tmp_path, guard).run(TASK))
    assert held.status == "paused" and held.held == PAUSE and held.pending == []
    assert model.calls == 1  # the turn in progress finished (its tool ran), no new request
    saved = json.loads(held.state_path.read_text(encoding="utf-8"))
    assert saved["pending"] == [] and saved["fingerprint"] == held.state_digest
    resumed_guard = Guard(RUN)
    again = make(resolved, model, tmp_path, resumed_guard)
    done = asyncio.run(again.resume(held.state_path, {}, held.state_digest))
    assert done.status == "completed" and done.output == "done"
    assert model.calls == 2 and model.seen_return  # continued with the tool's result
    assert done.usage.requests == 2  # turns already used still count


def test_a_kill_at_a_turn_raises(resolved, tmp_path):
    model = Model()
    with pytest.raises(RunKilled) as caught:
        asyncio.run(make(resolved, model, tmp_path, Guard(RUN, KILL)).run(TASK))
    assert caught.value.verdict == KILL and model.calls == 1


def test_a_child_ignores_a_pause_but_obeys_a_kill(resolved, tmp_path):
    child = make(resolved, Model(), tmp_path, Guard(PAUSE), is_child=True)
    assert asyncio.run(child.run(TASK)).status == "completed"
    with pytest.raises(RunKilled):
        asyncio.run(make(resolved, Model(), tmp_path, Guard(KILL), is_child=True).run(TASK))


def test_without_a_guard_nothing_changes(resolved, tmp_path):
    outcome = asyncio.run(make(resolved, Model(), tmp_path, None).run(TASK))
    assert outcome.status == "completed" and outcome.held is None
