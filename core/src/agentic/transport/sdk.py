"""The provider SDK clients, built over an identity client (the only place the
`openai` and `anthropic` SDKs are imported - P0-03).

Their own retries are off (`max_retries=0`): a retry would hide a refused budget
reservation behind seconds of backoff, and TokenLean already fails over between
providers. Whether to try a failed call again is the engine's decision.
"""

import httpx2
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI


def openai_client(base_url: str, api_key: str, http_client: httpx2.AsyncClient) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=base_url, api_key=api_key, http_client=http_client, max_retries=0)


def anthropic_client(
    base_url: str, api_key: str, http_client: httpx2.AsyncClient
) -> AsyncAnthropic:
    return AsyncAnthropic(
        base_url=base_url, api_key=api_key, http_client=http_client, max_retries=0
    )
