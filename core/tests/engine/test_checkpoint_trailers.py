"""Every agent step's checkpoint commit carries the audit trailers (M1-45; spec §14.5)."""

import asyncio

from _engine_support import FakeTrees, answer

from agentic.engine.gates import Approve, StandaloneAuthorizer
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
