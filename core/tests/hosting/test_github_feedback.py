"""Reading an agent-made pull request's reviews for the learning loop (M3-11): reviews and
inline review comments, from recorded GitHub REST API answers (no live call)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx2

from agentic.hosting import RepoRef
from agentic.hosting.base import FeedbackHost
from agentic.hosting.github import GITHUB_COM_API, GitHubHost

TOKEN = "gho_" + "Fake" + "Learn" + "0123456789"  # gitleaks:allow - built from pieces
REPO = RepoRef("github.com", "acme", "app")
RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "hosting" / "github"


def recorded(name: str) -> list[dict]:
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


def host(handler) -> GitHubHost:
    return GitHubHost(GITHUB_COM_API, TOKEN, transport=httpx2.MockTransport(handler))


def serving(seen: list | None = None):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/repos/acme/app/pulls/7/reviews":
            return httpx2.Response(200, json=recorded("pr-7-reviews.json"))
        if request.url.path == "/repos/acme/app/pulls/7/comments":
            return httpx2.Response(200, json=recorded("pr-7-comments.json"))
        return httpx2.Response(404, json={"message": "Not Found"})

    return handler


def test_github_host_is_a_feedback_host():
    assert isinstance(host(serving()), FeedbackHost)


def test_reviews_are_read_with_state_and_author():
    seen = []
    reviews = host(serving(seen)).pull_request_reviews(REPO, 7)
    assert [(r.id, r.author, r.state) for r in reviews] == [
        (101, "alice", "changes_requested"),
        (102, "agentic-bot[bot]", "commented"),
        (103, "bob", "approved"),
        (104, "carol", "commented"),
    ]
    assert reviews[0].body == "Please wrap the HTTP calls in our with_retry() helper."
    assert reviews[0].submitted_at == datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    assert reviews[0].commit == "1" * 40
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert seen[0].url.params["per_page"] == "100"


def test_review_comments_carry_path_and_line():
    comments = host(serving()).pull_request_review_comments(REPO, 7)
    first = comments[0]
    assert (first.id, first.review_id, first.author, first.path, first.line) == (
        201,
        101,
        "alice",
        "billing/export.py",
        42,
    )
    assert first.in_reply_to is None
    assert comments[2].in_reply_to == 201
    assert comments[3].line is None  # outdated: the line is gone from the newest diff
    assert first.created_at == datetime(2026, 10, 24, 10, 0, tzinfo=UTC)


def test_review_listing_follows_pages():
    rows = recorded("pr-7-reviews.json")

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.params.get("page") == "2":
            return httpx2.Response(200, json=rows[2:])
        nxt = f"{GITHUB_COM_API}/repos/acme/app/pulls/7/reviews?per_page=100&page=2"
        return httpx2.Response(200, json=rows[:2], headers={"Link": f'<{nxt}>; rel="next"'})

    assert [r.id for r in host(handler).pull_request_reviews(REPO, 7)] == [101, 102, 103, 104]


def test_a_page_link_to_another_origin_is_refused():
    def handler(request: httpx2.Request) -> httpx2.Response:
        nxt = "https://evil.example/repos/acme/app/pulls/7/reviews?page=2"
        return httpx2.Response(200, json=[], headers={"Link": f'<{nxt}>; rel="next"'})

    import pytest

    from agentic.hosting import HostingError

    with pytest.raises(HostingError):
        host(handler).pull_request_reviews(REPO, 7)
