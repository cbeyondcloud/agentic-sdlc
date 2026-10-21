"""A run never ends `completed` on a failing review or QA report (DF-238, SP18b review F2).

* an exhausted loop is not approved as it stands: a person edits a handoff so that the
  loop's `until` holds, sends the work back, or stops the run;
* a run that reaches its end while its newest handoffs report a failure pauses
  `failing-handoff` instead of completing.
"""

import asyncio

import pytest
from _engine_support import answer, review, write

from agentic.engine.engine import FAILING_PAUSE
from agentic.engine.gates import Approve, Edit, GateRefused, Reject, StandaloneAuthorizer

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

QA_THEN_PUBLISH = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
  - id: develop
    agent: dev
  - id: qa
    agent: qa
  - id: publish
    action: open_pr
    when: "test_report.verdict == 'pass'"
"""

QA_FAILS = answer(
    "test-report",
    summary="red",
    verdict="fail",
    tests={"passed": 41, "failed": 1, "skipped": 0},
    failures=["tests/test_export.py::test_csv"],
)
QA_PASSES = answer("test-report", summary="green")
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


@pytest.fixture
def with_qa(tmp_path):
    """A QA agent next to the harness's fixture agents (it reads the layer when built)."""
    write(
        tmp_path / "layer" / "agents" / "qa.md",
        "---\nname: qa\ntype: general\nproduces: test-report\nconsumes: [dev-report]\n"
        "version: 1.0.0\ndescription: qa\n---\nYou are qa.\n",
    )


def exhausted(make_harness):
    h = make_harness(LOOP, {**SCRIPT, "reviewer": [review(["No timeout."])]})
    state = start(h)
    assert (state.status, state.pending.id) == ("awaiting_gate", "build-review-exhausted")
    return h, state


# ── an exhausted loop ─────────────────────────────────────────────────────────


def test_an_exhausted_loop_cannot_be_approved_as_it_stands(make_harness):
    h, _ = exhausted(make_harness)

    with pytest.raises(GateRefused, match="review.open_blocking == 0") as refused:
        decide(h, Approve("build-review-exhausted", LEAD))
    assert "edit" in str(refused.value) and "reject" in str(refused.value)

    state = h.store.load("r-1")
    assert (state.status, state.pending.id) == ("awaiting_gate", "build-review-exhausted")
    assert h.gates["r-1"].entries == []  # nothing was recorded as approved


def test_an_edit_that_meets_until_lets_the_loop_end(make_harness):
    h, state = exhausted(make_harness)
    path = state.latest["review"]
    text = (h.ledger("r-1").run_dir / path).read_text(encoding="utf-8")
    accepted = text.replace("status: open", "status: accepted").replace(
        "open_blocking: 1", "open_blocking: 0"
    )

    state = decide(h, Edit("build-review-exhausted", LEAD, path, accepted))
    assert state.status == "completed", state.pending
    assert [e["verdict"] for e in h.gates["r-1"].entries] == ["approve"]


def test_an_edit_that_still_fails_until_is_refused(make_harness):
    h, state = exhausted(make_harness)
    path = state.latest["review"]
    text = (h.ledger("r-1").run_dir / path).read_text(encoding="utf-8")
    reworded = text.replace("No timeout.", "No timeout at all.")

    with pytest.raises(GateRefused, match="still does not hold"):
        decide(h, Edit("build-review-exhausted", LEAD, path, reworded))
    assert h.store.load("r-1").pending.id == "build-review-exhausted"


def test_reject_at_an_exhausted_gate_still_runs_another_pass(make_harness):
    h = make_harness(LOOP, {**SCRIPT, "reviewer": [review(["A"]), review(["B"]), review()]})
    assert start(h).pending.id == "build-review-exhausted"

    state = decide(h, Reject("build-review-exhausted", LEAD, "smaller change"))
    assert state.status == "completed", state.pending


# ── the end of the run ────────────────────────────────────────────────────────


def test_a_run_ending_on_a_failing_qa_pauses_instead_of_completing(make_harness, with_qa):
    published = []
    h = make_harness(
        QA_THEN_PUBLISH,
        {**SCRIPT, "qa": [QA_FAILS]},
        actions={"open_pr": lambda state: published.append(state.run_id) or "opened"},
    )

    state = start(h)
    assert (state.status, state.pending.kind, state.pending.id) == (
        "paused",
        "pause",
        FAILING_PAUSE,
    )
    assert "test-report" in state.pending.reason and "verdict 'fail'" in state.pending.reason
    assert "tests/test_export.py::test_csv" in state.pending.reason
    assert published == []
    assert "run_closed" not in [e["event"] for e in h.ledger("r-1").events()]


def test_resume_rechecks_and_stop_ends_it(make_harness, with_qa):
    h = make_harness(QA_THEN_PUBLISH, {**SCRIPT, "qa": [QA_FAILS]}, actions={"open_pr": str})
    start(h)

    assert run(h.engine.resume("r-1")).pending.id == FAILING_PAUSE
    state = run(h.engine.stop("r-1", LEAD, "QA failed; fixing by hand"))
    assert state.status == "stopped"


def test_a_passing_run_completes_as_before(make_harness, with_qa):
    h = make_harness(QA_THEN_PUBLISH, {**SCRIPT, "qa": [QA_PASSES]}, actions={"open_pr": str})

    state = start(h)
    assert state.status == "completed", state.pending
    assert [r.step for r in state.history][-1] == "publish"
