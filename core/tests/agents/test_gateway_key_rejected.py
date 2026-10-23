"""A revoked or rotated gateway key (M2-17, Review Focus 6): the gateway's 401 becomes
`GatewayKeyRejected` on every route and through the provider SDK's wrapping, so the engine
can pause the run with an actionable next step instead of a raw HTTP error; and a key can
be checked with the gateway before it is stored."""

import asyncio
from decimal import Decimal

import httpx2
import pytest
from pydantic_ai import Agent

from agentic.agents.models import GatewaySettings, build_model, metered_model
from agentic.agents.runner import AgentTask, PydanticAIRunner
from agentic.transport import BudgetLedger, GatewayKeyRejected, verify_gateway_key

GATEWAY = GatewaySettings(base_url="https://gateway.acme.internal", api_key="gw-" + "revoked")


def _refusing(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(401, json={"detail": "Invalid API key"})


@pytest.mark.parametrize("model_name", ["anthropic/claude-sonnet", "openai/gpt-5"])
def test_a_gateway_401_is_a_rejected_key(resolved, identity, model_name):
    agent = resolved.model_copy(update={"model": model_name})
    model = build_model(agent, identity, GATEWAY, transport=httpx2.MockTransport(_refusing))
    with pytest.raises(Exception) as caught:
        asyncio.run(Agent(model).run("hi"))
    found = caught.value
    while found is not None and not isinstance(found, GatewayKeyRejected):
        found = found.__cause__ or found.__context__
    assert isinstance(found, GatewayKeyRejected)
    assert "agentic login" in str(found)


def test_the_runner_raises_the_rejection_itself(resolved, identity, tmp_path):
    step = BudgetLedger(Decimal("5")).run.open("build", "step", Decimal("1"))
    model, log = metered_model(
        resolved,
        identity,
        GATEWAY,
        budget=step,
        price_for=lambda name: None,
        fallback_estimate_usd=Decimal("0.01"),
        _inner=httpx2.MockTransport(_refusing),
    )
    runner = PydanticAIRunner(resolved, model, budget=step, state_dir=tmp_path, costs=log)
    task = AgentTask(instructions="You are the Developer.", inputs={}, prompt="do it")
    with pytest.raises(GatewayKeyRejected):
        asyncio.run(runner.run(task))
    assert step.spent() == Decimal(0)  # an error without usage costs nothing


def _verify(handler) -> str:
    return verify_gateway_key(
        "https://gateway.acme.internal/", "gw-key", transport=httpx2.MockTransport(handler)
    )


def test_verify_asks_the_models_route_with_the_key():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(200, json={"object": "list", "data": []})

    assert _verify(handler) == "accepted"
    assert seen[0].method == "GET" and str(seen[0].url) == "https://gateway.acme.internal/v1/models"
    assert seen[0].headers["authorization"] == "Bearer gw-key"


@pytest.mark.parametrize(
    ("status", "verdict"),
    [(401, "rejected"), (403, "rejected"), (500, "unknown"), (404, "unknown")],
)
def test_verify_reads_the_answer(status, verdict):
    assert _verify(lambda request: httpx2.Response(status)) == verdict


def test_verify_without_a_network_is_unknown():
    def handler(request):
        raise httpx2.ConnectError("refused", request=request)

    assert _verify(handler) == "unknown"
