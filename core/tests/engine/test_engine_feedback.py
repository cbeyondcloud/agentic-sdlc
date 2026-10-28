"""The engine reports each gate correction as a feedback record (M3-11): a rejection with
feedback, and an edit's diff. A plain approval is not a correction; a failing feedback
port never stops a decision the gate ledger already holds."""

import asyncio

from _engine_support import answer

from agentic.engine.gates import Approve, Edit, Reject, StandaloneAuthorizer

GATED = """\
name: test
version: 1.0.0
steps:
  - id: requirements
    agent: ba
  - id: design
    agent: designer
  - gate: design-approval
    approver_role: team-lead
  - id: develop
    agent: dev
"""
STARTER = "dev@acme.example"
LEAD = "lead@acme.example"


def run(coro):
    return asyncio.run(coro)


def at_gate(make_harness, sink, script=None):
    h = make_harness(
        GATED,
        script
        or {
            "ba": [answer("requirements")],
            "designer": [answer("design"), answer("design")],
            "dev": [answer("dev-report")],
        },
        feedback=sink,
        repo="github.com/acme/app",
    )
    state = run(h.engine.start("test", {"request": "Add CSV export"}, STARTER, "r-1"))
    assert state.pending.id == "design-approval"
    return h, state


def decide(h, decision):
    return run(
        h.engine.decide(
            "r-1", decision, authorizer=StandaloneAuthorizer(), self_approval=lambda a, s: None
        )
    )


def test_reject_reports_a_gate_rejection_about_the_step_sent_back(make_harness):
    seen = []
    h, _ = at_gate(make_harness, seen.append)

    decide(h, Reject("design-approval", LEAD, "Use the existing queue, don't add a new one."))
    assert len(seen) == 1
    record = seen[0]
    assert record.signal == "gate-rejection"
    assert (record.gate, record.step, record.kind) == ("design-approval", "design", "design")
    assert record.agent == "designer@1.0.0"
    assert record.actor == LEAD
    assert record.text == "Use the existing queue, don't add a new one."
    assert record.repo == "github.com/acme/app"
    assert record.run_id == "r-1"


def test_edit_reports_a_gate_edit_with_the_diff(make_harness):
    seen = []
    h, _ = at_gate(make_harness, seen.append)
    text = (h.ledger("r-1").run_dir / "handoffs" / "design-1.md").read_text(encoding="utf-8")
    edited = text.replace(
        "summary: One line saying what this handoff concludes.", "summary: Use the existing queue."
    )

    decide(h, Edit("design-approval", LEAD, "handoffs/design-1.md", edited, notes="reuse it"))
    assert [r.signal for r in seen] == ["gate-edit"]
    record = seen[0]
    assert (record.kind, record.agent, record.path) == (
        "design",
        "designer@1.0.0",
        "handoffs/design-1.md",
    )
    assert "+summary: Use the existing queue." in record.diff
    assert record.text == "reuse it"


def test_plain_approve_reports_nothing(make_harness):
    seen = []
    h, _ = at_gate(make_harness, seen.append)

    assert decide(h, Approve("design-approval", LEAD)).status == "completed"
    assert seen == []


def test_a_failing_feedback_port_never_stops_the_decision(make_harness, caplog):
    def broken(record):
        raise OSError("disk full")

    h, _ = at_gate(make_harness, broken)

    state = decide(h, Reject("design-approval", LEAD, "use the existing queue"))
    assert state.pending.id == "design-approval"  # sent back, redone, waiting again
    assert h.gates["r-1"].entries[0]["verdict"] == "reject"
    assert "feedback" in caplog.text and "disk full" in caplog.text
