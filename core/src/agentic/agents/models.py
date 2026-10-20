"""A Pydantic AI model for a resolved agent, routed through TokenLean (M1-15).

The resolved model is `"<provider>/<name>"` (e.g. `anthropic/claude-sonnet`,
`openai/gpt-5`). Every call goes to TokenLean, never to a provider:

  * `anthropic/...` -> TokenLean's Anthropic route (`POST {base}/v1/messages`)
  * anything else   -> TokenLean's OpenAI-compatible route (`POST {base}/v1/chat/completions`),
                       which TokenLean translates for the real provider.

The name after the first `/` is what TokenLean routes on. The HTTP client is the
identity client (`make_async_client`) over the given transport. The API key is
handed to the SDK client, which sets the auth header itself (spike S1); SDK
retries are off (`agentic.transport.sdk`).

`metered_model` is what production uses: network transport (corporate CA and
proxy) -> metering transport (the budget) -> identity client -> model, built in
one call, with the run's `CostLog`. `build_model` needs the transport spelled
out, so a model is never unmetered by default (review I6).
"""

from dataclasses import dataclass
from decimal import Decimal

import httpx2
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from agentic.agents.metering import CostLog, MeteringTransport
from agentic.policy import ResolvedAgent
from agentic.transport import (
    BudgetSlice,
    CallIdentity,
    NetworkSettings,
    Pricer,
    make_async_client,
    make_network_transport,
)
from agentic.transport.sdk import anthropic_client, openai_client


@dataclass(frozen=True)
class GatewaySettings:
    base_url: str  # TokenLean's root, e.g. https://tokenlean.acme.internal
    api_key: str  # the user's TokenLean key
    network: NetworkSettings | None = None


def build_model(
    agent: ResolvedAgent,
    identity: CallIdentity,
    gateway: GatewaySettings,
    transport: httpx2.AsyncBaseTransport,
) -> Model:
    """The model the agent runs on, calling TokenLean through the identity client."""
    provider, _, name = agent.model.partition("/")
    if not name:
        provider, name = "", agent.model
    root = gateway.base_url.rstrip("/")
    client = make_async_client(root, identity, network=gateway.network, _transport=transport)
    if provider == "anthropic":
        sdk = anthropic_client(root, gateway.api_key, client)
        return AnthropicModel(name, provider=AnthropicProvider(anthropic_client=sdk))
    sdk = openai_client(f"{root}/v1", gateway.api_key, client)
    return OpenAIChatModel(name, provider=OpenAIProvider(openai_client=sdk))


def metered_model(
    agent: ResolvedAgent,
    identity: CallIdentity,
    gateway: GatewaySettings,
    *,
    budget: BudgetSlice,
    price_for: Pricer,
    fallback_estimate_usd: Decimal,
    log: CostLog | None = None,
    _inner: httpx2.AsyncBaseTransport | None = None,  # tests: a stand-in for the network
) -> tuple[Model, CostLog]:
    """The agent's model with every call charged to `budget`, and the log of those
    calls - pass the same log to the runner so its outcome reports them."""
    log = log if log is not None else CostLog()
    meter = MeteringTransport(
        _inner or make_network_transport(gateway.network),
        budget=budget,
        price_for=price_for,
        fallback_estimate_usd=fallback_estimate_usd,
        log=log,
    )
    return build_model(agent, identity, gateway, transport=meter), log


def model_settings(agent: ResolvedAgent) -> ModelSettings:
    """The sampling settings policy resolved for the agent: temperature, and effort as
    Pydantic AI's provider-neutral `thinking` level (M1-28). What a model cannot honour
    is reported before the run by the capability matrix (M1-27)."""
    settings = ModelSettings()
    if agent.temperature is not None:
        settings["temperature"] = agent.temperature
    if agent.effort is not None:
        settings["thinking"] = agent.effort
    return settings
