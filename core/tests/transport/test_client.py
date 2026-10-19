"""Tests for the model-call client: identity headers and the behaviour-header guard (M1-11)."""

import asyncio
import re
from dataclasses import replace

import httpx
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


def capturing() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler), seen


def client(identity: CallIdentity = IDENTITY, **kwargs) -> tuple[httpx.Client, list]:
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


def test_each_call_gets_its_own_traceparent():
    c, seen = client()
    c.get("/models")
    c.get("/models")

    assert seen[0].headers["traceparent"] != seen[1].headers["traceparent"]


def test_the_api_key_is_sent_as_a_bearer_token():
    c, seen = client(api_key="tok-abc")
    c.get("/models")

    assert seen[0].headers["Authorization"] == "Bearer tok-abc"


@pytest.mark.parametrize("header", ["X-Step-Name", "X-Workflow-ID", "X-Session-ID",
                                    "X-Complexity", "X-No-Cache", "x-route-to"])  # fmt: skip
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


@pytest.mark.parametrize(("field", "value"), [
    ("user", "José"),  # non-ASCII
    ("repo", "github.com/a/b\n"),  # a trailing newline
    ("team", "a\r\nX-Evil: 1"),  # header injection
    ("step", ""),
    ("agent", "x" * 257),
    ("tier", "fast\tlane"),
])  # fmt: skip
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
