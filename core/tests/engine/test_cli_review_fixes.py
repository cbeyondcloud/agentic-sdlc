"""Engine fixes from the SP14b final review: text the engine stores is redacted (F1), a
step that reaches its own budget is told apart from the run's (F4), more budget is only
granted where it does something (F4), and skipping a step names who did it (F6)."""

import asyncio
from decimal import Decimal

import pytest
from _engine_support import answer

from agentic.engine.engine import EngineError, StepContext
from agentic.transport.budget import LimitHit

SECRET = "tl-" + "engine-secret-0001"  # gitleaks:allow (not a real key)

ONE_STEP = """\
name: test
version: 1.0.0
steps:
  - id: requirements
    agent: ba
"""

CONDITION = """\
name: test
version: 1.0.0
regime: {regime}
steps:
  - id: requirements
    agent: ba
  - id: design
    agent: designer
    when: "requirements.no_such_field == 1"
"""

STARTER = "dev@acme.example"


def start(harness, run_id="r-1"):
    return asyncio.run(harness.engine.start("test", {}, STARTER, run_id))


def resume(harness, **kwargs):
    return asyncio.run(harness.engine.resume("r-1", **kwargs))


def masked(text: str) -> str:
    return text.replace(SECRET, "<redacted>")


# ── F1: the engine redacts what it stores ────────────────────────────────────


def test_a_provider_error_is_redacted_before_it_is_stored(make_harness):
    def provider_error(ctx: StepContext) -> str:
        raise ValueError(f"invalid api key {SECRET}")

    harness = make_harness(ONE_STEP, {"ba": [provider_error]}, redact=masked)
    state = start(harness)
    assert state.pending.id == "step-error"
    assert SECRET not in state.pending.reason and "<redacted>" in state.pending.reason
    assert SECRET not in state.history[-1].detail
    assert SECRET not in harness.store.path("r-1").read_text(encoding="utf-8")


def test_without_a_redactor_the_text_is_stored_as_it_is(make_harness):
    def provider_error(ctx: StepContext) -> str:
        raise ValueError("plain failure")

    harness = make_harness(ONE_STEP, {"ba": [provider_error]})
    assert "plain failure" in start(harness).pending.reason


# ── F4: step budget versus run budget ────────────────────────────────────────


def _over_step_budget(ctx: StepContext) -> str:
    raise LimitHit("step", ctx.step, Decimal("1"), Decimal("1.2"))


def test_a_step_reaching_its_own_budget_pauses_as_step_budget(make_harness):
    harness = make_harness(ONE_STEP, {"ba": [_over_step_budget]})
    assert start(harness).pending.id == "step-budget"


def test_the_run_reaching_its_budget_still_pauses_as_budget(make_harness):
    def over_run_budget(ctx: StepContext) -> str:
        raise LimitHit("run", "run", Decimal("1"), Decimal("1.2"))

    harness = make_harness(ONE_STEP, {"ba": [over_run_budget]}, run_budget_usd=Decimal("1"))
    assert start(harness).pending.id == "budget"


def test_a_step_capped_by_what_the_run_has_left_pauses_as_budget(make_harness):
    calls = {"n": 0}

    def once_over(ctx: StepContext) -> str:
        calls["n"] += 1
        if calls["n"] == 1:  # the slice is 0.5 (the run's), not the agent's 2
            raise LimitHit("step", ctx.step, ctx.budget.limit_usd, Decimal("0.6"))
        return answer("requirements")

    harness = make_harness(ONE_STEP, {"ba": [once_over]}, run_budget_usd=Decimal("0.5"))
    assert start(harness).pending.id == "budget"
    assert resume(harness, extra_budget_usd=Decimal("5")).status == "completed"


def test_more_run_budget_is_refused_for_a_step_budget_pause(make_harness):
    harness = make_harness(ONE_STEP, {"ba": [_over_step_budget]}, run_budget_usd=Decimal("10"))
    start(harness)
    with pytest.raises(EngineError, match="own budget"):
        resume(harness, extra_budget_usd=Decimal("5"))
    assert harness.store.load("r-1").pending.id == "step-budget"  # nothing changed


def test_more_budget_is_refused_for_a_run_without_a_budget_limit(make_harness):
    def provider_error(ctx: StepContext) -> str:
        raise ValueError("boom")

    harness = make_harness(ONE_STEP, {"ba": [provider_error]})
    start(harness)
    with pytest.raises(EngineError, match="no budget limit"):
        resume(harness, extra_budget_usd=Decimal("5"))


# ── F6: a skip names the person ──────────────────────────────────────────────


def _conditional(make_harness, regime: str = "team"):
    script = {"ba": [answer("requirements")], "designer": [answer("design")]}
    harness = make_harness(CONDITION.format(regime=regime), script)
    assert start(harness).pending.id == "condition"
    return harness


def test_a_skip_records_who_skipped(make_harness):
    harness = _conditional(make_harness)
    done = resume(harness, skip=True, who="lead@acme.example")
    assert done.status == "completed"
    record = next(r for r in done.history if r.outcome == "skipped")
    assert record.detail == "skipped by lead@acme.example"
    entry = harness.gates["r-1"].entries[-1]  # in the approval ledger, like a gate decision
    assert (entry["gate"], entry["verdict"], entry["approver"]) == (
        "skip-design",
        "approve",
        "lead@acme.example",
    )
    assert entry["step"] == "design" and entry["extra"]["action"] == "skip"
    assert "condition" in entry["notes"]


def test_a_skip_without_a_person_is_refused(make_harness):
    harness = _conditional(make_harness)
    with pytest.raises(EngineError, match="names the person"):
        resume(harness, skip=True)
    with pytest.raises(EngineError, match="names the person"):
        resume(harness, skip=True, who="  ")


def test_in_the_regulated_regime_the_starter_cannot_skip(make_harness):
    harness = _conditional(make_harness, "regulated")
    with pytest.raises(EngineError, match="four-eyes"):
        resume(harness, skip=True, who=STARTER.upper())
    with pytest.raises(EngineError, match="four-eyes"):
        resume(harness, skip=True, who="x", self_approval=lambda who, starter: "same person")
    done = resume(harness, skip=True, who="lead@acme.example", self_approval=lambda w, s: None)
    assert done.status == "completed"
