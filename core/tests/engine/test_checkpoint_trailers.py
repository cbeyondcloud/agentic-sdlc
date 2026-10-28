"""Every agent step's checkpoint commit carries the audit trailers (M1-45; spec §14.5)."""

import asyncio

from _engine_support import FakeTrees, answer

from agentic.engine.gates import Approve, Reject, StandaloneAuthorizer
from agentic.telemetry.trailers import TRAILER_KEYS, parse_trailers

PIPELINE = """\
name: test
version: 1.0.0
regime: team
steps:
  - id: design
    agent: designer
  - gate: design-approval
    approver_role: team-lead
  - id: develop
    agent: dev
"""


def test_checkpoint_commits_carry_the_trailers(make_harness):
    trees = FakeTrees()
    harness = make_harness(
        PIPELINE,
        {"designer": [answer("design")], "dev": [answer("dev-report")]},
        trees_for=lambda run_id: trees,
    )
    state = asyncio.run(harness.engine.start("test", {"request": "x"}, "asha@example.com", "r-tr"))
    assert state.pending.id == "design-approval"
    state = asyncio.run(
        harness.engine.decide(
            "r-tr",
            Approve("design-approval", "ravi@example.com"),
            authorizer=StandaloneAuthorizer(),
            self_approval=lambda approver, starter: None,
        )
    )
    assert state.status == "completed"
    assert [a.approver for a in state.approvals] == ["ravi@example.com"]

    design, develop = [parse_trailers(call[1]) for call in trees.calls if call[0] == "finish"]
    for trailers in (design, develop):
        assert list(trailers) == list(TRAILER_KEYS)
        assert trailers["Agentic-Run"] == ["r-tr"]
        assert trailers["Agentic-Triggered-By"] == ["asha@example.com"]
    assert design["Agentic-Step"] == ["design (iteration 1)"]
    assert design["Agentic-Agent"] == ["designer@1.0.0"]
    assert design["Agentic-Approved-By"] == ["none"]
    assert develop["Agentic-Agent"] == ["dev@1.0.0"]
    assert develop["Agentic-Approved-By"] == ["ravi@example.com (gate: design-approval)"]
    assert develop["Agentic-Handoff"] == ["handoffs/design-1.md"]  # what it consumed: the why


def decide(harness, run_id, decision):
    return asyncio.run(
        harness.engine.decide(
            run_id,
            decision,
            authorizer=StandaloneAuthorizer(),
            self_approval=lambda approver, starter: None,
        )
    )


def test_handoff_trailer_is_repo_relative_when_a_prefix_is_given(make_harness):
    """DF-174: spec 14.5 shows the repo-relative path, readable without Agentic-Run."""
    trees = FakeTrees()
    harness = make_harness(
        PIPELINE,
        {"designer": [answer("design")], "dev": [answer("dev-report")]},
        trees_for=lambda run_id: trees,
        handoff_prefix=".agentic/local/runs",
    )
    asyncio.run(harness.engine.start("test", {"request": "x"}, "asha@example.com", "r-tr"))
    decide(harness, "r-tr", Approve("design-approval", "ravi@example.com"))
    develop = parse_trailers([c[1] for c in trees.calls if c[0] == "finish"][-1])
    assert develop["Agentic-Handoff"] == [".agentic/local/runs/r-tr/handoffs/design-1.md"]


REJECT_TO_DESIGN = (
    PIPELINE
    + """\
  - gate: dev-approval
    approver_role: approver
    on_reject: design
"""
)


def test_a_redone_step_does_not_cite_a_superseded_approval(make_harness):
    """DF-173: the design approval approved the design the rejection threw away, so the
    redone design's commit cites no approval; the next approval is cited again."""
    trees = FakeTrees()
    harness = make_harness(
        REJECT_TO_DESIGN,
        {
            "designer": [answer("design"), answer("design", summary="v2")],
            "dev": [answer("dev-report"), answer("dev-report")],
        },
        trees_for=lambda run_id: trees,
    )
    asyncio.run(harness.engine.start("test", {"request": "x"}, "asha@example.com", "r-rj"))
    decide(harness, "r-rj", Approve("design-approval", "ravi@example.com"))
    state = decide(harness, "r-rj", Reject("dev-approval", "lead@example.com", "redo it"))
    assert state.pending.id == "design-approval"
    assert [a.superseded for a in state.approvals] == [True]
    state = decide(harness, "r-rj", Approve("design-approval", "lead@example.com"))

    finishes = [parse_trailers(c[1]) for c in trees.calls if c[0] == "finish"]
    approved = [(t["Agentic-Step"][0], t["Agentic-Approved-By"][0]) for t in finishes]
    assert approved == [
        ("design (iteration 1)", "none"),
        ("develop (iteration 1)", "ravi@example.com (gate: design-approval)"),
        ("design (iteration 1)", "none"),  # redone: ravi's approval no longer stands
        ("develop (iteration 1)", "lead@example.com (gate: design-approval)"),
    ]


def test_a_rejection_back_to_a_later_step_keeps_the_earlier_approval(make_harness):
    """Rejected back to develop, the design approval still stands for the redone develop."""
    trees = FakeTrees()
    harness = make_harness(
        REJECT_TO_DESIGN.replace("on_reject: design", "on_reject: develop"),
        {"designer": [answer("design")], "dev": [answer("dev-report"), answer("dev-report")]},
        trees_for=lambda run_id: trees,
    )
    asyncio.run(harness.engine.start("test", {"request": "x"}, "asha@example.com", "r-rk"))
    decide(harness, "r-rk", Approve("design-approval", "ravi@example.com"))
    state = decide(harness, "r-rk", Reject("dev-approval", "lead@example.com", "redo it"))
    assert [a.superseded for a in state.approvals] == [False]
    last = parse_trailers([c[1] for c in trees.calls if c[0] == "finish"][-1])
    assert last["Agentic-Approved-By"] == ["ravi@example.com (gate: design-approval)"]
