"""Nothing an agent step started outlives the step (M2-22): the engine opens a process
scope around every runner, whichever backend it is, and closes it however the step ends
- an answer, an error, or the run being stopped (cancelled)."""

import asyncio
import subprocess
import sys

from _engine_support import answer
from _process_support import PARENT, alive, gone
from pydantic_ai.usage import RunUsage

from agentic.agents.metering import CostLog
from agentic.agents.runner import AgentOutcome
from agentic.backends.processes import ProcessTree, spawn_group

ONE = """\
name: test
version: 1.0.0
steps:
  - id: develop
    agent: dev
"""


class Leaver:
    """A runner that starts a contained process (with a child of its own) and leaves it
    running: as a backend's background shell would."""

    def __init__(self, tmp_path, mode: str) -> None:
        self.script = tmp_path / "parent.py"
        self.script.write_text(PARENT, encoding="utf-8")
        self.mode, self.pids, self.started = mode, [], asyncio.Event()
        self.parents: list[subprocess.Popen] = []

    def __call__(self, ctx):
        self.contract = ctx.contract
        return self

    async def run(self, task) -> AgentOutcome:
        parent = subprocess.Popen(
            [sys.executable, str(self.script)],
            stdout=subprocess.PIPE,
            text=True,
            **spawn_group(),
        )
        self.parents.append(parent)
        ProcessTree().adopt(parent)  # never closed by the runner itself
        self.pids += [parent.pid, int(parent.stdout.readline())]
        self.started.set()
        if self.mode == "error":
            raise RuntimeError("the provider went away")
        if self.mode == "cancel":
            await asyncio.sleep(60)
        accepted, errors = self.contract.check(answer("dev-report", open_questions=[]))
        assert not errors
        return AgentOutcome("completed", accepted, None, RunUsage(), CostLog())

    async def resume(self, *args):
        raise AssertionError("not paused")

    def cleanup(self) -> None:
        for parent in self.parents:
            parent.kill()
            parent.wait(timeout=10)
            parent.stdout.close()


def harness(make_harness, tmp_path, mode):
    h = make_harness(ONE, {})
    leaver = Leaver(tmp_path, mode)
    h.engine.runner_factory = leaver
    return h, leaver


def test_what_a_completed_step_left_running_is_ended(make_harness, tmp_path):
    h, leaver = harness(make_harness, tmp_path, "completed")
    try:
        state = asyncio.run(h.engine.start("test", {}, "dev@acme.example", "r-1"))
        assert state.status == "completed", state.pending
        assert all(gone(pid) for pid in leaver.pids)
    finally:
        leaver.cleanup()


def test_what_a_failed_step_left_running_is_ended(make_harness, tmp_path):
    h, leaver = harness(make_harness, tmp_path, "error")
    try:
        state = asyncio.run(h.engine.start("test", {}, "dev@acme.example", "r-1"))
        assert state.pending.id == "step-error"
        assert all(gone(pid) for pid in leaver.pids)
    finally:
        leaver.cleanup()


def test_what_a_stopped_run_left_running_is_ended(make_harness, tmp_path):
    h, leaver = harness(make_harness, tmp_path, "cancel")

    async def stop_it():
        run = asyncio.ensure_future(h.engine.start("test", {}, "dev@acme.example", "r-1"))
        await leaver.started.wait()
        assert all(alive(pid) for pid in leaver.pids)
        run.cancel()  # a Ctrl-C, a kill switch: the run's task is cancelled
        try:
            await run
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(stop_it())
        assert all(gone(pid) for pid in leaver.pids)
    finally:
        leaver.cleanup()
