"""A failed check's log through the GitHub adapter (DF-290): the Actions job id on a check
run, the log endpoint's redirect followed once and without the token, the end of a long
log kept, an expired log or a non-Actions check answered with None. Recorded API shapes
served by `httpx2.MockTransport`; nothing reaches GitHub."""

import httpx2
import pytest

from agentic.hosting import CheckRun, HostingError, RepoRef
from agentic.hosting.base import CheckLogHost
from agentic.hosting.github import GITHUB_COM_API, LOG_LIMIT, GitHubHost

TOKEN = "gho_" + "Fake" + "Token" + "0123456789"  # gitleaks:allow - built from pieces
REPO = RepoRef("github.com", "acme", "app")
BLOB = "https://pipelines.actions.githubusercontent.com/serviceHosts/x/logs/42"
FAILED = CheckRun("test", "completed", "failure", "https://github.com/acme/app/runs/42", "42")


def host(handler, api_url: str = GITHUB_COM_API) -> GitHubHost:
    return GitHubHost(api_url, TOKEN, transport=httpx2.MockTransport(handler))


def test_actions_check_runs_carry_their_job_id():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/check-runs"):
            runs = [
                {
                    "id": 42,
                    "name": "test",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/acme/app/actions/runs/7/job/42",
                    "app": {"slug": "github-actions"},
                },
                {"id": 9, "name": "sonar", "status": "completed", "app": {"slug": "sonar"}},
            ]
            return httpx2.Response(200, json={"total_count": 2, "check_runs": runs})
        return httpx2.Response(200, json={"state": "success", "statuses": []})

    runs = host(handler).checks(REPO, "b" * 40)
    assert [run.log_ref for run in runs] == ["42", ""]
    assert isinstance(host(handler), CheckLogHost)


def test_check_log_follows_the_redirect_once_without_the_token():
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.host == "api.github.com":
            return httpx2.Response(302, headers={"location": BLOB + "?sig=s3cr3t&se=soon"})
        return httpx2.Response(200, content=b"step 1 ok\nFAILED tests/test_x.py::test_y\n")

    text = host(handler).check_log(REPO, FAILED)
    assert text == "step 1 ok\nFAILED tests/test_x.py::test_y\n"
    assert [str(r.url).split("?")[0] for r in seen] == [
        "https://api.github.com/repos/acme/app/actions/jobs/42/logs",
        BLOB,
    ]
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert "authorization" not in seen[1].headers


def test_check_log_keeps_the_end_of_a_long_log():
    body = ("x" * 100 + "\n") * 1000 + "the error at the end\n"

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "api.github.com":
            return httpx2.Response(302, headers={"location": BLOB})
        return httpx2.Response(200, content=body.encode())

    text = host(handler).check_log(REPO, FAILED)
    assert text is not None
    assert text.endswith("the error at the end\n")
    assert text.startswith("[... the start of the log was cut ...]\n")
    assert len(text) <= LOG_LIMIT + 50


@pytest.mark.parametrize("status", [404, 410])
def test_an_expired_log_is_none(status):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"message": "Not Found"})

    assert host(handler).check_log(REPO, FAILED) is None


def test_a_check_that_is_not_an_actions_job_has_no_log():
    def handler(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover - not called
        raise AssertionError(f"unexpected request {request.url}")

    assert host(handler).check_log(REPO, CheckRun("jenkins", "completed", "failure")) is None


def test_a_plain_http_download_is_refused_and_the_signature_is_not_shown():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"location": "http://evil.example/l?sig=s3cr3t"})

    with pytest.raises(HostingError) as raised:
        host(handler).check_log(REPO, FAILED)
    assert "evil.example" in str(raised.value)
    assert "s3cr3t" not in str(raised.value)


def test_a_failed_download_names_the_host_but_not_the_signature():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "api.github.com":
            return httpx2.Response(302, headers={"location": BLOB + "?sig=s3cr3t"})
        return httpx2.Response(403, content=b"AuthenticationFailed sig=s3cr3t")

    with pytest.raises(HostingError) as raised:
        host(handler).check_log(REPO, FAILED)
    assert "pipelines.actions.githubusercontent.com" in str(raised.value)
    assert "s3cr3t" not in str(raised.value)
