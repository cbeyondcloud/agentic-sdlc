"""Reading and reviewing pull requests (M2-46: skill submissions are pull requests that the
scope owner lists and approves; the submitter's CLI reads whether one was merged)."""

import json

import httpx2

from agentic.hosting import RepoRef
from agentic.hosting.base import ReviewHost
from agentic.hosting.github import GITHUB_COM_API, GitHubHost

TOKEN = "gho_" + "Fake" + "Review" + "0123456789"  # gitleaks:allow - built from pieces
REPO = RepoRef("github.com", "acme", "app")


def pr(number: int, head: str, **extra) -> dict:
    return {
        "number": number,
        "html_url": f"https://github.com/acme/app/pull/{number}",
        "state": "open",
        "title": f"title {number}",
        "body": f"body {number}",
        "user": {"login": "dev"},
        "head": {"ref": head, "sha": "a" * 40},
        "base": {"ref": "main"},
        **extra,
    }


def host(handler) -> GitHubHost:
    return GitHubHost(GITHUB_COM_API, TOKEN, transport=httpx2.MockTransport(handler))


def test_github_host_is_a_review_host():
    assert isinstance(host(lambda r: httpx2.Response(200, json={})), ReviewHost)


def test_list_pull_requests_returns_open_ones_under_a_branch_prefix():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        rows = [pr(1, "agentic/skill/retry-rules"), pr(2, "feature/x")]
        return httpx2.Response(200, json=rows)

    found = host(handler).list_pull_requests(REPO, head_prefix="agentic/skill/")
    assert [(p.number, p.title, p.body, p.author) for p in found] == [
        (1, "title 1", "body 1", "dev")
    ]
    assert seen[0].url.path == "/repos/acme/app/pulls"
    assert seen[0].url.params["state"] == "open"


def test_get_pull_request_reports_merged():
    def handler(request: httpx2.Request) -> httpx2.Response:
        number = int(request.url.path.rsplit("/", 1)[1])
        if number == 1:
            return httpx2.Response(200, json=pr(1, "a", state="closed", merged=True))
        return httpx2.Response(200, json=pr(2, "b", state="closed", merged=False))

    github = host(handler)
    merged, declined = github.get_pull_request(REPO, 1), github.get_pull_request(REPO, 2)
    assert (merged.state, merged.merged) == ("closed", True)
    assert (declined.state, declined.merged) == ("closed", False)


def test_pull_request_files_are_read_across_pages():
    def handler(request: httpx2.Request) -> httpx2.Response:
        page = int(request.url.params["page"])
        count = 100 if page == 1 else 3
        rows = [{"filename": f"skills/internal/x/f{page}-{i}.md"} for i in range(count)]
        return httpx2.Response(200, json=rows)

    files = host(handler).pull_request_files(REPO, 4)
    assert len(files) == 103 and files[0] == "skills/internal/x/f1-0.md"


def test_review_posts_approve_and_request_changes():
    posted = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        posted.append((request.method, request.url.path, json.loads(request.content)))
        return httpx2.Response(200, json={"id": 1, "state": "APPROVED"})

    github = host(handler)
    github.review_pull_request(REPO, 3, approve=True, body="looks right")
    github.review_pull_request(REPO, 3, approve=False, body="too specific to one repo")
    assert posted == [
        ("POST", "/repos/acme/app/pulls/3/reviews", {"event": "APPROVE", "body": "looks right"}),
        (
            "POST",
            "/repos/acme/app/pulls/3/reviews",
            {"event": "REQUEST_CHANGES", "body": "too specific to one repo"},
        ),
    ]
