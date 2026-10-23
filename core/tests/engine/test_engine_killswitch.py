"""Kill-switch checks in the engine (M2-19, M2-10; master Review Focus 3): before every step
and every agent turn; a kill stops the run (marked killed, processes cleaned up), a pause
holds and resumes without loss, and unconfirmed switch state pauses, never continues."""

import asyncio
import subprocess
import sys

import pytest
from _engine_support import answer
from _process_support import alive, gone
from pydantic_ai import Tool
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from agentic.backends.processes import ProcessTree, spawn_group
from agentic.engine.engine import EngineError
from agentic.killswitch import RUN, RunScope, Verdict, unconfirmed

TWO_STEPS = """\
name: test
version: 1.0.0
steps:
  - id: requirements
    agent: ba
  - id: design
    agent: designer
"""
ONE_STEP = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
"""
KILL = Verdict("kill", reason="incident 42", by="lead@acme.example", switch="s1")
PAUSE = Verdict("pause", reason="maintenance", by="admin@acme.example", switch="s2")


class Switches:
    """A KillSwitch port: `rule(scope, n)` gives the verdict of the n-th check (1-based)."""

    def __init__(self, rule=None) -> None:
        self.rule = rule or (lambda scope, n: RUN)
        self.asked: list[RunScope] = []

    def check(self, scope: RunScope) -> Verdict:
        self.asked.append(scope)
        return self.rule(scope, len(self.asked))


def start(h, inputs=None):
    return asyncio.run(h.engine.start("test", inputs or {}, "dev@acme.example", "r-1"))


def events(h):
    return h.ledger("r-1").events()


def harness(make_harness, switches, pipeline=TWO_STEPS, **kwargs):
    script = {"ba": [answer("requirements")], "designer": [answer("design")]}
    return make_harness(pipeline, script, killswitch=switches, repo="github.com/acme/app", **kwargs)


def test_kill_before_a_step_marks_the_run_killed(make_harness):
    h = harness(make_harness, Switches(lambda scope, n: KILL))
    state = start(h)
    assert state.status == "killed" and state.pending is None
    assert state.ended_by == "lead@acme.example" and "incident 42" in state.end_reason
    assert h.runners.calls == {}  # no model call was made
    closed = [e for e in events(h) if e["event"] == "run_closed"]
    assert closed[-1]["status"] == "killed"


def test_kill_between_steps_keeps_the_finished_step(make_harness):
    rule = lambda scope, n: KILL if scope.agent and scope.agent.startswith("designer") else RUN  # noqa: E731
    h = harness(make_harness, Switches(rule))
    state = start(h)
    assert state.status == "killed"
    assert [r.step for r in state.history if r.outcome == "completed"] == ["requirements"]


def test_the_scope_names_the_run_and_the_step(make_harness):
    switches = Switches()
    h = harness(make_harness, switches, kill_poll_s=3600)
    assert start(h).status == "completed"
    step_scopes = [s for s in switches.asked if s.agent and s.agent.startswith("ba@")]
    scope = step_scopes[0]
    assert (scope.team, scope.user, scope.pipeline) == ("payments", "dev@acme.example", "test")
    assert scope.repo == "github.com/acme/app"
    assert scope.agent == "ba@1.0.0" and scope.backend == "native" and scope.model


def test_pause_holds_before_a_step_and_resumes_once_cleared(make_harness):
    switches = Switches(lambda scope, n: PAUSE)
    h = harness(make_harness, switches)
    state = start(h)
    assert (state.status, state.pending.kind, state.pending.id) == ("paused", "pause", "hold")
    assert "maintenance" in state.pending.reason and h.runners.calls == {}
    still = asyncio.run(h.engine.resume("r-1"))
    assert (still.status, still.pending.id) == ("paused", "hold")
    switches.rule = lambda scope, n: RUN
    done = asyncio.run(h.engine.resume("r-1"))
    assert done.status == "completed"
    assert h.runners.calls == {"ba": 1, "designer": 1}


def test_stale_state_pauses_before_next_step(make_harness):
    h = harness(make_harness, Switches(lambda scope, n: unconfirmed("no answer for 75 s")))
    state = start(h)
    assert (state.status, state.pending.id) == ("paused", "kill-switch-unconfirmed")
    assert "75 s" in state.pending.reason


def test_port_error_pauses(make_harness):
    def broken(scope, n):
        raise OSError("connection refused")

    state = start(harness(make_harness, Switches(broken)))
    assert (state.status, state.pending.id) == ("paused", "kill-switch-unconfirmed")
    assert "connection refused" in state.pending.reason


def test_killed_run_does_not_resume(make_harness):
    h = harness(make_harness, Switches(lambda scope, n: KILL))
    start(h)
    with pytest.raises(EngineError, match="killed"):
        asyncio.run(h.engine.resume("r-1"))
    with pytest.raises(EngineError, match="killed"):
        asyncio.run(h.engine.stop("r-1", "dev@acme.example", "done"))
    assert h.store.load("r-1").status == "killed"


def _tool_then_answer(make_harness, switches):
    """A one-step pipeline whose designer calls a tool, then answers (two model requests)."""
    h = harness(make_harness, switches, pipeline=ONE_STEP, kill_poll_s=3600)
    calls = {"n": 0}

    def look(path: str) -> str:
        """Look at a file."""
        return "nothing new"

    def model(messages, info):
        calls["n"] += 1
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[TextPart(answer("design"))])
        return ModelResponse(parts=[ToolCallPart("look", {"path": "a.py"})])

    h.runners.tools = [Tool(look)]
    real = h.runners.__call__

    def factory(ctx):
        runner = real(ctx)
        runner.model = FunctionModel(model)
        return runner

    h.engine.runner_factory = factory
    return h, calls


def test_paused_run_resumes_without_loss(make_harness):
    # checks: 1 before the step, 2 before its first request, 3 before its second
    switches = Switches(lambda scope, n: PAUSE if n == 3 else RUN)
    h, calls = _tool_then_answer(make_harness, switches)
    state = start(h)
    assert (state.status, state.pending.id) == ("paused", "hold")
    assert state.pending.state_path and state.pending.state_digest
    assert calls["n"] == 1  # the first turn finished; the second request was not sent
    switches.rule = lambda scope, n: PAUSE  # still paused: the held turn stays held
    kept = asyncio.run(h.engine.resume("r-1"))
    assert (kept.status, kept.pending.id) == ("paused", "hold") and kept.pending.state_path
    switches.rule = lambda scope, n: RUN
    done = asyncio.run(h.engine.resume("r-1"))
    assert done.status == "completed" and calls["n"] == 2  # continued, nothing repeated


def test_kill_at_a_turn_stops_the_run(make_harness):
    switches = Switches(lambda scope, n: KILL if n == 3 else RUN)
    h, calls = _tool_then_answer(make_harness, switches)
    state = start(h)
    assert state.status == "killed" and calls["n"] == 1
    finished = [e for e in events(h) if e["event"] == "step_finished"]
    assert finished[-1]["outcome"] == "killed"


def test_kill_mid_step_cancels_and_cleans_processes(make_harness):
    """A step blocked in a long tool call: the watchdog sees the kill and cancels the step,
    and the process tree the tool started inside the step's scope is killed."""
    started: dict = {}

    async def wait_for_build(path: str) -> str:
        """Run a long build."""
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"], **spawn_group()
        )
        ProcessTree().adopt(process)
        started["pid"] = process.pid
        await asyncio.sleep(120)
        return "built"

    switches = Switches(lambda scope, n: KILL if "pid" in started else RUN)
    h = harness(make_harness, switches, pipeline=ONE_STEP, kill_poll_s=0.05)
    real = h.runners.__call__

    def factory(ctx):
        runner = real(ctx)
        runner.model = FunctionModel(
            lambda messages, info: ModelResponse(
                parts=[ToolCallPart("wait_for_build", {"path": "x"})]
            )
        )
        return runner

    h.runners.tools = [Tool(wait_for_build)]
    h.engine.runner_factory = factory
    state = start(h)
    assert state.status == "killed"
    assert gone(started["pid"]), "the step's process tree outlived the kill"
    assert not alive(started["pid"])


def test_without_a_port_nothing_is_asked(make_harness):
    h = make_harness(TWO_STEPS, {"ba": [answer("requirements")], "designer": [answer("design")]})
    assert start(h).status == "completed"
