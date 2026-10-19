"""Shared fixtures for the agent runtime tests: a real resolved agent, a TokenLean stand-in."""

import json
from pathlib import Path

import httpx2
import pytest

from agentic.definitions import load_agent
from agentic.policy import ResolveContext, ResolvedAgent, load_policy, resolve_agent
from agentic.transport import CallIdentity

IDENTITY = CallIdentity(
    team="payments",
    user="u123",
    repo="github.com/acme/api",
    pipeline="feature-delivery",
    run_id="r-sp4",
    step="build",
    agent="developer@1.2.0",
    policy_version="git:" + "a" * 40,
)


@pytest.fixture
def identity() -> CallIdentity:
    return IDENTITY


@pytest.fixture
def resolved(fixtures: Path) -> ResolvedAgent:
    """The fixture Developer agent, resolved for the payments team against the fixture policy."""
    policy = load_policy(fixtures / "policy" / "policy")
    agent, violations = resolve_agent(
        load_agent(fixtures / "agents" / "developer.md"), policy, ResolveContext(team="payments")
    )
    assert agent is not None, violations
    return agent


def openai_reply(model: str, text: str = "hello from tokenlean") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
        ],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500},
        "_token_opt": {
            "request_id": "req-1",
            "routed_model": model,
            "tokens_provider_billed": 1200,
            "response_tokens": 300,
            "cost_actual_usd": 0.004321,
            "cache_hit": False,
        },
    }


def anthropic_stream(model: str, text: str = "hello from tokenlean") -> bytes:
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 1},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"input_tokens": 812, "output_tokens": 95},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(f"event: {n}\ndata: {json.dumps(d)}\n\n".encode() for n, d in events)


def anthropic_reply(model: str, text: str = "hello from tokenlean") -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 200},
    }


class TokenLean:
    """A stand-in for TokenLean: answers in its real shapes and records each request."""

    def __init__(self) -> None:
        self.seen: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        body = json.loads(request.content or b"{}")
        model = body.get("model", "")
        if request.url.path.endswith("/messages"):
            if body.get("stream"):
                return httpx2.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=anthropic_stream(model),
                )
            return httpx2.Response(200, json=anthropic_reply(model))
        return httpx2.Response(
            200, headers={"x-tokenlean-request-id": "req-1"}, json=openai_reply(model)
        )


@pytest.fixture
def tokenlean() -> TokenLean:
    return TokenLean()
