"""Regression tests for the SP8 final review (Sonnet 5.5) of the engine's tree hooks: each
pins one finding (I6 per-step sandbox record, I7 stopped attempts)."""

import asyncio
from decimal import Decimal

from _engine_support import FakeTrees, answer
from pydantic_ai.usage import RunUsage

from agentic.agents.metering import CostLog
from agentic.agents.runner import AgentOutcome
from agentic.engine.engine import StepContext
from agentic.transport import LimitHit

TWO = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
  - id: develop
    agent: dev
"""


def run(coro):
    return asyncio.run(coro)


# ── I7: a stopped attempt is not checkpointed, so a retry starts from the base ─


class StoppedRunner:
    async def run(self, task):
        return AgentOutcome(
            status="stopped",
            output="half done",
            stopped="turns",
            usage=RunUsage(),
            costs=CostLog(),
            detail="the turn limit was reached",
        )


def test_a_stopped_attempt_is_not_checkpointed(make_harness):
    trees = FakeTrees()
    h = make_harness(TWO, {"designer": [answer("design")]}, trees_for=lambda run_id: trees)
    h.engine.runner_factory = lambda ctx: StoppedRunner()

    state = run(h.engine.start("test", {}, "dev@acme.example", "r-1"))

    assert state.pending.id == "step-turns"
    assert [c[0] for c in trees.calls] == ["prepare", "begin"]  # no finish: nothing kept
    assert state.history[-1].commit is None


# ── I6: each step records the sandbox it actually ran in ─────────────────────


def test_each_step_records_the_sandbox_it_ran_in(make_harness):
    calls = {"n": 0}

    def once_over_budget(ctx: StepContext) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise LimitHit("step", ctx.step, Decimal("1"), Decimal("1.2"))
        return answer("dev-report")

    shared: dict = {"ledgers": {}, "gates": {}}
    script = {"designer": [answer("design")], "dev": [once_over_budget]}
    first = make_harness(TWO, script, shared=shared, sandbox_label="srt 0.0.78")
    assert run(first.engine.start("test", {}, "dev@acme.example", "r-1")).pending.id == "budget"

    later = make_harness(TWO, script, shared=shared, sandbox_label="none")
    later.runners.calls.update(first.runners.calls)
    state = run(later.engine.resume("r-1"))

    assert state.status == "completed"
    assert [(r.step, r.sandbox) for r in state.history] == [
        ("design", "srt 0.0.78"),
        ("develop", "srt 0.0.78"),
        ("develop", "none"),
    ]
    assert state.sandbox == "srt 0.0.78"  # where the run started; each step says where it ran
