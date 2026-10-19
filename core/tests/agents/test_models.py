"""Tests for building a Pydantic AI model for a resolved agent, routed through TokenLean (M1-15)."""

import asyncio
import json

import httpx2
import pytest
from pydantic_ai import Agent

from agentic.agents.models import GatewaySettings, build_model, model_settings

GATEWAY = GatewaySettings(base_url="https://tokenlean.acme.internal", api_key="tok-gateway")


@pytest.fixture(autouse=True)
def _identity(identity):
    global IDENTITY
    IDENTITY = identity


def run(agent, tokenlean, **settings) -> str:
    model = build_model(agent, IDENTITY, GATEWAY, transport=httpx2.MockTransport(tokenlean))
    result = asyncio.run(Agent(model).run("hi", model_settings=settings or None))
    return result.output


def test_anthropic_models_use_the_messages_route(resolved, tokenlean):
    assert resolved.model.startswith("anthropic/")  # the fixture's balanced tier

    assert run(resolved, tokenlean) == "hello from tokenlean"
    sent = tokenlean.seen[-1]
    assert sent.url.path == "/v1/messages"
    assert json.loads(sent.content)["model"] == resolved.model.split("/", 1)[1]
    assert sent.headers["x-agentic-run-id"] == "r-sp4"
    assert sent.headers["x-api-key"] == "tok-gateway"


def test_other_models_use_the_openai_route(resolved, tokenlean):
    agent = resolved.model_copy(update={"model": "google/gemini-flash"})

    assert run(agent, tokenlean) == "hello from tokenlean"
    sent = tokenlean.seen[-1]
    assert sent.url.path == "/v1/chat/completions"
    assert json.loads(sent.content)["model"] == "gemini-flash"
    assert sent.headers["authorization"] == "Bearer tok-gateway"
    assert sent.headers["x-agentic-team"] == "payments"


def test_a_model_without_a_provider_prefix_uses_the_openai_route(resolved, tokenlean):
    run(resolved.model_copy(update={"model": "gpt-5"}), tokenlean)

    assert tokenlean.seen[-1].url.path == "/v1/chat/completions"
    assert json.loads(tokenlean.seen[-1].content)["model"] == "gpt-5"


def test_temperature_is_passed_through(resolved, tokenlean):
    # not gpt-5: Pydantic AI drops sampling settings for reasoning models (with only a
    # warning) - the capability matrix (M1-27) must report that, not hide it
    agent = resolved.model_copy(update={"model": "google/gemini-flash", "temperature": 0.2})

    run(agent, tokenlean, **model_settings(agent))
    assert json.loads(tokenlean.seen[-1].content)["temperature"] == 0.2


def test_no_temperature_means_none_is_sent(resolved):
    assert "temperature" not in model_settings(resolved.model_copy(update={"temperature": None}))


def test_requests_go_only_to_the_gateway_host(resolved, tokenlean):
    run(resolved, tokenlean)
    run(resolved.model_copy(update={"model": "openai/gpt-5"}), tokenlean)

    assert {r.url.host for r in tokenlean.seen} == {"tokenlean.acme.internal"}
