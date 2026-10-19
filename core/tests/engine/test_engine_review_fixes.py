"""Regression tests for the SP6 final review (Sonnet 5.5): each pins one finding."""

import asyncio
from decimal import Decimal

import pytest
from _engine_support import FakeClock, answer, review

from agentic.engine.engine import EngineError, StepContext
from agentic.engine.gates import Approve, GateRefused, Reject, StandaloneAuthorizer
from agentic.transport import LimitHit

STARTER, LEAD = "dev@acme.example", "lead@acme.example"


def run(coro):
    return asyncio.run(coro)


def start(h, run_id="r-1"):
    return run(h.engine.start("test", {"request": "Add CSV export"}, STARTER, run_id))


def decide(h, decision, self_approval=None):
    return run(
        h.engine.decide(
            "r-1",
            decision,
            authorizer=StandaloneAuthorizer(),
            self_approval=self_approval or (lambda a, s: None),
        )
    )


def four_eyes(approver, starter):
    return "may not approve your own run" if approver.lower() == starter.lower() else None


GATED = """\
name: test
version: 1.0.0
{extra}steps:
  - id: design
    agent: designer
  - gate: design-approval
    approver_role: team-lead
  - id: develop
    agent: dev
"""

REJECT_PAST = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
  - gate: design-approval
    approver_role: team-lead
  - id: develop
    agent: dev
  - gate: dev-approval
    approver_role: approver
    on_reject: design
"""


# ── C1: a gate reached again needs a new approval ─────────────────────────────


def test_a_gate_reached_again_needs_a_new_approval(make_harness):
    h = make_harness(
        REJECT_PAST,
        {
            "designer": [answer("design"), answer("design", summary="v2")],
            "dev": [answer("dev-report")],
        },
        gates="zyroflo",
    )
    start(h)
    decide(h, Approve("design-approval", LEAD))

    state = decide(h, Reject("dev-approval", LEAD, "redo the design"))
    assert (state.status, state.pending.id) == ("awaiting_gate", "design-approval")
    assert "handoffs/design-1-attempt2.md" in state.pending.artefacts
    assert "develop" not in [r.step for r in state.history[2:]]


# ── I1: a run can be stopped, even after its pipeline changed ─────────────────


def test_a_paused_run_can_be_stopped(make_harness):
    pipeline = GATED.format(extra="").replace(
        "agent: designer", 'agent: designer\n    when: "requirements.missing == 1"'
    )
    h = make_harness(pipeline, {"designer": []})
    assert start(h).pending.id == "condition"

    state = run(h.engine.stop("r-1", LEAD, "the condition cannot be fixed in this run"))
    assert (state.status, state.pending) == ("stopped", None)
    assert state.ended_by == LEAD and "cannot be fixed" in state.end_reason
    last = h.ledger("r-1").events()[-1]
    assert (last["event"], last["status"]) == ("run_closed", "stopped")
    assert run(h.engine.advance("r-1")).status == "stopped"


def test_a_run_whose_pipeline_changed_can_still_be_stopped(make_harness):
    shared: dict = {"ledgers": {}, "gates": {}}
    h = make_harness(GATED.format(extra=""), {"designer": [answer("design")]}, shared=shared)
    start(h)
    changed = make_harness(GATED.format(extra="max_minutes: 99\n"), {}, shared=shared)

    with pytest.raises(EngineError, match="changed"):
        run(changed.engine.advance("r-1"))
    assert run(changed.engine.stop("r-1", LEAD, "pipeline updated")).status == "stopped"


# ── I2: one process at a time, and a decision names its gate ──────────────────


def test_two_resumes_at_once_are_refused(make_harness):
    calls = {"n": 0}

    def once_over_budget(ctx: StepContext) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise LimitHit("step", ctx.step, Decimal("1"), Decimal("1.2"))
        return answer("design")

    h = make_harness(
        GATED.format(extra=""), {"designer": [once_over_budget], "dev": [answer("dev-report")]}
    )
    assert start(h).pending.id == "budget"

    async def both():
        return await asyncio.gather(
            h.engine.resume("r-1"), h.engine.resume("r-1"), return_exceptions=True
        )

    results = run(both())
    assert sum(isinstance(r, EngineError) and "busy" in str(r) for r in results) == 1
    assert calls["n"] == 2  # the paused step ran once more, not twice


def test_a_decision_must_name_the_gate_it_decides(make_harness):
    h = make_harness(
        GATED.format(extra=""), {"designer": [answer("design")], "dev": [answer("dev-report")]}
    )
    start(h)

    with pytest.raises(GateRefused, match="waits at design-approval"):
        decide(h, Approve("dev-approval", LEAD))


# ── I4: an unexpected failure pauses the step ─────────────────────────────────


def test_an_unexpected_error_pauses_the_step(make_harness):
    def provider_down(ctx: StepContext) -> str:
        raise RuntimeError("provider down")

    h = make_harness(GATED.format(extra=""), {"designer": [provider_down]})
    state = start(h)

    assert (state.status, state.pending.id) == ("paused", "step-error")
    assert "provider down" in state.pending.reason
    assert state.history[-1].outcome == "failed"
    assert h.ledger("r-1").events()[-1]["outcome"] == "error"


def test_a_failing_action_pauses_the_step(make_harness):
    pipeline = "name: test\nversion: 1.0.0\nsteps:\n  - id: publish\n    action: boom\n"

    def boom(state):
        raise RuntimeError("GitHub refused the push")

    h = make_harness(pipeline, {}, actions={"boom": boom})
    state = start(h)
    assert (state.pending.id, state.history[-1].outcome) == ("step-error", "failed")


# ── I6: time waiting for a person does not count against max_minutes ─────────


def test_time_waiting_for_a_person_does_not_count(make_harness):
    clock = FakeClock()
    h = make_harness(
        GATED.format(extra="max_minutes: 30\n"),
        {"designer": [answer("design")], "dev": [answer("dev-report")]},
        clock=clock,
    )
    start(h)
    clock.advance(14 * 60)  # the approver decides the next day

    assert decide(h, Approve("design-approval", LEAD)).status == "completed"


# ── I7: bytes after a gate, and refreshed pins ────────────────────────────────


def test_a_handoff_changed_after_its_gate_pauses_the_next_step(make_harness):
    calls = {"n": 0}

    def once_over_budget(ctx: StepContext) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise LimitHit("step", ctx.step, Decimal("1"), Decimal("1.2"))
        return answer("dev-report")

    h = make_harness(
        GATED.format(extra=""), {"designer": [answer("design")], "dev": [once_over_budget]}
    )
    start(h)
    assert decide(h, Approve("design-approval", LEAD)).pending.id == "budget"
    (h.ledger("r-1").run_dir / "handoffs" / "design-1.md").write_text(
        "edited by hand", encoding="utf-8"
    )

    state = run(h.engine.resume("r-1"))
    assert (state.pending.id, "design-1.md" in state.pending.reason) == ("handoff-changed", True)


def test_a_changed_pin_is_validated_before_it_can_be_approved(make_harness):
    h = make_harness(
        GATED.format(extra=""), {"designer": [answer("design")], "dev": [answer("dev-report")]}
    )
    start(h)
    (h.ledger("r-1").run_dir / "handoffs" / "design-1.md").write_text(
        "not a handoff", encoding="utf-8"
    )

    for _ in range(2):  # the broken bytes are never accepted on a second try
        with pytest.raises(GateRefused, match="not a valid handoff"):
            decide(h, Approve("design-approval", LEAD))


# ── I9: oscillation looks at the current pass of the loop only ────────────────


LOOP = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
  - loop:
      id: build-review
      max_iterations: 3
      until: "review.open_blocking == 0"
      steps:
        - id: develop
          agent: dev
        - id: review
          agent: reviewer
"""


def test_oscillation_ignores_an_earlier_pass_of_the_loop(make_harness):
    reviews = [review(["A"]), review(["B"]), review(["C"]), review(["B"]), review(["X"]), review()]
    h = make_harness(
        LOOP, {"designer": [answer("design")], "dev": [answer("dev-report")], "reviewer": reviews}
    )
    assert start(h).pending.id == "build-review-exhausted"

    state = decide(h, Reject("build-review-exhausted", LEAD, "try a smaller change"))
    assert state.status == "completed", state.pending


# ── promoted minors ───────────────────────────────────────────────────────────


def test_a_completed_step_and_the_cursor_move_are_saved_together(make_harness):
    pipeline = (
        "name: test\nversion: 1.0.0\nsteps:\n  - id: design\n    agent: designer\n"
        "  - id: develop\n    agent: dev\n"
    )
    h = make_harness(pipeline, {"designer": [answer("design")], "dev": [answer("dev-report")]})
    saved = []
    real_save = h.store.save

    def spy(state):
        saved.append(
            (state.cursor.index, [r.step for r in state.history if r.outcome == "completed"])
        )
        real_save(state)

    h.store.save = spy
    start(h)
    steps = ["design", "develop"]
    for index, done in saved:  # a completed step is never on disk with the cursor still on it
        assert not (done and index < len(steps) and done[-1] == steps[index]), saved


def test_the_starter_may_reject_their_own_regulated_run(make_harness):
    h = make_harness(
        GATED.format(extra="regime: regulated\n"),
        {"designer": [answer("design"), answer("design")], "dev": [answer("dev-report")]},
    )
    start(h)

    state = decide(h, Reject("design-approval", STARTER, "I want another option"), four_eyes)
    assert (state.status, state.pending.id) == ("awaiting_gate", "design-approval")
    with pytest.raises(GateRefused, match="own run"):
        decide(h, Approve("design-approval", STARTER), four_eyes)


def test_a_blocked_handoff_waits_for_a_person(make_harness):
    h = make_harness(
        GATED.format(extra=""),
        {
            "designer": [answer("design", status="blocked", summary="need the API owner's input")],
            "dev": [answer("dev-report")],
        },
    )

    state = start(h)
    assert (state.status, state.pending.id) == ("awaiting_gate", "blocked-output")
    assert "need the API owner's input" in state.pending.reason
    assert decide(h, Approve("blocked-output", LEAD)).pending.id == "design-approval"
