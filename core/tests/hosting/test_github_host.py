"""The GitHub REST adapter (M1-58), against `httpx2.MockTransport`: Enterprise URLs, the
token kept to the API origin and out of every message, pagination, checks and statuses."""

import json
import logging

import httpx2
import pytest

from agentic.hosting import CheckRun, HostingError, PullRequest, RepoRef
from agentic.hosting.github import GITHUB_COM_API, GitHubHost, api_url_for

TOKEN = "gho_" + "Fake" + "Token" + "0123456789"  # gitleaks:allow - built from pieces
REPO = RepoRef("github.com", "acme", "app")


def pr_json(number=7, head="sdlc/r-1", base="main", sha="a" * 40) -> dict:
    return {
        "number": number,
        "html_url": f"https://github.com/acme/app/pull/{number}",
        "state": "open",
        "head": {"ref": head, "sha": sha},
        "base": {"ref": base},
    }


def host(handler, api_url: str = GITHUB_COM_API) -> GitHubHost:
    return GitHubHost(api_url, TOKEN, transport=httpx2.MockTransport(handler))


def test_api_url_for_github_com_and_enterprise():
    assert api_url_for("github.com") == "https://api.github.com"
    assert api_url_for("ghe.example.com") == "https://ghe.example.com/api/v3"
    assert api_url_for("ghe.example.com", "https://api.ghe.example.com/") == (
        "https://api.ghe.example.com"
    )


def test_requests_carry_the_token_and_api_headers():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"default_branch": "trunk"})

    assert host(handler).default_branch(REPO) == "trunk"
    request = seen[0]
    assert str(request.url) == "https://api.github.com/repos/acme/app"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"


def test_enterprise_paths_keep_the_api_prefix():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(200, json={"default_branch": "main"})

    ghe = RepoRef("ghe.example.com", "team", "svc")
    host(handler, "https://ghe.example.com/api/v3").default_branch(ghe)
    assert seen == ["https://ghe.example.com/api/v3/repos/team/svc"]


@pytest.mark.parametrize("url", ["http://ghe.example.com/api/v3", "ftp://x/y", "not a url"])
def test_plain_http_is_refused_except_loopback(url):
    with pytest.raises(HostingError, match="https"):
        GitHubHost(url, TOKEN)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://localhost:9", "http://[::1]:1"])
def test_loopback_http_is_allowed_for_local_fakes(url):
    GitHubHost(url, TOKEN).close()


def test_find_opens_and_updates_pull_requests():
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, dict(request.url.params), body))
        if request.method == "GET":
            return httpx2.Response(200, json=[])
        if request.method == "POST":
            return httpx2.Response(201, json=pr_json())
        return httpx2.Response(200, json=pr_json())

    github = host(handler)
    assert github.find_pull_request(REPO, "sdlc/r-1") is None
    opened = github.open_pull_request(REPO, head="sdlc/r-1", base="main", title="T", body="B")
    assert opened == PullRequest(
        7, "https://github.com/acme/app/pull/7", "sdlc/r-1", "main", "a" * 40
    )
    github.update_pull_request(REPO, 7, body="B2")
    find, create, update = calls
    assert find[:3] == (
        "GET",
        "/repos/acme/app/pulls",
        {"state": "open", "head": "acme:sdlc/r-1", "per_page": "100"},
    )
    assert create[1:] == (
        "/repos/acme/app/pulls",
        {},
        {"title": "T", "head": "sdlc/r-1", "base": "main", "body": "B"},
    )
    assert update[0:2] == ("PATCH", "/repos/acme/app/pulls/7") and update[3] == {"body": "B2"}


def test_a_create_race_returns_the_pull_request_that_already_exists():
    found = iter([[], [pr_json(number=9)]])

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            message = {
                "message": "Validation Failed",
                "errors": [{"message": "A pull request already exists for acme:sdlc/r-1."}],
            }
            return httpx2.Response(422, json=message)
        return httpx2.Response(200, json=next(found))

    github = host(handler)
    assert github.find_pull_request(REPO, "sdlc/r-1") is None
    opened = github.open_pull_request(REPO, head="sdlc/r-1", base="main", title="T", body="B")
    assert opened.number == 9


def test_checks_merge_check_runs_and_commit_statuses():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/check-runs"):
            return httpx2.Response(
                200,
                json={
                    "total_count": 2,
                    "check_runs": [
                        {
                            "name": "build",
                            "status": "completed",
                            "conclusion": "success",
                            "html_url": "https://github.com/acme/app/runs/1",
                        },
                        {
                            "name": "lint",
                            "status": "in_progress",
                            "conclusion": None,
                            "html_url": "",
                        },
                    ],
                },
            )
        return httpx2.Response(
            200,
            json={
                "state": "failure",
                "statuses": [
                    {"context": "jenkins", "state": "error", "target_url": "https://ci/1"},
                    {"context": "sonar", "state": "pending", "target_url": None},
                    {"context": "legacy", "state": "success", "target_url": ""},
                ],
            },
        )

    runs = host(handler).checks(REPO, "b" * 40)
    assert runs == [
        CheckRun("build", "completed", "success", "https://github.com/acme/app/runs/1"),
        CheckRun("lint", "in_progress", None, ""),
        CheckRun("jenkins", "completed", "failure", "https://ci/1"),
        CheckRun("sonar", "pending", None, ""),
        CheckRun("legacy", "completed", "success", ""),
    ]


def test_check_runs_are_read_across_pages():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/status"):
            return httpx2.Response(200, json={"statuses": []})
        page = request.url.params.get("page", "1")
        if page == "1":
            nxt = f"{request.url.copy_with(params={'per_page': '100', 'page': '2'})}"
            return httpx2.Response(
                200,
                json={
                    "check_runs": [{"name": "a", "status": "completed", "conclusion": "success"}]
                },
                headers={"link": f'<{nxt}>; rel="next"'},
            )
        return httpx2.Response(
            200, json={"check_runs": [{"name": "b", "status": "queued", "conclusion": None}]}
        )

    assert [c.name for c in host(handler).checks(REPO, "c" * 40)] == ["a", "b"]


def test_pagination_never_leaves_the_api_origin():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.host)
        return httpx2.Response(
            200,
            json={"check_runs": []},
            headers={"link": '<https://evil.example.com/steal?page=2>; rel="next"'},
        )

    with pytest.raises(HostingError, match="another origin"):
        host(handler).checks(REPO, "d" * 40)
    assert "evil.example.com" not in seen


def test_errors_and_repr_never_hold_the_token(caplog):
    def handler(request: httpx2.Request) -> httpx2.Response:
        echoed = request.headers["authorization"]
        return httpx2.Response(500, json={"message": f"server saw {echoed}"})

    github = host(handler)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(HostingError) as raised:
        github.default_branch(REPO)
    assert TOKEN not in str(raised.value) and "500" in str(raised.value)
    assert TOKEN not in repr(github)
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(
    "status, words",
    [(401, "rejected"), (403, "permission"), (404, "not found"), (429, "rate")],
)
def test_http_errors_say_what_is_wrong(status, words):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"message": "nope"})

    with pytest.raises(HostingError, match=words):
        host(handler).default_branch(REPO)


def test_a_network_failure_is_a_hosting_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(HostingError, match="could not reach"):
        host(handler).default_branch(REPO)


@pytest.mark.parametrize(
    "status, headers, transient, retry_after",
    [
        (502, {}, True, None),
        (503, {"retry-after": "12"}, True, 12.0),
        (429, {"retry-after": "7"}, True, 7.0),
        (403, {"x-ratelimit-remaining": "0"}, True, None),
        (403, {"retry-after": "30"}, True, 30.0),  # a secondary rate limit
        (401, {}, False, None),
        (403, {}, False, None),
        (404, {}, False, None),
        (422, {}, False, None),
    ],
)
def test_an_error_says_whether_trying_again_may_help(status, headers, transient, retry_after):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, headers=headers, json={"message": "nope"})

    with pytest.raises(HostingError) as raised:
        host(handler).default_branch(REPO)
    assert (raised.value.transient, raised.value.retry_after) == (transient, retry_after)


def test_a_network_failure_may_pass_on_its_own():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow proxy", request=request)

    with pytest.raises(HostingError) as raised:
        host(handler).default_branch(REPO)
    assert raised.value.transient is True


def test_a_refusal_is_not_transient_by_default():
    assert (HostingError("refused").transient, HostingError("refused").retry_after) == (
        False,
        None,
    )


def test_redirects_are_not_followed():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(301, headers={"location": "https://elsewhere.example.com/x"})

    with pytest.raises(HostingError, match="301"):
        host(handler).default_branch(REPO)


def test_a_nested_owner_is_not_a_github_repository():
    with pytest.raises(HostingError, match="owner/name"):
        host(lambda r: httpx2.Response(200, json={})).default_branch(
            RepoRef("github.com", "a/b", "c")
        )


def test_no_merge_operation_exists():
    assert not [name for name in dir(GitHubHost) if "merge" in name.lower()]
