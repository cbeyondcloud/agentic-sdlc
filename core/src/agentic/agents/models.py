"""A Pydantic AI model for a resolved agent, routed through TokenLean (M1-15).

The resolved model is `"<provider>/<name>"` (e.g. `anthropic/claude-sonnet`,
`openai/gpt-5`). Every call goes to TokenLean, never to a provider:

  * `anthropic/...` -> TokenLean's Anthropic route (`POST {base}/v1/messages`)
  * anything else   -> TokenLean's OpenAI-compatible route (`POST {base}/v1/chat/completions`),
                       which TokenLean translates for the real provider.

The name after the first `/` is what TokenLean routes on. The HTTP client is the
identity client (`make_async_client`) over the given transport - the metering
transport in production. The API key is handed to the provider, which sets the
auth header itself (spike S1).
"""

from dataclasses import dataclass

import httpx2
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from agentic.policy import ResolvedAgent
from agentic.transport import CallIdentity, NetworkSettings, make_async_client


@dataclass(frozen=True)
class GatewaySettings:
    base_url: str  # TokenLean's root, e.g. https://tokenlean.acme.internal
    api_key: str  # the user's TokenLean key
    network: NetworkSettings | None = None


def build_model(
    agent: ResolvedAgent,
    identity: CallIdentity,
    gateway: GatewaySettings,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> Model:
    """The model the agent runs on, calling TokenLean through the identity client."""
    provider, _, name = agent.model.partition("/")
    if not name:
        provider, name = "", agent.model
    root = gateway.base_url.rstrip("/")
    client = make_async_client(root, identity, network=gateway.network, _transport=transport)
    if provider == "anthropic":
        return AnthropicModel(
            name,
            provider=AnthropicProvider(base_url=root, api_key=gateway.api_key, http_client=client),
        )
    return OpenAIChatModel(
        name,
        provider=OpenAIProvider(base_url=f"{root}/v1", api_key=gateway.api_key, http_client=client),
    )


def model_settings(agent: ResolvedAgent) -> ModelSettings:
    """The sampling settings policy resolved for the agent. Effort and other
    backend-specific settings are mapped with the capability matrix (M1-27)."""
    settings = ModelSettings()
    if agent.temperature is not None:
        settings["temperature"] = agent.temperature
    return settings
