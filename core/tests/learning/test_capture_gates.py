"""The two gate signals (M3-11): a rejection with feedback, and an edit's diff."""

from datetime import UTC, datetime

from agentic.learning.capture import HandoffRef, gate_edit, gate_rejection

AT = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
DESIGN = HandoffRef(path="handoffs/design-1.md", kind="design", agent="design@1.0.0", digest="d1")
REQS = HandoffRef(
    path="handoffs/requirements-1.md",
    kind="requirements",
    agent="business-analyst@1.0.0",
    digest="r1",
)


def test_gate_rejection_becomes_a_feedback_record():
    found = gate_rejection(
        run_id="r-1",
        gate="design-review",
        step="design",
        approver="lead@example.test",
        feedback="Use the existing queue, don't add a new one.",
        handoff=DESIGN,
        pinned=[REQS, DESIGN],
        at=AT,
        repo="github.com/acme/app",
    )
    assert found.signal == "gate-rejection"
    assert (found.gate, found.step, found.kind, found.agent) == (
        "design-review",
        "design",
        "design",
        "design@1.0.0",
    )
    assert found.actor == "lead@example.test"
    assert found.text == "Use the existing queue, don't add a new one."
    assert found.repo == "github.com/acme/app"
    assert found.source.startswith("gate:design-review:reject:")
    again = gate_rejection(
        run_id="r-1",
        gate="design-review",
        step="design",
        approver="other@example.test",
        feedback="different words",
        handoff=DESIGN,
        pinned=[DESIGN, REQS],
        at=AT,
    )
    assert again.id == found.id  # the same pinned bytes rejected: one correction


def test_gate_edit_records_the_diff():
    before = "---\nkind: requirements\n---\n# Requirements\n\n- AC1: export runs nightly\n"
    after = "---\nkind: requirements\n---\n# Requirements\n\n- AC1: export runs hourly\n"
    found = gate_edit(
        run_id="r-1",
        gate="requirements-review",
        step="requirements",
        approver="lead@example.test",
        handoff=REQS,
        before=before,
        after=after,
        notes="hourly, not nightly",
        at=AT,
    )
    assert found.signal == "gate-edit"
    assert (found.kind, found.agent) == ("requirements", "business-analyst@1.0.0")
    assert "-- AC1: export runs nightly" in found.diff
    assert "+- AC1: export runs hourly" in found.diff
    assert found.diff.startswith("--- a/handoffs/requirements-1.md")
    assert found.text == "hourly, not nightly"
    assert found.path == "handoffs/requirements-1.md"


def test_gate_edit_with_crlf_and_no_notes_has_a_text_from_the_diff():
    found = gate_edit(
        run_id="r-1",
        gate="g",
        step="s",
        approver="a@example.test",
        handoff=REQS,
        before="a\r\nb\r\n",
        after="a\nc\n",
        notes="",
        at=AT,
    )
    assert found.diff.count("\r") == 0
    assert found.text == "c"
