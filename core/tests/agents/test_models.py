"""Tests for building a Pydantic AI model for a resolved agent, routed through TokenLean (M1-15)."""

import asyncio
import json
import time
from decimal import Decimal

import httpx2
import pytest
from pydantic_ai import Agent

from agentic.agents.models import GatewaySettings, build_model, metered_model, model_settings
from agentic.agents.runner import _limit_hit
from agentic.transport import BudgetLedger

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


# ── review I6: a metered model in one call; retries are the engine's decision ─


def metered(agent, tokenlean, limit: str):
    step = BudgetLedger(Decimal("5")).run.open("build", "step", Decimal(limit))
    model, log = metered_model(
        agent,
        IDENTITY,
        GATEWAY,
        budget=step,
        price_for=lambda name: None,
        fallback_estimate_usd=Decimal("0.01"),
        _inner=httpx2.MockTransport(tokenlean),
    )
    return model, log, step


def test_a_metered_model_charges_every_call_to_the_budget(resolved, tokenlean):
    model, log, step = metered(resolved, tokenlean, "1")

    assert asyncio.run(Agent(model).run("hi")).output == "hello from tokenlean"
    assert log.requests == 1 and step.spent() == Decimal("0.01")  # unpriced: the hold


@pytest.mark.parametrize("model_name", ["anthropic/claude-sonnet", "openai/gpt-5"])
def test_a_refused_reservation_is_not_retried_by_the_sdk(resolved, tokenlean, model_name):
    agent = resolved.model_copy(update={"model": model_name})
    model, _, _ = metered(agent, tokenlean, "0.001")  # less than one call's hold

    started = time.perf_counter()
    with pytest.raises(Exception) as caught:
        asyncio.run(Agent(model).run("hi"))
    assert _limit_hit(caught.value) is not None
    assert time.perf_counter() - started < 1  # no backoff and retry first
    assert tokenlean.seen == []
