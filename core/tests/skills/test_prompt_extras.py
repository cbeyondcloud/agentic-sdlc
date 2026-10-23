"""`StepRunners(prompt_extras=...)`: text the framework adds at the end of a step's
instructions - the review pre-pass (M2-48), personal skills and notes (M2-45) - built when
the step runs, never when its runner is built (DF-146)."""

import asyncio
from pathlib import Path

from agentic.agents.runner import AgentTask
from agentic.engine.engine import StepContext
from agentic.engine.factory import StepRunners


class RecordingRunner:
    def __init__(self) -> None:
        self.tasks: list[AgentTask] = []

    async def run(self, task: AgentTask):
        self.tasks.append(task)
        return "done"

    async def resume(self, state_path, decisions, expected_digest):
        return "resumed"


class OneRunnerBackend:
    name = "native"

    def __init__(self) -> None:
        self.made = RecordingRunner()

    def runner(self, step, *, tool_context, state_dir, spawner, is_child=False):
        return self.made


def test_extras_end_the_instructions_when_the_step_runs(resolved, tmp_path: Path):
    backend = OneRunnerBackend()
    agent = resolved.model_copy(update={"backend": "native", "skills": []})
    asked: list[str] = []

    def extras(step: StepContext) -> str:
        asked.append(step.step)
        return "## Pre-pass results\n\n- secret-scan: ran, 1 finding"

    runners = StepRunners(
        {"native": backend},
        tools_for=lambda ctx: None,
        state_dir_for=lambda ctx: tmp_path,
        prompt_extras=extras,
    )
    runner = runners(StepContext("r-1", "review", 1, agent, None, None))
    assert asked == []  # building the runner computes nothing
    asyncio.run(runner.run(AgentTask(instructions="You review.", inputs={}, prompt="Go.")))
    (seen,) = backend.made.tasks
    block = "## Pre-pass results\n\n- secret-scan: ran, 1 finding"
    assert seen.instructions == f"You review.\n\n{block}"
    assert asked == ["review"]


def test_empty_extras_leave_the_instructions_alone(resolved, tmp_path: Path):
    backend = OneRunnerBackend()
    agent = resolved.model_copy(update={"backend": "native", "skills": []})
    runners = StepRunners(
        {"native": backend},
        tools_for=lambda ctx: None,
        state_dir_for=lambda ctx: tmp_path,
        prompt_extras=lambda step: "",
    )
    runner = runners(StepContext("r-1", "develop", 1, agent, None, None))
    asyncio.run(runner.run(AgentTask(instructions="You build.", inputs={}, prompt="Go.")))
    assert backend.made.tasks[0].instructions == "You build."
