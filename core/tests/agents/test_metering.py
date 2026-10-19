"""Tests for the metering transport: every model call reserved, settled or released (M1-15)."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest

from agentic.agents.metering import CostLog, MeteringTransport, estimate_request_usd
from agentic.policy import ModelPrice
from agentic.transport import BudgetLedger, LimitHit

D = Decimal
PRICES = {"gpt-5-mini": ModelPrice(input_per_mtok=D("0.25"), output_per_mtok=D("2"))}
REQUEST = {
    "model": "gpt-5-mini",
    "max_tokens": 1000,
    "messages": [{"role": "user", "content": "x" * 400}],
}
FALLBACK = D("0.05")


def fixture(fixtures: Path, name: str) -> dict:
    return json.loads((fixtures / "transport" / name).read_text(encoding="utf-8"))


def sse(events: list[dict]) -> bytes:
    return b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events) + b"data: [DONE]\n\n"


class Frames(httpx2.AsyncByteStream):
    """A stream that arrives one SSE frame per chunk, as a real one does."""

    def __init__(self, events: list[dict]) -> None:
        self.frames = [f"data: {json.dumps(e)}\n\n".encode() for e in events]

    async def __aiter__(self):
        for frame in self.frames:
            yield frame


class Upstream:
    """A stand-in for TokenLean that records what reached it."""

    def __init__(self, respond) -> None:
        self.respond, self.seen = respond, []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        return self.respond(request)


def metered(upstream: Upstream, budget, log: CostLog | None = None) -> httpx2.AsyncClient:
    transport = MeteringTransport(
        httpx2.MockTransport(upstream),
        budget=budget,
        price_for=PRICES.get,
        fallback_estimate_usd=FALLBACK,
        log=log,
    )
    return httpx2.AsyncClient(base_url="https://tl.example/v1", transport=transport)


def post(client: httpx2.AsyncClient, body: dict = REQUEST) -> httpx2.Response:
    async def call():
        async with client:
            return await client.post("/chat/completions", json=body)

    return asyncio.run(call())


def stream(client: httpx2.AsyncClient, *, chunks: int | None = None) -> bytes:
    async def call() -> bytes:
        async with client:
            body = {**REQUEST, "stream": True}
            async with client.stream("POST", "/chat/completions", json=body) as response:
                out = b""
                async for chunk in response.aiter_raw():
                    out += chunk
                    if chunks is not None and len(out) >= chunks:
                        break
                return out

    return asyncio.run(call())


# ── settle ────────────────────────────────────────────────────────────────────


def test_a_normal_response_is_settled_at_tokenlean_cost(fixtures):
    data = fixture(fixtures, "openai_nonstream.json")
    step = BudgetLedger(D("5")).run.open("build", "step", D("1"))
    log = CostLog()
    upstream = Upstream(lambda r: httpx2.Response(200, headers=data["headers"], json=data["body"]))

    response = post(metered(upstream, step, log))

    assert response.json()["choices"][0]["message"]["content"] == "done"  # the SDK sees it intact
    assert step.spent() == D("0.004321") and step.available() == D("1") - D("0.004321")
    assert (log.requests, log.total_usd(), log.by_source()) == (1, D("0.004321"), {"reported": 1})


def test_a_stream_passes_through_unchanged_and_settles_on_close(fixtures):
    events = fixture(fixtures, "openai_stream_events.json")["events"]
    body = sse(events)
    step = BudgetLedger(D("5")).run.open("build", "step", D("1"))
    upstream = Upstream(
        lambda r: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)
    )

    assert stream(metered(upstream, step)) == body
    assert step.spent() == D("0.0005")  # 800 in (200 cached, at input price) + 150 out


def test_a_stream_closed_early_charges_the_reservation(fixtures):
    events = fixture(fixtures, "openai_stream_events.json")["events"]
    step = BudgetLedger(D("5")).run.open("build", "step", D("1"))
    log = CostLog()
    upstream = Upstream(
        lambda r: httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Frames(events)
        )
    )

    stream(metered(upstream, step, log), chunks=10)  # stops after the first frame

    expected = estimate_request_usd({**REQUEST, "stream": True}, PRICES.get, FALLBACK)
    assert step.spent() == expected
    assert (log.by_source(), step.unpriced_calls) == ({"unavailable": 1}, 1)


# ── reserve and release ───────────────────────────────────────────────────────


def test_a_refused_reservation_sends_nothing():
    step = BudgetLedger(D("5")).run.open("build", "step", D("0.0000001"))
    upstream = Upstream(lambda r: httpx2.Response(200, json={}))

    with pytest.raises(LimitHit):
        post(metered(upstream, step))
    assert upstream.seen == []


def test_a_transport_error_releases_the_reservation():
    step = BudgetLedger(D("5")).run.open("build", "step", D("1"))

    def fail(request):
        raise httpx2.ConnectError("tokenlean unreachable")

    with pytest.raises(httpx2.ConnectError):
        post(metered(Upstream(fail), step))
    assert (step.spent(), step.available()) == (D("0"), D("1"))


def test_an_error_response_without_usage_is_not_charged():
    step = BudgetLedger(D("5")).run.open("build", "step", D("1"))
    upstream = Upstream(lambda r: httpx2.Response(503, json={"error": "overloaded"}))

    assert post(metered(upstream, step)).status_code == 503
    assert (step.spent(), step.available()) == (D("0"), D("1"))


def test_overshoot_on_settle_raises_limit_hit_after_recording(fixtures):
    data = fixture(fixtures, "openai_nonstream.json")  # reports $0.004321
    step = BudgetLedger(D("5")).run.open("build", "step", D("0.003"))
    body = {**REQUEST, "max_tokens": 1}  # a small estimate that fits
    upstream = Upstream(lambda r: httpx2.Response(200, headers=data["headers"], json=data["body"]))

    with pytest.raises(LimitHit):
        post(metered(upstream, step), body)
    assert step.spent() == D("0.004321")


# ── the estimate ──────────────────────────────────────────────────────────────


def test_the_estimate_uses_max_tokens_and_the_price():
    text_tokens = len(json.dumps(REQUEST)) // 4
    expected = (text_tokens * D("0.25") + 1000 * D("2")) / D(1_000_000)

    assert estimate_request_usd(REQUEST, PRICES.get, FALLBACK) == expected


def test_an_unpriced_model_reserves_the_fallback():
    assert estimate_request_usd({"model": "mystery"}, PRICES.get, FALLBACK) == FALLBACK


def test_a_request_without_max_tokens_assumes_a_large_answer():
    small = estimate_request_usd(REQUEST, PRICES.get, FALLBACK)
    unbounded = estimate_request_usd(
        {k: v for k, v in REQUEST.items() if k != "max_tokens"}, PRICES.get, FALLBACK
    )
    assert unbounded > small


def test_a_body_that_is_not_json_reserves_the_fallback():
    assert estimate_request_usd(None, PRICES.get, FALLBACK) == FALLBACK
