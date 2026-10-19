"""Tests for gate decisions: approve, reject with feedback, edit then approve; regimes and
four-eyes; digest pinning (M1-22; spec §6, §6.1, §7.3)."""

import asyncio

import pytest
from conftest import FakeGates, answer, review

from agentic.engine.gates import Approve, Edit, GateRefused, Reject, StandaloneAuthorizer

GATED = """\
name: test
version: 1.0.0
regime: {regime}
steps:
  - id: design
    agent: designer
  - gate: design-approval
    approver_role: team-lead
    four_eyes: {four_eyes}
  - id: develop
    agent: dev
"""

STARTER = "dev@acme.example"
LEAD = "lead@acme.example"


def run(coro):
    return asyncio.run(coro)


def at_gate(make_harness, regime="team", four_eyes="false", script=None):
    h = make_harness(
        GATED.format(regime=regime, four_eyes=four_eyes),
        script or {"designer": [answer("design")], "dev": [answer("dev-report")]},
    )
    state = run(h.engine.start("test", {"request": "Add CSV export"}, STARTER, "r-1"))
    assert state.pending.id == "design-approval"
    return h, state


def decide(h, decision, authorizer=None, self_approval=None):
    return run(
        h.engine.decide(
            "r-1",
            decision,
            authorizer=authorizer or StandaloneAuthorizer(),
            self_approval=self_approval or (lambda approver, starter: None),
        )
    )


def four_eyes_rule(approver, starter):
    return "you cannot approve your own run" if approver.lower() == starter.lower() else None


# ── approve ───────────────────────────────────────────────────────────────────


def test_approve_pins_records_and_continues(make_harness):
    h, state = at_gate(make_harness)
    pinned = dict(state.pending.artefacts)

    done = decide(h, Approve(LEAD))
    assert done.status == "completed" and [r.step for r in done.history][-1] == "develop"
    entries = h.gates["r-1"].entries
    assert len(entries) == 1 and entries[0]["artifacts"] == pinned
    assert entries[0]["verdict"] == "approve" and entries[0]["approver"] == LEAD


def test_a_handoff_changed_before_the_decision_is_refused(make_harness):  # RF 3
    h, state = at_gate(make_harness)
    (h.ledger("r-1").run_dir / "handoffs" / "design-1.md").write_text("changed", encoding="utf-8")

    with pytest.raises(GateRefused, match="design-1.md"):
        decide(h, Approve(LEAD))
    assert h.store.load("r-1").status == "awaiting_gate"
    assert h.gates["r-1"].entries == []


def test_a_change_after_approval_reopens_the_gate(make_harness):  # RF 3
    h, _ = at_gate(make_harness)
    gates = h.gates["r-1"]
    real_record = FakeGates.record

    def record_then_tamper(self, *args, **kwargs):
        result = real_record(self, *args, **kwargs)
        (h.ledger("r-1").run_dir / "handoffs" / "design-1.md").write_text("x", encoding="utf-8")
        return result

    gates.record = record_then_tamper.__get__(gates)
    state = decide(h, Approve(LEAD))
    assert (state.status, state.pending.id) == ("awaiting_gate", "design-approval")
    assert "changed since" in state.pending.reason


# ── edit ──────────────────────────────────────────────────────────────────────


def test_a_broken_edit_is_refused_and_the_original_kept(make_harness):  # RF 4
    h, state = at_gate(make_harness)
    path = h.ledger("r-1").run_dir / "handoffs" / "design-1.md"
    before = path.read_bytes()
    broken = before.decode("utf-8").replace("risks:\n", "risks: [unclosed\n", 1)

    with pytest.raises(GateRefused, match="line"):
        decide(h, Edit(LEAD, "handoffs/design-1.md", broken))
    assert path.read_bytes() == before
    assert not any(p.name.startswith("design-1.md.edited") for p in path.parent.iterdir())


def test_an_edit_is_repinned_then_approved(make_harness):
    h, state = at_gate(make_harness)
    text = (h.ledger("r-1").run_dir / "handoffs" / "design-1.md").read_text(encoding="utf-8")
    edited = text.replace(
        "summary: One line saying what this handoff concludes.", "summary: Use the existing queue."
    )

    done = decide(h, Edit(LEAD, "handoffs/design-1.md", edited))
    entry = h.gates["r-1"].entries[0]
    assert list(entry["artifacts"]) == ["handoffs/design-1.md.edited-1.md"]
    assert done.latest["design"] == "handoffs/design-1.md.edited-1.md"
    assert done.status == "completed"


def test_an_edit_must_name_a_pinned_file(make_harness):
    h, _ = at_gate(make_harness)

    with pytest.raises(GateRefused, match="not one of"):
        decide(h, Edit(LEAD, "handoffs/other.md", "x"))


def test_an_edit_cannot_change_open_blocking_by_hand(make_harness):  # DF-42
    gated_review = """\
name: test
version: 1.0.0
steps:
  - id: review
    agent: reviewer
  - gate: review-approval
    approver_role: approver
"""
    h = make_harness(gated_review, {"reviewer": [review(["SQL injection"])]})
    run(h.engine.start("test", {}, STARTER, "r-1"))
    text = (h.ledger("r-1").run_dir / "handoffs" / "review-1.md").read_text(encoding="utf-8")

    with pytest.raises(GateRefused, match="open_blocking: 0 but the findings show 1"):
        decide(
            h,
            Edit(
                LEAD, "handoffs/review-1.md", text.replace("open_blocking: 1", "open_blocking: 0")
            ),
        )


# ── reject ────────────────────────────────────────────────────────────────────


def test_reject_routes_back_with_feedback(make_harness):
    h, _ = at_gate(
        make_harness,
        script={"designer": [answer("design"), answer("design")], "dev": [answer("dev-report")]},
    )

    state = decide(h, Reject(LEAD, "use the existing queue"))
    assert "use the existing queue" in h.runners.prompts["designer"][-1]
    assert (state.status, state.pending.id) == ("awaiting_gate", "design-approval")
    assert h.gates["r-1"].entries[0]["verdict"] == "reject"
    assert "handoffs/design-1-attempt2.md" in state.pending.artefacts


# ── who may decide ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("regime", "four_eyes", "refused"),
    [
        ("team", "true", True),
        ("regulated", "false", True),
        ("team", "false", False),
        ("solo", "false", False),
    ],
)
def test_four_eyes(make_harness, regime, four_eyes, refused):
    h, _ = at_gate(make_harness, regime=regime, four_eyes=four_eyes)

    if refused:
        with pytest.raises(GateRefused, match="own run"):
            decide(h, Approve(STARTER), self_approval=four_eyes_rule)
    else:
        assert decide(h, Approve(STARTER), self_approval=four_eyes_rule).status == "completed"


class NoRoles:
    def holds(self, user, role):
        return False


def test_a_role_is_required_outside_solo(make_harness):
    h, _ = at_gate(make_harness, regime="team")

    with pytest.raises(GateRefused, match="team-lead"):
        decide(h, Approve(LEAD), authorizer=NoRoles())


def test_the_starter_may_approve_under_solo_without_the_role(make_harness):
    h, _ = at_gate(make_harness, regime="solo")

    assert decide(h, Approve(STARTER), authorizer=NoRoles()).status == "completed"


def test_a_run_not_at_a_gate_is_refused(make_harness):
    h, _ = at_gate(make_harness)
    decide(h, Approve(LEAD))

    with pytest.raises(GateRefused, match="not waiting at a gate"):
        decide(h, Approve(LEAD))


# ── the other things a person settles at a gate ───────────────────────────────


TWO = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
  - id: develop
    agent: dev
"""


def test_invalid_output_is_settled_by_an_edit(make_harness):
    h = make_harness(TWO, {"designer": ["prose", "prose"], "dev": [answer("dev-report")]})
    state = run(h.engine.start("test", {}, STARTER, "r-1"))
    assert state.pending.id == "invalid-output"

    with pytest.raises(GateRefused, match="cannot be approved as it is"):
        decide(h, Approve(LEAD))
    done = decide(h, Edit(LEAD, "handoffs/design-1.invalid.md", answer("design")))
    assert done.status == "completed"
    assert done.latest["design"] == "handoffs/design-1.invalid.md.edited-1.md"


def test_invalid_output_can_be_rejected_back_to_the_step(make_harness):
    h = make_harness(
        TWO, {"designer": ["prose", "prose", answer("design")], "dev": [answer("dev-report")]}
    )
    run(h.engine.start("test", {}, STARTER, "r-1"))

    done = decide(h, Reject(LEAD, "write the handoff format"))
    assert done.status == "completed"
    assert "write the handoff format" in h.runners.prompts["designer"][-1]
