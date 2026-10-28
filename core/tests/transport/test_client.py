"""Tests for the model-call client: identity headers and the behaviour-header guard (M1-11)."""

import asyncio
import re
from dataclasses import replace

import httpx2
import pytest

from agentic.transport import (
    BEHAVIOUR_HEADERS,
    CallIdentity,
    ForbiddenHeaderError,
    IdentityError,
    IdentityHeaderError,
    make_async_client,
    make_client,
)

IDENTITY = CallIdentity(
    team="payments",
    user="u123",
    repo="github.com/acme/api",
    pipeline="feature-delivery",
    run_id="r-20261019-abc",
    step="build",
    agent="developer@1.2.0",
    policy_version="git:" + "a" * 40,
    tier="balanced",
    parent_agent="lead@1.0.0",
)
EXPECTED = {
    "X-Agentic-Team": "payments",
    "X-Agentic-User": "u123",
    "X-Agentic-Repo": "github.com/acme/api",
    "X-Agentic-Pipeline": "feature-delivery",
    "X-Agentic-Run-Id": "r-20261019-abc",
    "X-Agentic-Step": "build",
    "X-Agentic-Agent": "developer@1.2.0",
    "X-Agentic-Parent-Agent": "lead@1.0.0",
    "X-Agentic-Tier": "balanced",
    "X-Agentic-Policy-Version": "git:" + "a" * 40,
}
TRACEPARENT = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01")


def capturing() -> tuple[httpx2.MockTransport, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"ok": True})

    return httpx2.MockTransport(handler), seen


def client(identity: CallIdentity = IDENTITY, **kwargs) -> tuple[httpx2.Client, list]:
    transport, seen = capturing()
    return make_client("https://tl.example/v1", identity, _transport=transport, **kwargs), seen


def test_all_ten_identity_headers_and_traceparent_are_sent():
    c, seen = client()
    c.post("/chat/completions", json={})

    sent = seen[0].headers
    assert {name: sent[name] for name in EXPECTED} == EXPECTED
    assert TRACEPARENT.fullmatch(sent["traceparent"])
    assert str(seen[0].url) == "https://tl.example/v1/chat/completions"


def test_optional_headers_are_omitted_when_unset():
    c, seen = client(replace(IDENTITY, tier=None, parent_agent=None))
    c.get("/models")

    assert "X-Agentic-Tier" not in seen[0].headers
    assert "X-Agentic-Parent-Agent" not in seen[0].headers
    assert seen[0].headers["X-Agentic-Team"] == "payments"


def test_the_classification_header_is_sent_only_when_set():
    """The repo's classification rides with the tier, so the gateway's routing rules can
    keep a restricted repo on its allowed models (M2-09)."""
    c, seen = client(replace(IDENTITY, classification="restricted"))
    c.get("/models")
    assert seen[0].headers["X-Agentic-Classification"] == "restricted"

    c, seen = client()
    c.get("/models")
    assert "X-Agentic-Classification" not in seen[0].headers


def test_each_call_gets_its_own_traceparent():
    c, seen = client()
    c.get("/models")
    c.get("/models")

    assert seen[0].headers["traceparent"] != seen[1].headers["traceparent"]


def test_the_api_key_is_sent_as_a_bearer_token():
    c, seen = client(api_key="tok-abc")
    c.get("/models")

    assert seen[0].headers["Authorization"] == "Bearer tok-abc"


@pytest.mark.parametrize(
    "header",
    ["X-Step-Name", "X-Workflow-ID", "X-Session-ID", "X-Complexity", "X-No-Cache", "x-route-to"],
)
def test_tokenlean_behaviour_headers_are_refused(header):
    c, seen = client()

    with pytest.raises(ForbiddenHeaderError, match=header.lower()):
        c.post("/chat/completions", json={}, headers={header: "x"})
    assert seen == []


def test_the_spec_names_the_three_most_dangerous_behaviour_headers():
    assert {"x-step-name", "x-workflow-id", "x-session-id"} <= BEHAVIOUR_HEADERS


def test_a_behaviour_header_can_be_allowed_on_purpose():
    c, seen = client(allow=frozenset({"x-no-cache"}))
    c.post("/chat/completions", json={}, headers={"X-No-Cache": "true"})

    assert seen[0].headers["X-No-Cache"] == "true"


def test_agent_code_cannot_relabel_a_call():
    c, seen = client()

    with pytest.raises(IdentityHeaderError, match="x-agentic-team"):
        c.post("/chat/completions", json={}, headers={"X-Agentic-Team": "other"})
    assert seen == []


def test_repeating_the_identity_value_is_harmless():
    c, seen = client()
    c.post("/chat/completions", json={}, headers={"X-Agentic-Team": "payments"})

    assert seen[0].headers["X-Agentic-Team"] == "payments"


def test_a_callers_traceparent_is_kept():
    c, seen = client()
    given = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"
    c.get("/models", headers={"traceparent": given})

    assert seen[0].headers["traceparent"] == given


def test_an_active_span_traceparent_is_propagated(monkeypatch):
    from agentic.transport import client as client_module

    active = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    monkeypatch.setattr(client_module, "inject", lambda carrier: carrier.update(traceparent=active))
    c, seen = client()
    c.get("/models")

    assert seen[0].headers["traceparent"] == active


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user", "José"),  # non-ASCII
        ("repo", "github.com/a/b\n"),  # a trailing newline
        ("team", "a\r\nX-Evil: 1"),  # header injection
        ("step", ""),
        ("agent", "x" * 257),
        ("tier", "fast\tlane"),
    ],
)
def test_unsafe_identity_values_are_refused_by_field_name(field, value):
    with pytest.raises(IdentityError) as error:
        replace(IDENTITY, **{field: value})

    assert field in str(error.value)
    assert value.strip() == "" or value.strip() not in str(error.value)


def test_the_async_client_stamps_the_same_headers():
    transport, seen = capturing()

    async def call():
        async with make_async_client("https://tl.example/v1", IDENTITY, _transport=transport) as c:
            await c.post("/chat/completions", json={})
            with pytest.raises(ForbiddenHeaderError):
                await c.post("/chat/completions", json={}, headers={"X-Session-ID": "s"})

    asyncio.run(call())

    assert {name: seen[0].headers[name] for name in EXPECTED} == EXPECTED
    assert TRACEPARENT.fullmatch(seen[0].headers["traceparent"])
    assert len(seen) == 1


# ── SP3 review fixes ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/steal",
        "http://tl.example/v1/models",
        "https://tl.example:8443/v1/models",
    ],
)
def test_a_request_to_any_other_host_is_refused_before_it_is_sent(url):  # review I7
    from agentic.transport import ForeignHostError

    c, seen = client(api_key="SECRET")
    with pytest.raises(ForeignHostError):
        c.get(url)
    assert seen == []


def test_redirects_are_not_followed():  # review I7
    c, _ = client()

    assert c.follow_redirects is False


@pytest.mark.parametrize("header", ["X_No_Cache", "x_step_name", "X-TokenLean-Anything"])
def test_underscore_spellings_and_tokenlean_namespace_headers_are_refused(header):  # review M5
    c, seen = client()

    with pytest.raises(ForbiddenHeaderError):
        c.get("/models", headers={header: "1"})
    assert seen == []


def test_all_calls_of_a_run_share_one_trace(monkeypatch):  # review M8
    c, seen = client()
    c.get("/models")
    c.get("/models")
    other, other_seen = client(replace(IDENTITY, run_id="r-other"))
    other.get("/models")

    trace = [s.headers["traceparent"].split("-")[1] for s in (*seen, *other_seen)]
    assert trace[0] == trace[1] != trace[2]
    assert seen[0].headers["traceparent"] != seen[1].headers["traceparent"]  # fresh span ids


def test_a_malformed_caller_traceparent_is_replaced():  # review M8
    c, seen = client()
    c.get("/models", headers={"traceparent": "garbage\x7f"})

    assert TRACEPARENT.fullmatch(seen[0].headers["traceparent"])


@pytest.mark.parametrize(
    "repo", ["https://token@github.com/acme/api", "https://user:pass@gitlab.example/acme/api"]
)
def test_a_repo_url_carrying_credentials_is_refused(repo):  # review M9
    with pytest.raises(IdentityError, match="repo"):
        replace(IDENTITY, repo=repo)


def test_an_scp_style_git_remote_is_fine():
    assert replace(IDENTITY, repo="git@github.com:acme/api.git").repo.startswith("git@")


def test_clients_are_httpx2():  # D36: the Anthropic SDK requires httpx2; Pydantic AI drops httpx
    assert isinstance(make_client("https://tl.example/v1", IDENTITY), httpx2.Client)
    assert isinstance(make_async_client("https://tl.example/v1", IDENTITY), httpx2.AsyncClient)
