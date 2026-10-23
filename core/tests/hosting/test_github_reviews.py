"""Reading and reviewing pull requests (M2-46: skill submissions are pull requests that the
scope owner lists and approves; the submitter's CLI reads whether one was merged)."""

import base64
import json

import httpx2
import pytest

from agentic.hosting import HostingError, RepoRef
from agentic.hosting.base import ListingIncomplete, ReviewHost
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


def files_host(pages: list[list[dict]], changed: int | None):
    """A host whose pull request 4 lists `pages` of files and reports `changed` files."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/pulls/4"):
            extra = {} if changed is None else {"changed_files": changed}
            return httpx2.Response(200, json=pr(4, "agentic/skill/x", **extra))
        page = int(request.url.params["page"])
        return httpx2.Response(200, json=pages[page - 1] if page <= len(pages) else [])

    return host(handler)


def test_pull_request_files_are_read_across_pages():
    pages = [
        [{"filename": f"skills/internal/x/f{page}-{i}.md"} for i in range(count)]
        for page, count in ((1, 100), (2, 3))
    ]
    files = files_host(pages, 103).pull_request_files(REPO, 4)
    assert len(files) == 103 and files[0] == "skills/internal/x/f1-0.md"


def test_a_renamed_file_lists_its_old_path_too():
    """Review F1: a pull request moving pipelines/ci.yaml into a skill folder changes
    pipelines/ too; GitHub names the old path only in `previous_filename`."""
    rows = [
        {"filename": "teams/payments/skills/internal/x/SKILL.md", "status": "added"},
        {
            "filename": "teams/payments/skills/internal/x/ci.yaml",
            "status": "renamed",
            "previous_filename": "pipelines/ci.yaml",
        },
    ]
    files = files_host([rows], 2).pull_request_files(REPO, 4)
    assert "pipelines/ci.yaml" in files and len(files) == 3


def test_a_listing_the_host_cut_short_is_an_error():
    """Review F1: GitHub lists at most 3000 files; the rest would never be checked."""
    rows = [{"filename": f"skills/internal/x/f{i}.md"} for i in range(5)]
    with pytest.raises(ListingIncomplete, match="7"):
        files_host([rows], 7).pull_request_files(REPO, 4)
    full = [[{"filename": f"f{p}-{i}.md"} for i in range(100)] for p in range(30)]
    with pytest.raises(ListingIncomplete):
        files_host(full, None).pull_request_files(REPO, 4)


def test_file_at_reads_a_file_at_a_commit():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path.endswith("/missing.md"):
            return httpx2.Response(404, json={"message": "Not Found"})
        if request.url.path.endswith("/folder"):
            return httpx2.Response(200, json=[{"type": "file", "name": "a.md"}])
        content = base64.b64encode(b"# Retries\r\n").decode("ascii")
        return httpx2.Response(200, json={"type": "file", "encoding": "base64", "content": content})

    github = host(handler)
    assert github.file_at(REPO, "skills/internal/x/SKILL.md", "c" * 40) == b"# Retries\r\n"
    assert seen[0].url.path == "/repos/acme/app/contents/skills/internal/x/SKILL.md"
    assert seen[0].url.params["ref"] == "c" * 40
    assert github.file_at(REPO, "skills/missing.md", "c" * 40) is None
    assert github.file_at(REPO, "skills/folder", "c" * 40) is None
    with pytest.raises(HostingError, match="path"):
        github.file_at(REPO, "skills/../policy/org.yaml", "c" * 40)


def test_common_ancestor_comes_from_the_compare_endpoint():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        return httpx2.Response(200, json={"merge_base_commit": {"sha": "e" * 40}})

    assert host(handler).common_ancestor(REPO, "main", "f" * 40) == "e" * 40
    assert seen == [f"/repos/acme/app/compare/main...{'f' * 40}"]


def test_a_review_can_be_pinned_to_the_commit_it_checked():
    posted = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        posted.append(json.loads(request.content))
        return httpx2.Response(200, json={"id": 1})

    host(handler).review_pull_request(REPO, 3, approve=True, body="ok", commit="d" * 40)
    assert posted == [{"event": "APPROVE", "body": "ok", "commit_id": "d" * 40}]


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
