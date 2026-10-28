# Spike S1 — Cost capture through Pydantic AI over our transport client

**Date:** 2026-10-19 · **Code (throwaway):** `spikes/s1_cost_capture/spike.py` · **Versions:** pydantic-ai-slim 2.54.0 (released 2026-10-03), openai 3.35.1, anthropic 1.15.0, httpx2 2.13.1–2.14.0

## Question

Can `agentic.transport` (identity headers, cost capture, budget ledger) sit under Pydantic AI's model calls, for normal and streamed responses on both TokenLean routes, and how does a budget refusal surface?

## Method

TokenLean was stood in for by an `httpx.MockTransport` returning TokenLean's real response shapes (body `_token_opt`, OpenAI SSE with a final usage chunk, Anthropic SSE with `message_start` … `message_delta`). The fixtures mirror TokenLean's source, read on 2026-10-19. A candidate design ran underneath our client: a wrapping transport (`CostCapture`) that prices normal responses and passes stream chunks through untouched while feeding SSE events to `StreamUsage`. Real TokenLean end to end belongs to M1-67/M1-68.

## Results

| # | Question | Result |
|---|---|---|
| Q1 | Do the providers accept our client and carry identity? | **Yes** on both routes: `X-Agentic-*` and `traceparent` arrive. The API key must be given to the **provider** (`api_key=`): the OpenAI SDK replaces the client's `Authorization` header with its own (`Bearer api-key-not-set` otherwise). |
| Q2 | Normal response: TokenLean's cost without breaking the SDK? | **Yes.** The transport reads the body, prices it (`reported` $0.004321), and hands the SDK an identical response. Pydantic AI still reports 1200/300 tokens. |
| Q3 | Streams: usage chunk, and no buffering? | **Yes.** Pydantic AI already sends `stream_options: {"include_usage": true}` on the OpenAI route. The tee stream gives Pydantic AI its text unchanged and reports `estimated` $0.0005 (800/150 tokens) when the stream closes. |
| Q4 | Anthropic route | **Fails on `httpx`**: the Anthropic SDK 1.15 refuses an `httpx.AsyncClient` and requires `httpx2.AsyncClient`. **Works on `httpx2`**: identity arrives at `/v1/messages`, and the cost is estimated from `message_delta` ($0.003861, 812/95). Pydantic AI's Anthropic model **streams even for `run()`**, so stream capture is its main path. |
| Q5 | A `LimitHit` raised under the SDK | It surfaces from `agent.run` **as `LimitHit` itself** (`isinstance` true; an `ExceptionGroup` sits in its cause chain). The engine can catch it directly. |

**httpx → httpx2.** `httpx2` is the Pydantic team's maintained successor to httpx (github.com/pydantic/httpx2, BSD-3-Clause), with the same client API: `event_hooks`, `transport`, `verify`, `trust_env`, `follow_redirects`, `MockTransport`. Pydantic AI deprecates plain `httpx` clients for OpenAI-compatible providers (removal in v3), and the Anthropic SDK already requires `httpx2`. **All 109 `core/tests/transport` tests pass unchanged with `httpx2` swapped in** (including the real TLS and proxy tests), and a real `httpx2.AsyncClient` is accepted by both providers with no warnings.

**Telemetry.** pydantic-ai-slim made no request except through our client (every request reached the stand-in; the client refuses other hosts). It prints a banner for its hosted observability product, silenced by `PYDANTIC_AI_NO_BANNER=1`; the CLI sets it.

## Decision

1. **D36 — `agentic.transport` moves from `httpx` to `httpx2`** (same API, same licence family). It's done as the first task of SP4, with tests, and the dependency is pinned to a release at least two weeks old.
2. **Cost capture is a wrapping transport under our client** (the `CostCapture` design above), not a response event hook. It works on both routes, for normal and streamed responses, without buffering streams. SP4 builds it with budget wiring: reserve before sending, settle when the response (or stream) ends, release on error.
3. **The engine catches `LimitHit` from `agent.run` directly**, and also checks the ledger between calls.
4. **Pass the API key to the provider**, not the client; the client still refuses foreign hosts.
5. Pin pydantic-ai-slim to a release at least two weeks old (2.54.0 now).
