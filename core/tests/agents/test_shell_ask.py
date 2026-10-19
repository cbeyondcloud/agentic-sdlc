"""Tests for ask-listed shell commands: `run_shell` pauses for a person's approval before
running one (M1-31; spec §9.2: prefix -> allow / ask / forbid)."""

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agentic.agents.runner import AgentTask, PydanticAIRunner
from agentic.agents.tools import ShellResult, ToolContext, build_tools
from agentic.transport import BudgetLedger

TASK = AgentTask(instructions="You are the Developer.", inputs={}, prompt="Publish it.")


@dataclass(frozen=True)
class FakeBlock:
    wall: str
    why: str


class FakeGuard:
    def __init__(
        self, blocks: dict[str, str] | None = None, asks: dict[str, list[str]] | None = None
    ):
        self.blocks, self.ask_rules = blocks or {}, asks or {}

    def writes(self, paths):
        return []

    def command(self, command: str):
        return [FakeBlock("denied-command", why)] if (why := self.blocks.get(command)) else []

    def asks(self, command: str):
        return self.ask_rules.get(command, [])


class FakeSandbox:
    isolated = True

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def run(self, command: str, cwd: Path, timeout_s: float) -> ShellResult:
        self.ran.append(command)
        return ShellResult(exit_code=0, stdout="published", stderr="", timed_out=False)


def model(command: str) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if returns:
            return ModelResponse(parts=[TextPart(f"tool said: {returns[-1].content}")])
        return ModelResponse(parts=[ToolCallPart("run_shell", {"command": command})])

    return FunctionModel(respond)


def runner(resolved, tmp_path: Path, guard, sandbox, command: str) -> PydanticAIRunner:
    agent = resolved.model_copy(update={"tools": ["run_shell"]})
    tools = build_tools(agent, ToolContext(tree=tmp_path, guard=guard, sandbox=sandbox))
    step = BudgetLedger(Decimal("5")).run.open("build", "step", Decimal("1"))
    return PydanticAIRunner(
        agent, model(command), budget=step, state_dir=tmp_path / "state", tools=tools
    )


ASKING = {"npm publish": ["npm publish"]}


def test_an_ask_listed_command_pauses_for_approval(resolved, tmp_path):
    sandbox = FakeSandbox()
    r = runner(resolved, tmp_path, FakeGuard(asks=ASKING), sandbox, "npm publish")
    outcome = asyncio.run(r.run(TASK))

    assert outcome.status == "paused"
    assert len(outcome.pending) == 1
    assert sandbox.ran == []


def resume(resolved, tmp_path, outcome, sandbox, decision):
    r = runner(resolved, tmp_path, FakeGuard(asks=ASKING), sandbox, "npm publish")
    decisions = {call_id: decision for call_id in outcome.pending}
    return asyncio.run(r.resume(outcome.state_path, decisions, outcome.state_digest))


def test_an_approved_ask_runs_the_command(resolved, tmp_path):
    first = FakeSandbox()
    paused = asyncio.run(
        runner(resolved, tmp_path, FakeGuard(asks=ASKING), first, "npm publish").run(TASK)
    )
    sandbox = FakeSandbox()
    done = resume(resolved, tmp_path, paused, sandbox, True)

    assert done.status == "completed"
    assert sandbox.ran == ["npm publish"]
    assert "published" in done.output


def test_a_denied_ask_never_runs(resolved, tmp_path):
    paused = asyncio.run(
        runner(resolved, tmp_path, FakeGuard(asks=ASKING), FakeSandbox(), "npm publish").run(TASK)
    )
    sandbox = FakeSandbox()
    done = resume(resolved, tmp_path, paused, sandbox, "not this release")

    assert sandbox.ran == []
    assert "not this release" in done.output


def test_a_blocked_command_is_refused_before_asking(resolved, tmp_path):
    guard = FakeGuard(blocks={"npm publish": "denied by org"}, asks=ASKING)
    sandbox = FakeSandbox()
    outcome = asyncio.run(runner(resolved, tmp_path, guard, sandbox, "npm publish").run(TASK))

    assert outcome.status == "completed"
    assert "REFUSED: denied by org" in outcome.output
    assert sandbox.ran == []


def test_an_unlisted_command_runs_without_asking(resolved, tmp_path):
    sandbox = FakeSandbox()
    outcome = asyncio.run(
        runner(resolved, tmp_path, FakeGuard(asks=ASKING), sandbox, "npm test").run(TASK)
    )
    assert (outcome.status, sandbox.ran) == ("completed", ["npm test"])
