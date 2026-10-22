"""`max_body_bytes` is enforced app-wide (DF-420): a declared or streamed body over the limit
gets 413 before any route parses it."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.anyio


async def test_declared_body_over_the_limit_is_413(api):
    small = api.with_settings(max_body_bytes=2048)
    body = {"name": "x" * 4096}
    reply = await small.post("/v1/tokens", api.a.admin_token, body)
    assert reply.status_code == 413
    assert reply.json()["code"] == "too_large"
    assert reply.headers["content-type"].startswith("application/problem+json")
    await small.http.aclose()


async def test_streamed_body_over_the_limit_is_413(api):
    small = api.with_settings(max_body_bytes=2048)
    payload = json.dumps({"name": "x" * 4096}).encode()

    async def chunks():
        for start in range(0, len(payload), 512):
            yield payload[start : start + 512]

    reply = await small.http.post(
        "/v1/tokens",
        content=chunks(),
        headers={
            "Authorization": f"Bearer {api.a.admin_token}",
            "Content-Type": "application/json",
        },
    )
    assert reply.status_code == 413
    await small.http.aclose()


async def test_body_under_the_limit_is_served(api):
    small = api.with_settings(max_body_bytes=2048)
    reply = await small.post("/v1/tokens", api.a.admin_token, {"name": "fine"})
    assert reply.status_code == 201
    await small.http.aclose()
