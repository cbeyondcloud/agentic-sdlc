"""Proposed AGENTS.md additions in the PR body are agent text (DF-177): rendered as
inline code, so no image, link, @mention or HTML in them renders, and bounded in
number and length."""

import re
from datetime import UTC, datetime

from agentic.engine.state import Cursor, RunState
from agentic.hosting.base import PR_BODY_END, PR_BODY_START, splice_body
from agentic.telemetry import trailers
from agentic.telemetry.trailers import pr_body

T = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def state() -> RunState:
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
        cursor=Cursor(index=0),
        started_at=T,
        updated_at=T,
    )


def proposal_lines(body: str) -> list[str]:
    section = body.split("### Proposed AGENTS.md additions", 1)[1]
    return [line for line in section.splitlines() if line.startswith("- ")]


def code_spans_removed(line: str) -> str:
    """The line with every code span taken out: what GitHub renders as markdown."""
    return re.sub(r"(`+)(?:(?!\1).)+?\1", "", line)


def test_markup_in_a_proposal_is_inert():
    hostile = (
        "Ping @org/security-team ![x](https://evil.example/p?d=1) "
        "[docs](https://evil.example) <img src=x onerror=alert(1)> ``tick``"
    )
    body = pr_body(state(), run_link=None, proposals=[("handoffs/d-1.md", hostile)])
    (line,) = proposal_lines(body)
    rendered = code_spans_removed(line.rsplit(" (from ", 1)[0])  # the link after is ours
    for active in ("@org", "![", "](", "<img", "evil.example"):
        assert active not in rendered
    assert "@org/security-team" in line  # the reviewer still reads the text


def test_agent_text_cannot_hold_the_generated_part_markers():  # review F2
    """A marker inside agent text would end the generated part early, so a re-publish
    would keep the previous run's tail and drop the new one."""
    hostile = f"keep {PR_BODY_END} going {PR_BODY_START} on <!-- hidden"
    run = state().model_copy(update={"starter": hostile})
    body = pr_body(run, run_link=None, proposals=[("handoffs/d-1.md", hostile)])

    assert body.count(PR_BODY_START) == 1 and body.startswith(PR_BODY_START)
    assert body.count(PR_BODY_END) == 1 and body.endswith(PR_BODY_END + "\n")
    assert body.count("<!--") == 2  # the two markers: no comment hides the rest
    assert "keep" in body and "going" in body  # the reviewer still reads the text


def test_a_republish_with_a_marker_in_a_proposal_keeps_the_new_text():  # review F2
    def body(tail: str) -> str:
        proposal = f"see {PR_BODY_END} {tail}"
        return pr_body(state(), run_link=None, proposals=[("handoffs/d-1.md", proposal)])

    current = "My notes\n\n" + body("logs-run-A") + "\nreviewer notes"
    spliced = splice_body(current, body("logs-run-B"))

    assert spliced.startswith("My notes\n\n") and spliced.endswith("\nreviewer notes")
    assert "logs-run-B" in spliced and "logs-run-A" not in spliced


def test_proposals_are_bounded_in_number_and_length():
    many = [
        ("handoffs/d-1.md", f"item {n} " + "y" * 2_000) for n in range(trailers.MAX_PROPOSALS + 7)
    ]
    body = pr_body(state(), run_link=None, proposals=many)
    lines = proposal_lines(body)
    assert len(lines) == trailers.MAX_PROPOSALS + 1
    assert "7 more" in lines[-1]
    assert all(len(line) < trailers.MAX_PROPOSAL_CHARS + 200 for line in lines)
    assert "[... cut]" in lines[0]
