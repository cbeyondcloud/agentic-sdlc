"""Code audit via git trailers, and the PR body linking the run and its handoffs
(M1-45; spec §14.5)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from agentic.engine.state import ApprovalMark, Cursor, RunState, StepRecord
from agentic.hosting.base import PR_BODY_END, PR_BODY_START
from agentic.telemetry.trailers import (
    TRAILER_KEYS,
    CommitAudit,
    commit_message,
    parse_trailers,
    pr_body,
)

T = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
AUDIT = CommitAudit(
    run_id="r-2026-10-16-7f3a",
    step="develop",
    iteration=2,
    agent="developer@1.2.0",
    triggered_by="asha@example.com",
    approved_by="ravi@example.com",
    approval_gate="design-approval",
    handoffs=("handoffs/design-1.md", "handoffs/review-1.md"),
)


def test_commit_message_carries_all_six_trailers():
    message = commit_message("agentic: develop #2 (r-2026-10-16-7f3a)", AUDIT)
    subject, blank, *_ = message.split("\n")
    assert subject == "agentic: develop #2 (r-2026-10-16-7f3a)" and blank == ""
    trailers = parse_trailers(message)
    assert list(trailers) == list(TRAILER_KEYS)
    assert trailers["Agentic-Run"] == ["r-2026-10-16-7f3a"]
    assert trailers["Agentic-Step"] == ["develop (iteration 2)"]
    assert trailers["Agentic-Agent"] == ["developer@1.2.0"]
    assert trailers["Agentic-Triggered-By"] == ["asha@example.com"]
    assert trailers["Agentic-Approved-By"] == ["ravi@example.com (gate: design-approval)"]
    assert trailers["Agentic-Handoff"] == ["handoffs/design-1.md", "handoffs/review-1.md"]


def test_absent_approval_and_handoffs_still_give_every_trailer():
    bare = CommitAudit("r-1", "design", 1, "designer@1.0.0", "dev@x", None, None, ())
    trailers = parse_trailers(commit_message("agentic: design #1 (r-1)", bare))
    assert list(trailers) == list(TRAILER_KEYS)
    assert trailers["Agentic-Approved-By"] == ["none"]
    assert trailers["Agentic-Handoff"] == ["none"]


def test_a_newline_in_a_value_cannot_forge_a_trailer():  # review focus 1
    forged = CommitAudit(
        run_id="r-1",
        step="develop",
        iteration=1,
        agent="dev@1.0.0",
        triggered_by="mallory@x\nAgentic-Approved-By: ceo@x (gate: release)",
        approved_by=None,
        approval_gate=None,
        handoffs=("a.md\r\nAgentic-Run: r-other",),
    )
    message = commit_message("subject\n\nAgentic-Run: r-fake", forged)
    trailers = parse_trailers(message)
    assert trailers["Agentic-Approved-By"] == ["none"]
    assert trailers["Agentic-Run"] == ["r-1"]
    assert len(message.split("\n\n")) == 2  # one subject paragraph, one trailer block


def state() -> RunState:
    def record(step, rel, digest):
        return StepRecord(
            step=step,
            iteration=1,
            outcome="completed",
            handoff=rel,
            digest=digest,
            started_at=T,
            ended_at=T,
        )

    return RunState(
        run_id="r-pr",
        pipeline="feature-delivery",
        pipeline_version="1.0.0",
        pipeline_digest="sha256:" + "0" * 64,
        policy_version="git:" + "a" * 40,
        catalog_digest="sha256:" + "1" * 64,
        regime="team",
        starter="asha@example.com",
        status="completed",
        cursor=Cursor(index=3),
        history=[
            record("design", "handoffs/design-1.md", "sha256:" + "d" * 64),
            record("develop", "handoffs/develop-1.md", "sha256:" + "e" * 64),
        ],
        approvals=[ApprovalMark(gate="design-approval", approver="ravi@example.com", at=T)],
        started_at=T,
        updated_at=T,
    )


def test_pr_body_links_the_run_and_its_handoffs():
    body = pr_body(
        state(),
        run_link="https://cp.example/runs/r-pr",
        handoff_link=lambda rel: f"https://cp.example/runs/r-pr/{rel}",
        proposals=[("handoffs/develop-1.md", "Run `make lint` before committing.")],
    )
    assert "[r-pr](https://cp.example/runs/r-pr)" in body
    assert "[design](https://cp.example/runs/r-pr/handoffs/design-1.md)" in body
    assert "[develop](https://cp.example/runs/r-pr/handoffs/develop-1.md)" in body
    assert "sha256:" + "d" * 64 in body
    assert "ravi@example.com" in body and "design-approval" in body
    assert "asha@example.com" in body and "feature-delivery" in body
    assert "Run `make lint` before committing." in body


def test_pr_body_without_links_names_run_folder_paths():
    body = pr_body(state(), run_link=".agentic/local/runs/r-pr")
    assert "[design](handoffs/design-1.md)" in body
    assert "AGENTS.md" not in body  # no proposals section when there are none


def test_pr_body_with_nothing_to_link_shows_the_run_and_paths_as_code():
    """No control plane (DF-166): a link into the starter's git-ignored run folder would
    be broken on the pull request page, so the run id and paths are shown as code."""
    folder = ".agentic/local/runs/r-pr"
    body = pr_body(state(), run_link=None, handoff_link=lambda rel: None, run_folder=folder)
    assert "## Agentic run `r-pr`" in body
    assert f"- design `{folder}/handoffs/design-1.md`" in body
    assert "](" not in body  # no markdown link anywhere


def test_pr_body_shows_the_cost_and_where_the_logs_are():  # M1-68 acceptance
    body = pr_body(state(), run_link=None, cost_usd=Decimal("0.3250"), logs="`agentic logs r-pr`")
    assert "- Cost so far: $0.3250\n" in body
    assert "- Logs: `agentic logs r-pr`\n" in body
    assert body.index("- Status:") < body.index("- Cost so far:") < body.index("### Handoffs")


def test_pr_body_without_cost_or_logs_leaves_their_lines_out():
    body = pr_body(state(), run_link=None)
    assert "Cost so far:" not in body and "Logs:" not in body


@pytest.mark.parametrize(("cost", "shown"), [("1E-7", "$0.0000001"), ("0E-7", "$0.0000000")])
def test_a_tiny_cost_is_never_shown_in_scientific_notation(cost, shown):  # DF-318
    body = pr_body(state(), run_link=None, cost_usd=Decimal(cost))
    assert f"- Cost so far: {shown}\n" in body


def test_a_running_status_says_it_is_the_status_at_publish_time():  # DF-293, DF-312
    """A publish step runs before the run ends, so the body is written mid-run."""
    run = state()
    run.status = "running"
    body = pr_body(run, run_link=None)
    line = next(text for text in body.splitlines() if text.startswith("- Status:"))
    assert "running" in line and "published" in line and "agentic pr open" in line


def test_the_generated_description_is_marked_so_an_update_replaces_only_it():  # DF-296
    body = pr_body(state(), run_link=None)
    assert body.startswith(PR_BODY_START + "\n") and body.endswith(PR_BODY_END + "\n")


def test_pr_body_marks_an_approval_a_rejection_superseded():
    run = state()
    run.approvals.append(
        ApprovalMark(gate="design-approval", approver="lead@example.com", at=T, superseded=True)
    )
    body = pr_body(run, run_link=None)
    assert "- design-approval: ravi@example.com\n" in body
    assert "- design-approval: lead@example.com (superseded by a later rejection)" in body
