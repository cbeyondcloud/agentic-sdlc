"""The Jira REST adapter (M2-50) against `httpx2.MockTransport` serving the response shapes
Jira Cloud and Data Center document (fixtures/jira/). No live Jira is called (owner
decision 73): Cloud and Data Center paths, both auth schemes, comments in v2 text and v3
ADF, transitions, every error explained, and the token kept to the configured origin."""

import base64
import json
import logging
from pathlib import Path

import httpx2
import pytest

from agentic.integrations.jira import JiraClient, JiraError, JiraTransition, issue_key

FIXTURES = Path(__file__).parent / "fixtures" / "jira"
TOKEN = "ATATT3x" + "FakeJiraToken" + "0123456789"  # gitleaks:allow - built from pieces
CLOUD = "https://acme.atlassian.net"
DC = "https://jira.acme.example/jira"


def recorded(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def client(handler, url: str = CLOUD, **kwargs) -> JiraClient:
    if url == CLOUD and "user" not in kwargs:
        kwargs["user"] = "mia@acme.example"
    return JiraClient(url, TOKEN, transport=httpx2.MockTransport(handler), **kwargs)


def serving(answers: dict[tuple[str, str], httpx2.Response], seen: list | None = None):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return answers.get(
            (request.method, request.url.path),
            httpx2.Response(404, json=recorded("error-404.json")),
        )

    return handler


# ── keys ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text, key", [("PROJ-123", "PROJ-123"), (" proj-9 ", "PROJ-9")])
def test_issue_keys_are_normalised(text, key):
    assert issue_key(text) == key


@pytest.mark.parametrize(
    "text", ["", "PROJ", "PROJ-0", "PROJ-12a", "../PROJ-1", "1PROJ-1", "A B-1"]
)
def test_a_text_that_is_not_an_issue_key_is_refused_before_any_request(text):
    seen = []
    with pytest.raises(JiraError, match="not a Jira issue key"):
        client(serving({}, seen)).issue(text)
    assert seen == []


# ── reading an issue ─────────────────────────────────────────────────────────


def test_cloud_reads_an_issue_with_basic_auth_over_v2():
    seen = []
    handler = serving(
        {
            ("GET", "/rest/api/2/issue/PROJ-123"): httpx2.Response(
                200, json=recorded("cloud-v2-issue.json")
            )
        },
        seen,
    )
    issue = client(handler).issue("PROJ-123")

    assert issue["fields"]["summary"] == "Export the payments ledger as CSV"
    [request] = seen
    pair = base64.b64encode(f"mia@acme.example:{TOKEN}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {pair}"
    assert request.headers["accept"] == "application/json"


def test_data_center_uses_a_bearer_token_under_its_context_path():
    seen = []
    handler = serving(
        {
            ("GET", "/jira/rest/api/2/issue/OPS-7"): httpx2.Response(
                200, json=recorded("dc-v2-issue.json")
            )
        },
        seen,
    )
    issue = client(handler, DC).issue("ops-7")

    assert issue["key"] == "OPS-7"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert str(seen[0].url) == "https://jira.acme.example/jira/rest/api/2/issue/OPS-7"


def test_cloud_v3_returns_the_adf_description_as_sent():
    handler = serving(
        {
            ("GET", "/rest/api/3/issue/PROJ-123"): httpx2.Response(
                200, json=recorded("cloud-v3-issue.json")
            )
        }
    )
    issue = client(handler, api_version="3").issue("PROJ-123")

    assert issue["fields"]["description"]["type"] == "doc"


def test_the_browse_url_is_on_the_site():
    assert client(serving({})).browse_url("proj-123") == f"{CLOUD}/browse/PROJ-123"
    assert client(serving({}), DC).browse_url("OPS-7") == f"{DC}/browse/OPS-7"


# ── commenting ───────────────────────────────────────────────────────────────


def test_a_v2_comment_is_plain_text():
    seen = []
    path = ("POST", "/rest/api/2/issue/PROJ-123/comment")
    handler = serving({path: httpx2.Response(201, json=recorded("comment-created.json"))}, seen)

    assert client(handler).comment("PROJ-123", "line one\n\nline two") == "10310"
    assert json.loads(seen[0].content) == {"body": "line one\n\nline two"}


def test_a_v3_comment_is_an_adf_document():
    seen = []
    path = ("POST", "/rest/api/3/issue/PROJ-123/comment")
    handler = serving({path: httpx2.Response(201, json=recorded("comment-created.json"))}, seen)

    client(handler, api_version="3").comment("PROJ-123", "line one\n\nline two")
    body = json.loads(seen[0].content)["body"]
    assert body["type"] == "doc" and body["version"] == 1
    texts = [p["content"][0]["text"] for p in body["content"]]
    assert texts == ["line one", "line two"]  # ADF has no empty text nodes


# ── transitions ──────────────────────────────────────────────────────────────


def transitions_handler(seen: list, apply_status: int = 204):
    path = "/rest/api/2/issue/PROJ-123/transitions"
    return serving(
        {
            ("GET", path): httpx2.Response(200, json=recorded("transitions.json")),
            ("POST", path): httpx2.Response(
                apply_status, json=recorded("error-400-transition.json")
            )
            if apply_status >= 300
            else httpx2.Response(apply_status),
        },
        seen,
    )


def test_the_available_transitions_are_listed():
    found = client(transitions_handler([])).transitions("PROJ-123")

    assert found[1] == JiraTransition(id="21", name="Start work", to="In Progress")


@pytest.mark.parametrize("name", ["Start work", "in progress", "IN PROGRESS"])
def test_a_transition_is_found_by_its_name_or_target_status(name):
    seen = []
    done = client(transitions_handler(seen)).transition("PROJ-123", name)

    assert done.id == "21"
    assert json.loads(seen[-1].content) == {"transition": {"id": "21"}}


def test_an_unknown_transition_names_the_available_ones():
    seen = []
    with pytest.raises(JiraError, match="To Do, Start work .In Progress., Done") as raised:
        client(transitions_handler(seen)).transition("PROJ-123", "Shipped")
    assert raised.value.status is None
    assert [r.method for r in seen] == ["GET"]  # nothing applied


def test_a_transition_needing_a_screen_field_is_explained():
    with pytest.raises(JiraError, match="HTTP 400.*resolution: Field 'resolution' is required"):
        client(transitions_handler([], apply_status=400)).transition("PROJ-123", "Done")


# ── errors ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status, words, transient",
    [
        (401, "token was rejected", False),
        (403, "may not", False),
        (404, "does not exist or the token's user cannot see it", False),
        (429, "rate limited", True),
        (503, "refused the request", True),
    ],
)
def test_errors_are_explained(status, words, transient):
    def handler(request):
        return httpx2.Response(
            status, json=recorded("error-404.json"), headers={"Retry-After": "7"}
        )

    with pytest.raises(JiraError, match=words) as raised:
        client(handler).issue("PROJ-123")
    assert raised.value.status == status
    assert raised.value.transient is transient
    assert "GET /rest/api/2/issue/PROJ-123" in str(raised.value)
    if transient:
        assert raised.value.retry_after == 7.0


def test_an_html_login_page_is_not_mistaken_for_an_issue():
    def handler(request):
        return httpx2.Response(200, text="<html><body>Log in</body></html>")

    with pytest.raises(JiraError, match="not JSON"):
        client(handler).issue("PROJ-123")


def test_an_unreachable_site_is_a_transient_error():
    def handler(request):
        raise httpx2.ConnectError("connection refused")

    with pytest.raises(JiraError, match="could not reach acme.atlassian.net") as raised:
        client(handler).issue("PROJ-123")
    assert raised.value.transient


def test_redirects_are_not_followed():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(302, headers={"Location": "https://evil.example/steal"})

    with pytest.raises(JiraError, match="redirect"):
        client(handler).issue("PROJ-123")
    assert [r.url.host for r in seen] == ["acme.atlassian.net"]


@pytest.mark.parametrize("url", ["http://jira.acme.example", "ftp://x/y", "not a url", ""])
def test_plain_http_is_refused_except_loopback(url):
    with pytest.raises(JiraError, match="https"):
        JiraClient(url, TOKEN)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://localhost:9", "http://[::1]:1"])
def test_loopback_http_is_allowed_for_local_fakes(url):
    JiraClient(url, TOKEN).close()


def test_a_cloud_site_needs_the_account_email():
    with pytest.raises(JiraError, match="email"):
        JiraClient(CLOUD, TOKEN)


def test_an_unknown_api_version_is_refused():
    with pytest.raises(JiraError, match="api_version"):
        JiraClient(DC, TOKEN, api_version="4")


def test_the_token_is_scrubbed_from_errors_and_never_logged(caplog):
    def echo(request):  # a server that echoes the request back, header included
        said = f"bad request from {request.headers['authorization']}"
        return httpx2.Response(400, json={"errorMessages": [said], "errors": {}})

    caplog.set_level(logging.DEBUG)
    jira = client(echo, DC)
    with pytest.raises(JiraError) as raised:
        jira.issue("OPS-7")
    assert TOKEN not in str(raised.value) and "<redacted>" in str(raised.value)
    assert TOKEN not in repr(jira) and TOKEN not in caplog.text


def test_basic_credentials_are_scrubbed_in_their_encoded_form_too():
    pair = base64.b64encode(f"mia@acme.example:{TOKEN}".encode()).decode()

    def echo(request):
        return httpx2.Response(400, text=f"bad header {request.headers['authorization']}")

    with pytest.raises(JiraError) as raised:
        client(echo).issue("PROJ-123")
    assert pair not in str(raised.value) and TOKEN not in str(raised.value)
