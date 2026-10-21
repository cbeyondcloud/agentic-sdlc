"""M1-81 hardening of the engine: gate decisions, budgets after a crash and pauses a
person can act on (DF-37, DF-50, DF-52, DF-53, DF-56, DF-58, DF-267, DF-268)."""

import asyncio
from decimal import Decimal

import pytest
from _engine_support import answer, charge, review
from pydantic_ai.usage import RunUsage

from agentic.agents.metering import CostLog
from agentic.agents.runner import AgentOutcome
from agentic.engine.engine import FAILING_PAUSE
from agentic.engine.gates import Approve, Edit, GateRefused, Reject, StandaloneAuthorizer
from agentic.transport.cost import CallCost

STARTER, LEAD = "dev@acme.example", "lead@acme.example"

LOOP = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
  - loop:
      id: build-review
      max_iterations: 2
      until: "review.open_blocking == 0"
      steps:
        - id: develop
          agent: dev
        - id: review
          agent: reviewer
"""

TWO_THEN_GATE = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
  - id: develop
    agent: dev
  - gate: approval
    approver_role: team-lead
    on_reject: design
"""

REVIEW_OUTSIDE_A_LOOP = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
  - id: develop
    agent: dev
  - id: review
    agent: reviewer
"""

ONE_STEP = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
"""

SCRIPT = {"designer": [answer("design")], "dev": [answer("dev-report")]}


def run(coro):
    return asyncio.run(coro)


def start(h):
    return run(h.engine.start("test", {"request": "Add CSV export"}, STARTER, "r-1"))


def decide(h, decision):
    return run(
        h.engine.decide(
            "r-1", decision, authorizer=StandaloneAuthorizer(), self_approval=lambda a, s: None
        )
    )


def handoff_files(h) -> list[str]:
    folder = h.ledger("r-1").run_dir / "handoffs"
    return sorted(path.name for path in folder.iterdir())


# ── a refused approval keeps nothing (DF-50, DF-267) ─────────────────────────


def test_a_refused_edit_at_an_exhausted_gate_is_not_written(make_harness):
    """An edit that does not make the loop's `until` hold is refused before it is
    written: no `.edited-1` file is pinned that nobody is recorded as making (DF-267)."""
    h = make_harness(LOOP, {**SCRIPT, "reviewer": [review(["No timeout."])]})
    before = start(h)
    assert before.pending.id == "build-review-exhausted"
    path = before.latest["review"]
    files = handoff_files(h)
    text = (h.ledger("r-1").run_dir / path).read_text(encoding="utf-8")

    with pytest.raises(GateRefused, match="still does not hold"):
        decide(h, Edit("build-review-exhausted", LEAD, path, text.replace("No ", "Still no ")))

    after = h.store.load("r-1")
    assert handoff_files(h) == files
    assert after.latest == before.latest
    assert after.unpinned == before.unpinned
    assert after.pending.artefacts == before.pending.artefacts


def test_an_edit_is_not_written_when_another_pinned_file_changed(make_harness):
    """The gate refuses because a pinned file changed on disk: the person's edit is not
    kept behind their back either (DF-50)."""
    h = make_harness(TWO_THEN_GATE, SCRIPT)
    state = start(h)
    assert state.pending.id == "approval"
    run_dir = h.ledger("r-1").run_dir
    design, develop = state.latest["design"], state.latest["dev-report"]
    (run_dir / develop).write_text(
        answer("dev-report", summary="changed on disk"), encoding="utf-8", newline="\n"
    )
    files = handoff_files(h)
    edited = (run_dir / design).read_text(encoding="utf-8").replace("summary:", "summary: new")

    with pytest.raises(GateRefused, match="changed since the gate opened"):
        decide(h, Edit("approval", LEAD, design, edited))

    assert handoff_files(h) == files
    assert h.store.load("r-1").latest["design"] == design


# ── a rejection sends back the files it supersedes (DF-56) ───────────────────


def test_after_a_reject_the_next_gate_pins_only_the_new_handoffs(make_harness):
    h = make_harness(TWO_THEN_GATE, SCRIPT)
    start(h)

    state = decide(h, Reject("approval", LEAD, "split the export"))

    assert state.pending.id == "approval"
    assert sorted(state.pending.artefacts) == [
        "handoffs/design-1-attempt2.md",
        "handoffs/develop-1-attempt2.md",
    ]


# ── the person is recorded by their name, not their typing (DF-58) ──────────


def test_the_approver_is_recorded_without_surrounding_spaces(make_harness):
    h = make_harness(TWO_THEN_GATE, SCRIPT)
    start(h)

    decide(h, Reject("approval", f"  {LEAD} ", "again"))
    state = decide(h, Approve("approval", f"{LEAD}\t"))

    assert [e["approver"] for e in h.gates["r-1"].entries] == [LEAD, LEAD]
    assert state.approvals[-1].approver == LEAD


# ── budgets (DF-52, DF-53) ───────────────────────────────────────────────────


class Crash(BaseException):
    """The process dies mid-step (a laptop closed, a kill)."""


def test_a_restored_step_does_not_keep_holding_its_crashed_childrens_budget(make_harness):
    """A child slice left open by a crash still held its whole limit, so the step ran
    again with less budget than intended (DF-52). Its spend counts; its hold does not."""
    seen = {}

    def spawn_then_crash(ctx):
        child = ctx.budget.open("helper", "child", Decimal("0.8"))
        charge("0.1")(type("C", (), {"budget": child})())
        raise Crash

    def record(ctx):
        seen["limit"], seen["available"] = ctx.budget.limit_usd, ctx.budget.available()
        return answer("design")

    h = make_harness(ONE_STEP, {"designer": [spawn_then_crash, record]})
    with pytest.raises(Crash):
        start(h)

    state = run(h.engine.advance("r-1"))

    assert state.status == "completed", state.pending
    assert seen["available"] == seen["limit"] - Decimal("0.1")
    assert state.history[-1].cost_usd == "0.1"
    assert state.history[-1].children_usd == {"helper": "0.1"}


def test_a_budget_stopped_step_records_what_it_spent(make_harness):
    """The ledger was always right; the step's history record now is too (DF-53)."""

    def overspend(ctx):
        cost = CallCost("reported", Decimal("1.5"), 1, 1, None, None, "m", None, None)
        ctx.budget.reserve(Decimal("0.01")).settle(cost)
        return answer("design")

    h = make_harness(ONE_STEP, {"designer": [overspend]})
    state = start(h)

    assert state.pending.id == "step-budget"
    assert state.history[-1].outcome == "stopped"
    assert state.history[-1].cost_usd == "1.5"


# ── pauses say what to look at (DF-37, DF-268) ───────────────────────────────


def test_the_failing_handoff_pause_pins_the_failing_handoffs(make_harness):
    """`agentic show` lists the pause's artefacts: the failing review is there to read,
    not only named in the reason (DF-268)."""
    h = make_harness(REVIEW_OUTSIDE_A_LOOP, {**SCRIPT, "reviewer": [review(["No timeout."])]})

    state = start(h)

    assert state.pending.id == FAILING_PAUSE
    path = state.latest["review"]
    assert state.pending.artefacts == {path: state.digests[path]}


def test_a_turn_limit_before_a_retry_says_why_the_answer_was_refused(make_harness):
    """The step stopped on its turns before a refused answer could be retried: the pause
    names the errors, so a person knows the answer itself was the problem (DF-37)."""

    class Refused:
        async def run(self, task):
            return AgentOutcome(
                status="stopped",
                output="prose",
                stopped="turns",
                usage=RunUsage(),
                costs=CostLog(),
                errors=["body: the Markdown document after the front matter is empty"],
            )

    h = make_harness(ONE_STEP, {"designer": [answer("design")]})
    h.engine.runner_factory = lambda ctx: Refused()

    state = start(h)

    assert state.pending.id == "step-turns"
    assert "not a valid handoff" in state.pending.reason
    assert "front matter is empty" in state.pending.reason
