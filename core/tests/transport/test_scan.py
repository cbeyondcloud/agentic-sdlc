"""Tests for the secret scan on every model call through the transport clients (M1-41)."""

import asyncio
import json

import httpx2
import pytest

from agentic.security import SecretBlocked, SecretScanEvent, SecretScanner
from agentic.transport import CallIdentity, make_async_client, make_client

IDENTITY = CallIdentity(
    team="payments",
    user="u123",
    repo="github.com/acme/api",
    pipeline="feature-delivery",
    run_id="r-scan",
    step="build",
    agent="developer@1.2.0",
    policy_version="git:" + "a" * 40,
)
AWS = "AKIA" + "Z7Q4R2XW" + "9PLM3TKB"  # assembled, so this file never holds one whole
BASE = "https://tl.example/v1"


def capturing() -> tuple[httpx2.MockTransport, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        request.read()
        seen.append(request)
        return httpx2.Response(200, json={"ok": True})

    return httpx2.MockTransport(handler), seen


def body(text: str) -> dict:
    return {"model": "m", "messages": [{"role": "user", "content": text}]}


def test_a_client_built_without_a_scanner_still_blocks():
    transport, seen = capturing()
    client = make_client(BASE, IDENTITY, _transport=transport)
    with pytest.raises(SecretBlocked, match="AWS Access Key"):
        client.post("/chat/completions", json=body(f"deploy with {AWS}"))
    assert seen == []


def test_the_async_client_blocks_before_sending():
    transport, seen = capturing()
    events: list[SecretScanEvent] = []
    scanner = SecretScanner(on_event=events.append)

    async def go():
        async with make_async_client(BASE, IDENTITY, scanner=scanner, _transport=transport) as c:
            await c.post("/chat/completions", json=body(f"deploy with {AWS}"))

    with pytest.raises(SecretBlocked):
        asyncio.run(go())
    assert seen == []
    assert events == [SecretScanEvent("blocked", "model-call", {"AWSKeyDetector": 1})]


def test_mask_sends_the_masked_body_with_a_matching_length():
    transport, seen = capturing()
    client = make_client(BASE, IDENTITY, scanner=SecretScanner("mask"), _transport=transport)
    client.post("/chat/completions", json=body(f"deploy with {AWS}"))

    (sent,) = seen
    assert AWS.encode() not in sent.content
    assert json.loads(sent.content)["messages"][0]["content"] == (
        "deploy with [MASKED SECRET: AWS Access Key]"
    )
    assert int(sent.headers["content-length"]) == len(sent.content)
    assert sent.headers["x-agentic-run-id"] == "r-scan"  # the identity hook still ran


def test_a_clean_request_is_sent_byte_for_byte():
    transport, seen = capturing()
    raw = b'{"model":  "m",\n  "messages": []}'
    client = make_client(BASE, IDENTITY, _transport=transport)
    client.post("/chat/completions", content=raw, headers={"content-type": "application/json"})
    assert seen[0].content == raw


def test_a_streamed_request_body_is_scanned_too():
    transport, seen = capturing()

    async def chunks():
        yield b'{"content": "'
        yield AWS.encode()
        yield b'"}'

    async def go():
        async with make_async_client(BASE, IDENTITY, _transport=transport) as c:
            await c.post("/chat/completions", content=chunks())

    with pytest.raises(SecretBlocked):
        asyncio.run(go())
    assert seen == []
