"""Tests for the pipeline engine: steps, conditions, loops, pauses, timeouts, resume
(M1-21, M1-24, M1-26; spec §6, §8.3, §8.4, §8.7)."""

import asyncio
from decimal import Decimal

import pytest
from conftest import FakeClock, answer, charge, review
from pydantic_ai import Tool
from pydantic_ai.messages import ModelResponse, ToolCallPart

from agentic.engine.engine import StepContext
from agentic.handoffs import parse_handoff
from agentic.transport import LimitHit

TWO_STEPS = """\
name: test
version: 1.0.0
steps:
  - id: requirements
    agent: ba
  - id: design
    agent: designer
"""

LOOP = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
  - loop:
      id: build-review
      max_iterations: {max_iterations}
      until: "review.open_blocking == 0"
      on_exhausted: {on_exhausted}
      steps:
        - id: develop
          agent: dev
        - id: review
          agent: reviewer
"""

GATED = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
  - gate: design-approval
    approver_role: team-lead
  - id: develop
    agent: dev
"""


def run(coro):
    return asyncio.run(coro)


def start(h, run_id="r-1", inputs=None):
    return run(h.engine.start("test", inputs or {"request": "Add CSV export"}, "dev@acme.example",
                              run_id))  # fmt: skip


def events(h, run_id="r-1") -> list[dict]:
    return h.ledger(run_id).events()


# ── steps and handoffs ────────────────────────────────────────────────────────


def test_steps_run_in_order_and_handoffs_are_pinned(make_harness):
    h = make_harness(TWO_STEPS, {"ba": [answer("requirements")], "designer": [answer("design")]})

    state = start(h)
    assert state.status == "completed"
    assert [(r.step, r.outcome) for r in state.history] == [
        ("requirements", "completed"),
        ("design", "completed"),
    ]
    for record in state.history:
        assert h.ledger("r-1").digest(record.handoff) == record.digest
    assert state.latest == {"requirements": "handoffs/requirements-1.md",
                            "design": "handoffs/design-1.md"}  # fmt: skip
    assert h.ledger("r-1").verify() == []
    kinds = [e["event"] for e in events(h)]
    assert kinds.count("step_started") == 2 and kinds[-1] == "run_closed"


def test_the_final_handoff_carries_the_framework_envelope(make_harness):
    h = make_harness(TWO_STEPS, {"ba": [answer("requirements")], "designer": [answer("design")]})

    state = start(h)
    text = (h.ledger("r-1").run_dir / state.latest["design"]).read_text(encoding="utf-8")
    handoff = parse_handoff(text)
    assert (handoff.envelope.run_id, handoff.envelope.step) == ("r-1", "design")
    assert handoff.envelope.agent == "designer@1.0.0"


def test_a_step_is_given_its_consumed_handoffs_and_the_request(make_harness):
    h = make_harness(TWO_STEPS, {"ba": [answer("requirements")], "designer": [answer("design")]})

    start(h)
    prompt = h.runners.prompts["designer"][0]
    assert "step: design" in prompt and "request: Add CSV export" in prompt


def test_run_json_is_saved_after_every_step(make_harness):
    h = make_harness(TWO_STEPS, {"ba": [answer("requirements")], "designer": [answer("design")]})

    start(h)
    saved = h.store.load("r-1")
    assert saved.status == "completed" and len(saved.history) == 2
    assert saved.catalog_digest.startswith("sha256:")


# ── conditions ────────────────────────────────────────────────────────────────


def test_a_false_when_skips_the_step(make_harness):
    pipeline = TWO_STEPS.replace(
        "agent: designer", "agent: designer\n    when: \"requirements.size != 'trivial'\""
    )
    h = make_harness(pipeline, {"ba": [answer("requirements", size="trivial")], "designer": []})

    state = start(h)
    assert state.status == "completed"
    assert [r.outcome for r in state.history] == ["completed", "skipped"]


def test_a_condition_on_a_missing_field_pauses_with_the_reason(make_harness):  # RF 5
    pipeline = TWO_STEPS.replace(
        "agent: designer", 'agent: designer\n    when: "requirements.missing == 1"'
    )
    h = make_harness(pipeline, {"ba": [answer("requirements")], "designer": []})

    state = start(h)
    assert (state.status, state.pending.kind, state.pending.id) == ("paused", "pause", "condition")
    assert "missing" in state.pending.reason and "requirements.missing" in state.pending.reason


# ── loops ─────────────────────────────────────────────────────────────────────


def test_a_loop_ends_when_until_holds(make_harness):
    h = make_harness(
        LOOP.format(max_iterations=3, on_exhausted="gate"),
        {
            "designer": [answer("design")],
            "dev": [answer("dev-report")],
            "reviewer": [review(["no timeout"]), review()],
        },  # fmt: skip
    )

    state = start(h)
    assert state.status == "completed", state.pending
    assert [r.step for r in state.history] == ["design", "develop", "review", "develop", "review"]
    assert [e["event"] for e in events(h)].count("loop_back") == 1
    assert state.latest["review"] == "handoffs/review-2.md"


def test_an_exhausted_loop_escalates_to_a_gate(make_harness):
    h = make_harness(
        LOOP.format(max_iterations=2, on_exhausted="gate"),
        {
            "designer": [answer("design")],
            "dev": [answer("dev-report")],
            "reviewer": [review(["first"]), review(["second"])],
        },  # fmt: skip
    )

    state = start(h)
    assert (state.status, state.pending.kind, state.pending.id) == (
        "awaiting_gate",
        "gate",
        "build-review-exhausted",
    )


def test_an_exhausted_loop_can_fail_the_run(make_harness):
    h = make_harness(
        LOOP.format(max_iterations=1, on_exhausted="fail"),
        {
            "designer": [answer("design")],
            "dev": [answer("dev-report")],
            "reviewer": [review(["blocking"])],
        },  # fmt: skip
    )

    state = start(h)
    assert state.status == "failed"
    assert events(h)[-1]["event"] == "run_closed"


def test_an_oscillating_loop_escalates_early(make_harness):  # DF-29
    h = make_harness(
        LOOP.format(max_iterations=5, on_exhausted="gate"),
        {
            "designer": [answer("design")],
            "dev": [answer("dev-report")],
            "reviewer": [review(["Missing test for retry"]), review(["missing test for retry "])],
        },  # fmt: skip
    )

    state = start(h)
    assert state.pending.id == "build-review-exhausted" and "oscillat" in state.pending.reason
    assert len([r for r in state.history if r.step == "review"]) == 2


# ── gates ─────────────────────────────────────────────────────────────────────


def test_a_gate_stops_the_run_and_pins_what_it_gates(make_harness):
    h = make_harness(GATED, {"designer": [answer("design")], "dev": [answer("dev-report")]})

    state = start(h)
    digest = h.ledger("r-1").digest("handoffs/design-1.md")
    assert (state.status, state.pending.kind, state.pending.id) == (
        "awaiting_gate",
        "gate",
        "design-approval",
    )
    assert state.pending.artefacts == {"handoffs/design-1.md": digest}
    assert any(e["event"] == "gate_waiting" and e.get("gate") == "design-approval"
               for e in events(h))  # fmt: skip


# ── pauses for a person ───────────────────────────────────────────────────────


def test_invalid_output_waits_for_a_person(make_harness):
    prose = "I think we should use a queue."
    h = make_harness(TWO_STEPS, {"ba": [answer("requirements")], "designer": [prose, prose]})

    state = start(h)
    assert (state.status, state.pending.kind, state.pending.id) == (
        "awaiting_gate",
        "gate",
        "invalid-output",
    )
    kept = h.ledger("r-1").run_dir / "handoffs" / "design-1.invalid.md"
    assert kept.read_text(encoding="utf-8") == prose
    assert "must be one handoff document" in state.pending.reason


def test_a_budget_limit_pauses_the_run(make_harness):
    def over_budget(ctx: StepContext) -> str:
        raise LimitHit("step", ctx.step, Decimal("1"), Decimal("1.2"))

    h = make_harness(TWO_STEPS, {"ba": [answer("requirements")], "designer": [over_budget]})

    state = start(h)
    assert (state.status, state.pending.id) == ("paused", "budget")
    assert "budget" in state.pending.reason


def test_the_run_time_limit_pauses_and_resume_can_extend_it(make_harness):  # M1-26
    clock = FakeClock()

    def slow_requirements(ctx: StepContext) -> str:
        clock.advance(11)
        return answer("requirements")

    pipeline = TWO_STEPS.replace("version: 1.0.0\n", "version: 1.0.0\nmax_minutes: 10\n", 1)
    h = make_harness(pipeline, {"ba": [slow_requirements], "designer": [answer("design")]},
                     clock=clock)  # fmt: skip

    state = start(h)
    assert (state.status, state.pending.id) == ("paused", "run-timeout")
    resumed = run(h.engine.resume("r-1", extra_minutes=30))
    assert resumed.status == "completed"


def test_a_step_timeout_pauses_with_time(make_harness):  # M1-26
    async def never(messages, info):
        await asyncio.sleep(5)

    from pydantic_ai.models.function import FunctionModel

    h = make_harness(TWO_STEPS, {"ba": [answer("requirements")], "designer": []})
    real = h.runners.__call__

    def factory(ctx):
        runner = real(ctx)
        if ctx.step == "design":
            runner.model, runner.time_limit_s = FunctionModel(never), 0.2
        return runner

    h.engine.runner_factory = factory
    state = start(h)
    assert (state.status, state.pending.id) == ("paused", "step-time")


def test_a_crash_mid_step_reruns_that_step_and_keeps_its_spend(make_harness):  # RF 2
    class Crash(BaseException):
        pass

    def spend_then_crash(ctx: StepContext) -> str:
        charge("0.40")(ctx)
        raise Crash()

    shared: dict = {"ledgers": {}, "gates": {}}
    script = {"ba": [answer("requirements")], "designer": [spend_then_crash]}
    h = make_harness(TWO_STEPS, script, shared=shared, run_budget_usd=Decimal("10"))
    with pytest.raises(Crash):
        start(h)
    saved = h.store.load("r-1")
    assert [r.step for r in saved.history] == ["requirements"]

    def spend_then_answer(ctx: StepContext) -> str:
        charge("0.10")(ctx)
        return answer("design")

    again = make_harness(TWO_STEPS, {"ba": [answer("requirements")],
                                     "designer": [spend_then_answer]},
                         shared=shared, run_budget_usd=Decimal("10"))  # fmt: skip
    state = run(again.engine.resume("r-1"))
    assert state.status == "completed"
    assert "ba" not in again.runners.calls  # the completed step is not run again
    from agentic.transport.budget import BudgetLedger

    assert BudgetLedger.restore(state.budget).run.total_spent() == Decimal("0.50")


def test_a_tool_approval_pause_resumes_with_decisions(make_harness, tmp_path):
    def write_file(path: str) -> str:
        """Write a file (needs a person's approval)."""
        (tmp_path / path).write_text("hello", encoding="utf-8")
        return f"wrote {path}"

    calls = {"n": 0}

    def ask_then_answer(ctx: StepContext):
        calls["n"] += 1
        return answer("design")

    h = make_harness(TWO_STEPS, {"ba": [answer("requirements")], "designer": [ask_then_answer]})
    h.runners.tools = [Tool(write_file, requires_approval=True)]
    real = h.runners.__call__

    def factory(ctx):
        runner = real(ctx)
        if ctx.step == "design":
            from pydantic_ai.messages import ToolReturnPart
            from pydantic_ai.models.function import FunctionModel

            def model(messages, info):
                if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
                    return ModelResponse(parts=[TextPart(answer("design"))])
                return ModelResponse(parts=[ToolCallPart("write_file", {"path": "out.txt"})])

            runner.model = FunctionModel(model)
        return runner

    from pydantic_ai.messages import TextPart

    h.engine.runner_factory = factory
    state = start(h)
    assert (state.status, state.pending.kind) == ("paused", "approval")
    pending_call = h.runners.state_root  # the runner's state lives under the agent-state root
    assert pending_call.is_dir() and state.pending.state_digest

    decisions = {call: True for call in state.pending.calls}
    resumed = run(h.engine.resume("r-1", decisions=decisions))
    assert resumed.status == "completed" and (tmp_path / "out.txt").is_file()
