"""Tests for building a Pydantic AI model for a resolved agent, routed through TokenLean (M1-15)."""

import asyncio
import json
import time
from decimal import Decimal

import httpx2
import pytest
from pydantic_ai import Agent

from agentic.agents.models import GatewaySettings, build_model, metered_model, model_settings
from agentic.agents.runner import AgentTask, PydanticAIRunner, _found, _limit_hit
from agentic.security import SecretBlocked, SecretScanner
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


def metered(agent, tokenlean, limit: str, scanner: SecretScanner | None = None):
    step = BudgetLedger(Decimal("5")).run.open("build", "step", Decimal(limit))
    model, log = metered_model(
        agent,
        IDENTITY,
        GATEWAY,
        budget=step,
        price_for=lambda name: None,
        fallback_estimate_usd=Decimal("0.01"),
        scanner=scanner,
        _inner=httpx2.MockTransport(tokenlean),
    )
    return model, log, step


# ── M1-41: a secret never leaves; the masked variant does ─────────────────────

AWS = "AKIA" + "Z7Q4R2XW" + "9PLM3TKB"  # assembled, so this file never holds one whole


@pytest.mark.parametrize("model_name", ["anthropic/claude-sonnet", "openai/gpt-5"])
def test_a_planted_secret_is_blocked_before_any_http_call(resolved, tokenlean, model_name):
    agent = resolved.model_copy(update={"model": model_name})
    model, log, step = metered(agent, tokenlean, "1")

    with pytest.raises(Exception) as caught:
        asyncio.run(Agent(model).run(f"deploy with key {AWS}"))
    assert _found(caught.value, SecretBlocked) is not None
    assert tokenlean.seen == []  # nothing left the machine
    assert log.requests == 0 and step.spent() == Decimal(0)
    assert step.available() == Decimal(1)  # nothing reserved either


def test_a_blocked_run_raises_the_secret_error_itself(resolved, tokenlean, tmp_path):
    # the SDK wraps a transport error as a connection error; the engine must see why
    model, log, step = metered(resolved, tokenlean, "1")
    runner = PydanticAIRunner(resolved, model, budget=step, state_dir=tmp_path, costs=log)
    task = AgentTask(instructions="You are the Developer.", inputs={}, prompt=f"use {AWS}")

    with pytest.raises(SecretBlocked, match="AWS Access Key"):
        asyncio.run(runner.run(task))
    assert tokenlean.seen == []


@pytest.mark.parametrize("model_name", ["anthropic/claude-sonnet", "openai/gpt-5"])
def test_the_masked_variant_is_sent(resolved, tokenlean, model_name):
    agent = resolved.model_copy(update={"model": model_name})
    model, log, _ = metered(agent, tokenlean, "1", scanner=SecretScanner("mask"))

    assert asyncio.run(Agent(model).run(f"deploy with key {AWS}")).output
    (sent,) = tokenlean.seen
    assert AWS.encode() not in sent.content
    assert b"[MASKED SECRET: AWS Access Key]" in sent.content
    assert log.requests == 1


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
