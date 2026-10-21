"""Proposed AGENTS.md additions in the PR body are agent text (DF-177): rendered as
inline code, so no image, link, @mention or HTML in them renders, and bounded in
number and length."""

import re
from datetime import UTC, datetime

from agentic.engine.state import Cursor, RunState
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
