"""What the CLI says about a run that could not complete on a failing handoff (DF-238)."""

from datetime import UTC, datetime

import pytest

from agentic.engine.engine import FAILING_PAUSE
from agentic.engine.gates import GateRefused
from agentic.engine.state import Cursor, Pending, RunState
from agentic_cli.driving import engine_errors, exit_code_for, next_steps
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def paused(pause_id: str) -> RunState:
    return RunState(
        run_id="r-1",
        pipeline="feature-delivery",
        pipeline_version="1.0.0",
        pipeline_digest="sha256:0",
        policy_version="1",
        catalog_digest="sha256:0",
        regime="team",
        starter="dev@example.invalid",
        status="paused",
        cursor=Cursor(index=3),
        pending=Pending(kind="pause", id=pause_id, reason="test-report: verdict 'fail'", since=NOW),
        started_at=NOW,
        updated_at=NOW,
    )


def test_a_failing_handoff_pause_offers_show_and_stop_and_exits_4():
    state = paused(FAILING_PAUSE)

    assert exit_code_for(state) == ExitCode.PAUSED
    steps = next_steps(state)
    assert steps[0] == "agentic show r-1"
    assert steps[1].startswith("agentic stop r-1 --reason")
    assert not any(step.startswith("agentic resume") for step in steps)  # it would pause again


def at_gate(gate: str, step: str) -> RunState:
    state = paused("unused")
    pending = Pending(kind="gate", id=gate, reason="waiting", since=NOW, step=step)
    return state.model_copy(update={"status": "awaiting_gate", "pending": pending})


def test_an_exhausted_loop_gate_suggests_edit_and_reject_before_approve():
    """DF-321 (DF-266): a bare approval of an exhausted loop is refused, so it is not the
    first thing to suggest."""
    steps = next_steps(at_gate("build-review-exhausted", "build-review"))
    decide = next(step for step in steps if "approve" in step or "edit" in step)
    assert decide.index("agentic edit") < decide.index("reject") < decide.index("approve")


def test_a_pipeline_gate_still_suggests_approve_first():
    steps = next_steps(at_gate("review-approval", "review-approval"))
    decide = next(step for step in steps if "approve" in step)
    assert decide.startswith("agentic approve r-1")


def test_refusing_a_bare_approval_of_an_exhausted_loop_hints_at_edit_reject_and_stop():
    message = (
        "build-review: its exit condition 'review.open_blocking == 0' still does not hold - "
        "edit a pinned handoff so that it holds"
    )
    with pytest.raises(CliError) as refused:
        with engine_errors():
            raise GateRefused(message)
    assert refused.value.code == ExitCode.REFUSED
    assert all(word in refused.value.hint for word in ("agentic edit", "reject", "agentic stop"))
    assert "accepted_failures" in refused.value.hint  # review F3: how a QA failure is accepted
