"""The pull request signals (M3-11): review comments / changes requested on an agent-made
pull request, and review-agent misses - from recorded GitHub answers through the adapter."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx2

from agentic.handoffs.kinds import Finding
from agentic.hosting import RepoRef
from agentic.hosting.github import GITHUB_COM_API, GitHubHost
from agentic.learning.capture import pr_feedback, review_misses

AT = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
TOKEN = "gho_" + "Fake" + "Learn" + "9876543210"  # gitleaks:allow - built from pieces
RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "hosting" / "github"
REPO = "github.com/acme/app"


def recorded_pr():
    def handler(request: httpx2.Request) -> httpx2.Response:
        name = (
            "pr-7-reviews.json" if request.url.path.endswith("/reviews") else "pr-7-comments.json"
        )
        return httpx2.Response(200, json=json.loads((RECORDED / name).read_text("utf-8")))

    github = GitHubHost(GITHUB_COM_API, TOKEN, transport=httpx2.MockTransport(handler))
    ref = RepoRef("github.com", "acme", "app")
    return github.pull_request_reviews(ref, 7), github.pull_request_review_comments(ref, 7)


def records(tdd_skipped=None):
    reviews, comments = recorded_pr()
    return pr_feedback(
        run_id="r-1",
        repo=REPO,
        number=7,
        reviews=reviews,
        comments=comments,
        agent_logins=["agentic-bot[bot]"],
        at=AT,
        tdd_skipped=tdd_skipped,
    )


def test_pr_review_comments_become_records():
    found = records()
    assert [(r.signal, r.actor, r.path, r.line) for r in found] == [
        ("pr-review", "alice", None, None),  # the changes-requested review's own words
        ("pr-review", "alice", "billing/export.py", 42),
        ("pr-review", "carol", "billing/export.py", 88),
        ("pr-review", "carol", "docs/billing.md", None),
    ]
    review, inline = found[0], found[1]
    assert review.review_state == "changes_requested"
    assert review.text == "Please wrap the HTTP calls in our with_retry() helper."
    assert review.source == "pr:github.com/acme/app#7:review:101"
    assert inline.text == "Wrap this call in our `with_retry()` helper."
    assert inline.review_state == "changes_requested"
    assert found[2].review_state == "commented"
    assert inline.given_at == datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    assert all(r.repo == REPO and r.run_id == "r-1" for r in found)


def test_approvals_and_bot_comments_are_not_corrections():
    sources = {r.source for r in records()}
    assert "pr:github.com/acme/app#7:review:103" not in sources  # LGTM approval
    assert "pr:github.com/acme/app#7:review:102" not in sources  # the agent's own bot
    assert "pr:github.com/acme/app#7:comment:203" not in sources  # a reply, by the bot
    assert "pr:github.com/acme/app#7:review:104" not in sources  # an empty review body


def test_capturing_twice_gives_the_same_ids():
    assert [r.id for r in records()] == [r.id for r in records()]


def test_tdd_skipped_is_carried_for_attribution():
    assert {r.tdd_skipped for r in records(tdd_skipped=True)} == {True}


def test_review_miss_on_lines_the_review_passed():
    findings = [Finding(severity="medium", file="billing/export.py", line=90, issue="x")]
    misses = review_misses(records(), findings, reviewer="code-review@1.0.0")
    # line 42 has no finding near it; line 88 is within 3 of the finding at 90; the
    # outdated docs comment has no line any more
    assert [(m.signal, m.path, m.line, m.agent, m.kind) for m in misses] == [
        ("review-miss", "billing/export.py", 42, "code-review@1.0.0", "review")
    ]
    assert misses[0].id != records()[1].id  # its own record, from the same comment


def test_comment_near_a_review_finding_is_not_a_miss():
    findings = [Finding(severity="low", file="billing/export.py", line=44, issue="x")]
    misses = review_misses(records(), findings, reviewer="code-review@1.0.0")
    assert [m.line for m in misses] == [88]


def test_no_review_in_the_run_means_no_miss():
    assert review_misses(records(), [], reviewer=None) == []
