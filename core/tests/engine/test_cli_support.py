"""Engine behaviour the CLI relies on (M1-54, M1-55, M1-57): declared inputs (DF-54), a
runner that cannot be built pauses the run (DF-69), skipping the step a condition pause
is about (DF-59), and each child's spend recorded per step."""

import asyncio
from decimal import Decimal

import pytest
from _engine_support import answer, charge

from agentic.engine.engine import EngineError, StepContext
from agentic.transport.cost import CallCost

ONE_STEP = """\
name: test
version: 1.0.0
inputs:
  request: { from: [text] }
  ticket: { from: [jira], required: false }
steps:
  - id: requirements
    agent: ba
"""


def start(harness, inputs, run_id="r-1"):
    return asyncio.run(harness.engine.start("test", inputs, "dev@acme.example", run_id))


def test_an_undeclared_input_is_refused(make_harness):
    harness = make_harness(ONE_STEP, {"ba": [answer("requirements")]})
    with pytest.raises(EngineError, match="unknown input.*colour"):
        start(harness, {"request": "x", "colour": "red"})
    assert not harness.store.exists("r-1")


def test_a_missing_required_input_is_refused(make_harness):
    harness = make_harness(ONE_STEP, {"ba": [answer("requirements")]})
    with pytest.raises(EngineError, match="missing input.*request"):
        start(harness, {"ticket": "PROJ-1"})


def test_optional_inputs_may_be_left_out(make_harness):
    harness = make_harness(ONE_STEP, {"ba": [answer("requirements")]})
    assert start(harness, {"request": "x"}).status == "completed"


def test_a_pipeline_without_inputs_accepts_any(make_harness):
    pipeline = "name: test\nversion: 1.0.0\nsteps:\n  - id: requirements\n    agent: ba\n"
    harness = make_harness(pipeline, {"ba": [answer("requirements")]})
    assert start(harness, {"anything": "x"}).status == "completed"


def test_a_runner_that_cannot_be_built_pauses_the_run(make_harness):
    harness = make_harness(ONE_STEP, {"ba": [answer("requirements")]})
    scripted = harness.engine.runner_factory

    def broken(ctx: StepContext):
        raise ValueError("the regime needs a sandbox")

    harness.engine.runner_factory = broken
    state = start(harness, {"request": "x"})
    assert state.status == "paused" and state.pending.id == "step-error"
    assert "the regime needs a sandbox" in state.pending.reason
    assert state.history[-1].outcome == "failed"
    assert state.budget["children"] == []  # the step's slice was closed, not leaked

    harness.engine.runner_factory = scripted
    resumed = asyncio.run(harness.engine.resume("r-1"))
    assert resumed.status == "completed"


CONDITION = """\
name: test
version: 1.0.0
steps:
  - id: requirements
    agent: ba
  - id: design
    agent: designer
    when: "requirements.no_such_field == 1"
  - id: develop
    agent: dev
"""


def test_skip_moves_past_the_step_a_condition_pause_is_about(make_harness):
    script = {
        "ba": [answer("requirements")],
        "designer": [answer("design")],
        "dev": [answer("dev-report")],
    }
    harness = make_harness(CONDITION, script)
    state = start(harness, {"request": "x"})
    assert state.status == "paused" and state.pending.id == "condition"

    done = asyncio.run(harness.engine.resume("r-1", skip=True, who="lead@acme.example"))
    assert done.status == "completed"
    assert [(r.step, r.outcome) for r in done.history] == [
        ("requirements", "completed"),
        ("design", "skipped"),
        ("develop", "completed"),
    ]
    assert "designer" not in harness.runners.calls
    finished = [e for e in harness.ledger("r-1").events() if e["event"] == "step_finished"]
    assert any(e["step"] == "design" and e["outcome"] == "skipped" for e in finished)


def test_skip_is_refused_for_any_other_pause(make_harness):
    harness = make_harness(ONE_STEP, {"ba": [answer("requirements")]})
    harness.engine.runner_factory = lambda ctx: (_ for _ in ()).throw(ValueError("boom"))
    start(harness, {"request": "x"})
    with pytest.raises(EngineError, match="only a condition pause"):
        asyncio.run(harness.engine.resume("r-1", skip=True, who="lead@acme.example"))


def _cost(usd: str) -> CallCost:
    return CallCost(
        source="reported",
        cost_usd=Decimal(usd),
        tokens_in=1,
        tokens_out=1,
        tokens_cache_read=None,
        tokens_cache_write=None,
        model="m",
        request_id=None,
        trace_id=None,
    )


def test_each_child_s_spend_is_recorded_on_the_step(make_harness):
    def with_children(ctx: StepContext) -> str:
        charge("0.10")(ctx)
        for name, usd in (("security", "0.20"), ("perf", "0.05"), ("security", "0.01")):
            child = ctx.budget.open(name, "child", Decimal("1"))
            child.reserve(Decimal(usd)).settle(_cost(usd))
            child.close()
        return answer("requirements")

    harness = make_harness(ONE_STEP, {"ba": [with_children]})
    state = start(harness, {"request": "x"})
    record = state.history[-1]
    assert record.cost_usd == "0.36"
    assert record.children_usd == {"security": "0.21", "perf": "0.05"}
    finished = [e for e in harness.ledger("r-1").events() if e["event"] == "step_finished"]
    assert finished[-1]["cost"] == {"usd": "0.36", "children": {"security": "0.21", "perf": "0.05"}}


def test_a_step_without_children_records_no_breakdown(make_harness):
    harness = make_harness(ONE_STEP, {"ba": [answer("requirements")]})
    state = start(harness, {"request": "x"})
    assert state.history[-1].children_usd == {}
    finished = [e for e in harness.ledger("r-1").events() if e["event"] == "step_finished"]
    assert finished[-1]["cost"] == {"usd": "0"}
