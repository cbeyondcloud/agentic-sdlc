"""A person grants a step paused at its own budget more budget on resume (DF-357, owner
decision 2026-11-07): never above what the run has left nor the agent type's (or team's)
cap, clamped and said so, and recorded with who granted how much."""

import asyncio
from decimal import Decimal

import pytest
from _engine_support import answer, write

from agentic.engine.engine import EngineError, StepContext
from agentic.engine.state import RunState
from agentic.policy import load_policy
from agentic.policy.resolve import budget_ceiling
from agentic.transport.budget import LimitHit

PIPELINE = """\
name: test
version: 1.0.0
steps:
  - id: requirements
    agent: {agent}
"""

PERSON = "lead@acme.example"


@pytest.fixture
def lean(tmp_path):
    """A business analyst asking for 0.5 USD; its type's cap is 2 (the fixture policy)."""
    write(
        tmp_path / "layer" / "agents" / "lean.md",
        "---\nname: lean\ntype: business-analyst\nversion: 1.0.0\ndescription: lean\n"
        "limits: { budget_usd: 0.5 }\n---\nYou are lean.\n",
    )


def harness_for(make_harness, agent: str, limits: list, **kwargs):
    """The step reaches its own budget once, then answers; `limits` collects each slice."""

    def behaviour(ctx: StepContext) -> str:
        limits.append(ctx.budget.limit_usd)
        if len(limits) == 1:
            raise LimitHit("step", ctx.step, ctx.budget.limit_usd, ctx.budget.limit_usd)
        return answer("requirements")

    return make_harness(PIPELINE.format(agent=agent), {agent: [behaviour, behaviour]}, **kwargs)


def start(harness):
    return asyncio.run(harness.engine.start("test", {}, "dev@acme.example", "r-1"))


def resume(harness, **kwargs):
    return asyncio.run(harness.engine.resume("r-1", **kwargs))


def test_the_type_cap_lowered_by_the_team_is_the_ceiling(fixtures):
    policy = load_policy(fixtures / "policy" / "policy")
    assert budget_ceiling(policy, "business-analyst", "payments") == Decimal("2")
    assert budget_ceiling(policy, "developer", None) == Decimal("5")
    assert budget_ceiling(policy, "developer", "payments") == Decimal("3")  # the team's cap
    assert budget_ceiling(policy, "nobody", None) is None


def test_a_grant_reruns_the_step_with_its_budget_plus_the_grant(make_harness, lean):
    limits: list = []
    harness = harness_for(make_harness, "lean", limits)
    assert start(harness).pending.id == "step-budget"

    state = resume(harness, add_budget_usd=Decimal("1"), who=PERSON)
    assert state.status == "completed"
    assert limits == [Decimal("0.5"), Decimal("1.5")]
    (grant,) = state.budget_grants
    assert (grant.step, grant.by, grant.requested_usd, grant.granted_usd) == (
        "requirements",
        PERSON,
        "1",
        "1",
    )
    assert grant.limited_by is None and grant.step_limit_usd == "1.5"
    (entry,) = harness.gates["r-1"].entries  # the approval ledger names the person
    assert entry["approver"] == PERSON and entry["step"] == "requirements"
    assert entry["extra"]["action"] == "budget-grant"
    assert entry["extra"]["granted_usd"] == "1" and entry["extra"]["requested_usd"] == "1"


def test_a_grant_above_the_policy_cap_is_clamped_and_says_so(make_harness, lean):
    limits: list = []
    harness = harness_for(make_harness, "lean", limits)
    start(harness)
    state = resume(harness, add_budget_usd=Decimal("5"), who=PERSON)
    (grant,) = state.budget_grants
    assert grant.granted_usd == "1.5" and grant.requested_usd == "5"
    assert grant.limited_by == "policy cap"
    assert limits[-1] == Decimal("2")  # the business-analyst type's cap
    assert "clamped" in harness.gates["r-1"].entries[0]["notes"]


def test_a_grant_above_what_the_run_has_left_is_clamped(make_harness, lean):
    limits: list = []
    harness = harness_for(make_harness, "lean", limits, run_budget_usd=Decimal("0.8"))
    start(harness)
    state = resume(harness, add_budget_usd=Decimal("1"), who=PERSON)
    (grant,) = state.budget_grants
    assert grant.granted_usd == "0.3" and grant.limited_by == "run budget"
    assert limits[-1] == Decimal("0.8")


def test_more_run_budget_and_a_grant_go_together(make_harness, lean):
    limits: list = []
    harness = harness_for(make_harness, "lean", limits, run_budget_usd=Decimal("0.5"))
    start(harness)
    state = resume(harness, extra_budget_usd=Decimal("1"), add_budget_usd=Decimal("1"), who=PERSON)
    assert state.status == "completed"
    assert state.budget_grants[0].granted_usd == "1" and limits[-1] == Decimal("1.5")


def test_nothing_to_grant_at_the_cap_is_refused_and_changes_nothing(make_harness):
    limits: list = []
    harness = harness_for(make_harness, "ba", limits)  # ba asks for nothing: its cap, 2
    start(harness)
    with pytest.raises(EngineError, match="cap"):
        resume(harness, add_budget_usd=Decimal("1"), who=PERSON)
    state = harness.store.load("r-1")
    assert state.pending.id == "step-budget" and state.budget_grants == []
    assert harness.gates["r-1"].entries == [] and len(limits) == 1


def test_a_grant_needs_a_step_budget_pause_and_a_person(make_harness, lean):
    def provider_error(ctx: StepContext) -> str:
        raise ValueError("boom")

    harness = make_harness(PIPELINE.format(agent="lean"), {"lean": [provider_error]})
    start(harness)
    with pytest.raises(EngineError, match="own budget"):
        resume(harness, add_budget_usd=Decimal("1"), who=PERSON)

    limits: list = []
    other = harness_for(make_harness, "lean", limits)
    other.store.path("r-1").unlink()
    start(other)
    with pytest.raises(EngineError, match="names the person"):
        resume(other, add_budget_usd=Decimal("1"))


def test_a_run_saved_before_grants_existed_still_loads(make_harness, lean):
    harness = harness_for(make_harness, "lean", [])
    start(harness)
    path = harness.store.path("r-1")
    data = RunState.model_validate_json(path.read_text(encoding="utf-8")).model_dump(mode="json")
    data.pop("budget_grants")
    assert RunState.model_validate(data).budget_grants == []


# ── review fixes ─────────────────────────────────────────────────────────────


def test_a_grant_after_real_spend_is_bounded_by_what_the_run_has_left(make_harness, monkeypatch):
    """Run 8, own 2, spent 2 on the first attempt, 12 asked: the run has 6 left, so the
    retry may spend up to 6 (a grant of 4), and the record says what was spent before."""
    from _engine_support import charge

    from agentic.engine import engine as engine_module

    monkeypatch.setattr(engine_module, "budget_ceiling", lambda *a: Decimal("100"))
    limits: list = []

    def behaviour(ctx: StepContext) -> str:
        limits.append(ctx.budget.limit_usd)
        if len(limits) == 1:
            charge("2")(ctx)
            raise LimitHit("step", ctx.step, ctx.budget.limit_usd, Decimal("2"))
        return answer("requirements")

    harness = make_harness(
        PIPELINE.format(agent="ba"), {"ba": [behaviour, behaviour]}, run_budget_usd=Decimal("8")
    )
    assert start(harness).pending.id == "step-budget"
    state = resume(harness, add_budget_usd=Decimal("12"), who=PERSON)
    (grant,) = state.budget_grants
    assert Decimal(grant.granted_usd) == 4 and Decimal(grant.earlier_usd) == 2
    assert grant.limited_by == "run budget"
    assert limits[-1] == Decimal("6")
    notes = harness.gates["r-1"].entries[0]["notes"]
    assert f"this retry may spend up to {grant.step_limit_usd} USD" in notes
    assert Decimal(grant.step_limit_usd) == 6
    assert f"earlier attempts on this step spent {grant.earlier_usd} USD" in notes
    assert "for the rest of the run" in notes


def test_two_successive_grants_add_up(make_harness, lean):
    limits: list = []

    def behaviour(ctx: StepContext) -> str:
        limits.append(ctx.budget.limit_usd)
        if len(limits) < 3:
            raise LimitHit("step", ctx.step, ctx.budget.limit_usd, ctx.budget.limit_usd)
        return answer("requirements")

    harness = make_harness(PIPELINE.format(agent="lean"), {"lean": [behaviour] * 3})
    start(harness)
    assert resume(harness, add_budget_usd=Decimal("0.5"), who=PERSON).pending.id == "step-budget"
    state = resume(harness, add_budget_usd=Decimal("0.5"), who=PERSON)
    assert state.status == "completed"
    assert limits == [Decimal("0.5"), Decimal("1.0"), Decimal("1.5")]
    assert [g.step_limit_usd for g in state.budget_grants] == ["1.0", "1.5"]


def test_amounts_are_recorded_as_plain_decimals(make_harness, lean):
    harness = harness_for(make_harness, "lean", [])
    start(harness)
    state = resume(harness, add_budget_usd=Decimal("1E+1"), who=PERSON)
    assert state.budget_grants[0].requested_usd == "10"
    assert "E" not in state.budget_grants[0].step_limit_usd


LOOP = """\
name: test
version: 1.0.0
steps:
  - loop:
      id: refine
      max_iterations: 2
      until: "review.open_blocking == 0"
      steps:
        - id: requirements
          agent: lean
        - id: review
          agent: reviewer
"""


def test_a_grant_holds_for_the_step_in_later_loop_iterations(make_harness, lean):
    from _engine_support import review

    limits: list = []

    def behaviour(ctx: StepContext) -> str:
        limits.append(ctx.budget.limit_usd)
        if len(limits) == 1:
            raise LimitHit("step", ctx.step, ctx.budget.limit_usd, ctx.budget.limit_usd)
        return answer("requirements")

    script = {"lean": [behaviour] * 3, "reviewer": [review(["open"]), review()]}
    harness = make_harness(LOOP, script)
    assert start(harness).pending.id == "step-budget"
    state = resume(harness, add_budget_usd=Decimal("1"), who=PERSON)
    assert state.status == "completed"
    assert limits == [Decimal("0.5"), Decimal("1.5"), Decimal("1.5")]  # iteration 2 too
