"""Spike S1 (throwaway): cost capture through Pydantic AI over our transport client.

Questions:
  Q1  Do Pydantic AI's OpenAI and Anthropic providers accept our httpx client,
      so every request carries the X-Agentic-* identity headers?
  Q2  Non-streaming: can a wrapping transport read TokenLean's cost
      (`_token_opt.cost_actual_usd`) without breaking the SDK's own parsing?
  Q3  Streaming: does Pydantic AI ask for the usage chunk itself
      (`stream_options.include_usage`)? Can the wrapper tee SSE events into
      `StreamUsage` without buffering, and report the cost when the stream ends?
  Q4  Anthropic route: same, from `message_delta` usage.
  Q5  A LimitHit raised under the SDK (budget refused before sending): what
      exception does `agent.run` surface?

TokenLean is stood in for by an httpx.MockTransport returning TokenLean's real
response shapes (see core/tests/fixtures/transport/); no network, no keys.
Run:  uv run --with "pydantic-ai-slim[openai,anthropic]==2.54.0" python spikes/s1_cost_capture/spike.py
"""

import asyncio
import json
import os
import sys
from decimal import Decimal

if os.environ.get("SPIKE_HTTPX2"):  # probe: run our transport on httpx2 instead of httpx
    import httpx2

    sys.modules["httpx"] = httpx2

import httpx
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from agentic.policy import ModelPrice
from agentic.transport import (
    BudgetLedger,
    CallIdentity,
    LimitHit,
    StreamUsage,
    cost_from_response,
    make_async_client,
)

PRICES = {"gpt-5-mini": ModelPrice(input_per_mtok=Decimal("0.25"), output_per_mtok=Decimal("2")),
          "claude-sonnet-5-5": ModelPrice(input_per_mtok=Decimal("3"),
                                          output_per_mtok=Decimal("15"))}
IDENTITY = CallIdentity(team="payments", user="u123", repo="github.com/acme/api",
                        pipeline="feature-delivery", run_id="r-s1", step="build",
                        agent="developer@1.2.0", policy_version="git:" + "a" * 40)
SENT: list[httpx.Request] = []


# ── a stand-in for TokenLean: its real response shapes ────────────────────────


def openai_json() -> dict:
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-5-mini",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hello from tokenlean"}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500},
            "_token_opt": {"request_id": "req-1", "routed_model": "gpt-5-mini",
                           "tokens_provider_billed": 1200, "response_tokens": 300,
                           "cost_actual_usd": 0.004321, "cache_hit": False}}


def openai_sse() -> bytes:
    def chunk(choices, usage=None):
        return {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "gpt-5-mini",
                "choices": choices, "usage": usage}

    events = [
        chunk([{"index": 0, "delta": {"role": "assistant", "content": "hel"}}]),
        chunk([{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]),
        chunk([], {"prompt_tokens": 800, "completion_tokens": 150, "total_tokens": 950}),
    ]
    return b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events) + b"data: [DONE]\n\n"


def anthropic_json() -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": "hello from tokenlean"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 200}}


def anthropic_sse() -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_2", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "hello"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"input_tokens": 812, "output_tokens": 95}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(f"event: {n}\ndata: {json.dumps(d)}\n\n".encode() for n, d in events)


def tokenlean(request: httpx.Request) -> httpx.Response:
    SENT.append(request)
    body = json.loads(request.content or b"{}")
    anthropic = request.url.path.endswith("/messages")
    if body.get("stream"):
        content = anthropic_sse() if anthropic else openai_sse()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)
    headers = {"x-tokenlean-request-id": "req-1",
               "x-tokenlean-routed-model": body.get("model", ""),
               "x-trace-id": "0af7651916cd43dd8448eb211c80319c"}
    return httpx.Response(
        200, headers=headers, json=anthropic_json() if anthropic else openai_json()
    )


# ── the candidate design: a cost-capturing transport under our client ─────────


class CostCapture(httpx.AsyncBaseTransport):
    """Wraps the real transport. Non-stream: reads the body and prices it. Stream:
    passes each chunk through untouched while feeding SSE events to StreamUsage,
    and reports the cost when the stream is closed - no buffering."""

    def __init__(self, inner: httpx.AsyncBaseTransport, on_cost) -> None:
        self.inner, self.on_cost = inner, on_cost

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            return httpx.Response(response.status_code, headers=response.headers,
                                  stream=_TeeStream(response, self.on_cost),
                                  request=request)
        content = await response.aread()
        body = json.loads(content) if content else None
        self.on_cost(cost_from_response(response.headers, body, PRICES.get))
        return httpx.Response(response.status_code, headers=response.headers, content=content,
                              request=request)


class _TeeStream(httpx.AsyncByteStream):
    def __init__(self, response: httpx.Response, on_cost) -> None:
        self.response, self.on_cost = response, on_cost
        self.usage, self.buffer, self.chunks = StreamUsage(), b"", 0

    async def __aiter__(self):
        async for chunk in self.response.stream:  # the raw bytes, at the transport level
            self.chunks += 1
            self.buffer += chunk
            while b"\n\n" in self.buffer:
                frame, self.buffer = self.buffer.split(b"\n\n", 1)
                for line in frame.split(b"\n"):
                    if line.startswith(b"data: ") and line != b"data: [DONE]":
                        self.usage.observe(json.loads(line[6:]))
            yield chunk

    async def aclose(self) -> None:
        if hasattr(self.response.stream, "aclose"):
            await self.response.stream.aclose()
        self.on_cost(self.usage.result(self.response.headers, PRICES.get))


def client(on_cost, *, before_send=None) -> httpx.AsyncClient:
    inner = httpx.MockTransport(tokenlean)
    transport = CostCapture(inner, on_cost)
    c = make_async_client("https://tl.example/v1", IDENTITY, api_key="tok-x", _transport=transport)
    if before_send:
        c.event_hooks["request"].append(before_send)
    return c


# ── the probes ────────────────────────────────────────────────────────────────


async def main() -> None:
    costs = []

    # Q1 + Q2: OpenAI route, non-streaming
    SENT.clear()
    model = OpenAIChatModel("gpt-5-mini", provider=OpenAIProvider(
        base_url="https://tl.example/v1", api_key="tok-x", http_client=client(costs.append)))
    result = await Agent(model).run("hi")
    sent = SENT[-1]
    print(
        "Q1 openai identity headers:",
        sent.headers.get("x-agentic-run-id"),
        sent.headers.get("traceparent")[:20],
        "| auth:",
        sent.headers.get("authorization"),
    )
    print(
        "Q2 openai non-stream:",
        repr(result.output),
        "| cost:",
        costs[-1].source,
        costs[-1].cost_usd,
        "| pydantic-ai usage:",
        result.usage.input_tokens,
        result.usage.output_tokens,
    )

    # Q3: OpenAI route, streaming
    SENT.clear()
    async with Agent(model).run_stream("hi") as stream:
        text = await stream.get_output()
    body = json.loads(SENT[-1].content)
    print(
        "Q3 openai stream: output",
        repr(text),
        "| stream_options sent by pydantic-ai:",
        body.get("stream_options"),
        "| cost:",
        costs[-1].source,
        costs[-1].cost_usd,
        costs[-1].tokens_in,
        costs[-1].tokens_out,
    )

    # Q4: Anthropic route, non-stream and stream
    SENT.clear()
    try:
        amodel = AnthropicModel("claude-sonnet-5-5", provider=AnthropicProvider(
            base_url="https://tl.example", api_key="tok-x", http_client=client(costs.append)))
        result = await Agent(amodel).run("hi")
        print(
            "Q4 anthropic non-stream:",
            repr(result.output),
            "| cost:",
            costs[-1].source,
            costs[-1].cost_usd,
            "| identity:",
            SENT[-1].headers.get("x-agentic-team"),
            "| path:",
            SENT[-1].url.path,
        )
        async with Agent(amodel).run_stream("hi") as stream:
            text = await stream.get_output()
        print(
            "Q4 anthropic stream:",
            repr(text),
            "| cost:",
            costs[-1].source,
            costs[-1].cost_usd,
            costs[-1].tokens_in,
            costs[-1].tokens_out,
        )
    except Exception as error:  # noqa: BLE001 - a spike reports what happened
        print("Q4 anthropic FAILED:", type(error).__name__, str(error)[:300])

    # Q5: budget refused before sending - what does agent.run surface?
    ledger = BudgetLedger(Decimal("0.001"))
    step = ledger.run.open("build", "step", None)

    async def reserve(request: httpx.Request) -> None:
        step.reserve(Decimal("0.01"))  # more than is left: LimitHit

    model5 = OpenAIChatModel("gpt-5-mini", provider=OpenAIProvider(
        base_url="https://tl.example/v1", api_key="tok-x",
        http_client=client(costs.append, before_send=reserve)))
    try:
        await Agent(model5).run("hi")
        print("Q5: no exception (!)")
    except Exception as error:  # noqa: BLE001
        chain, e = [], error
        while e is not None:
            chain.append(type(e).__name__)
            e = e.__cause__ or e.__context__
        print(
            "Q5 LimitHit under the SDK surfaces as:",
            " <- ".join(chain),
            "| isinstance LimitHit:",
            isinstance(error, LimitHit),
        )


asyncio.run(main())
