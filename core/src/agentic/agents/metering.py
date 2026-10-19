"""Every model call charged to the budget (M1-15; spike S1).

A transport under the identity client (`agentic.transport.make_async_client`),
so it sees every request Pydantic AI or a provider SDK makes:

  1. reserve an estimate before sending - refused (LimitHit) means nothing is
     sent. The estimate is the model's price x (input ~ JSON characters / 4,
     output = `max_tokens`, else a large answer), or a fallback for a model
     with no price; never zero, because an unpriced call is charged it;
  2. send through the real transport;
  3. a normal response is read, priced (`cost_from_response`) and settled; a
     stream is passed through untouched while its events feed `StreamUsage`,
     and is settled when it is closed - a stream closed before it finished is
     charged its reservation;
  4. a transport error, or an error response with no usage, releases the
     reservation: a failed call is never charged.

A settle that overshoots records the spend, then raises `LimitHit`.
"""

import json
from collections import Counter
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

import httpx2

from agentic.transport import (
    BudgetSlice,
    CallCost,
    LimitHit,
    Pricer,
    Reservation,
    StreamUsage,
    cost_from_response,
    estimate_usd,
)

#: Output assumed when a request sets no max_tokens (a long answer, so the hold is honest).
DEFAULT_MAX_OUTPUT = 16_384
_SMALLEST = Decimal("0.000001")  # a reservation is never zero


@dataclass
class CostLog:
    """Every call of one agent run: its cost, and what the budget was charged."""

    calls: list[tuple[CallCost, Decimal]] = field(default_factory=list)
    limit_hit: LimitHit | None = None  # the first, if a settle crossed a limit

    def add(self, cost: CallCost, charged: Decimal) -> None:
        self.calls.append((cost, charged))

    @property
    def requests(self) -> int:
        return len(self.calls)

    def total_usd(self) -> Decimal:
        return sum((charged for _, charged in self.calls), Decimal(0))

    def by_source(self) -> dict[str, int]:
        return dict(Counter(cost.source for cost, _ in self.calls))


def estimate_request_usd(body: Mapping | None, price_for: Pricer, fallback: Decimal) -> Decimal:
    """What to hold before a call: price x (input estimate + max output), else `fallback`."""
    if not isinstance(body, Mapping):
        return fallback
    model = body.get("model")
    price = price_for(model) if isinstance(model, str) and model else None
    if price is None:
        return fallback
    out = body.get("max_tokens") or body.get("max_completion_tokens") or DEFAULT_MAX_OUTPUT
    out = (
        out
        if isinstance(out, int) and not isinstance(out, bool) and out > 0
        else DEFAULT_MAX_OUTPUT
    )
    tokens_in = len(json.dumps(body)) // 4
    return max(estimate_usd(price, tokens_in, out), _SMALLEST)


class MeteringTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport,
        *,
        budget: BudgetSlice,
        price_for: Pricer,
        fallback_estimate_usd: Decimal,
        log: CostLog | None = None,
    ) -> None:
        if fallback_estimate_usd <= 0:
            raise ValueError("the fallback estimate must be above zero")
        self.inner, self.budget, self.price_for = inner, budget, price_for
        self.fallback, self.log = fallback_estimate_usd, log if log is not None else CostLog()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        body = _json(request.content)
        reservation = self.budget.reserve(estimate_request_usd(body, self.price_for, self.fallback))
        try:
            response = await self.inner.handle_async_request(request)
        except BaseException:
            reservation.release()
            raise
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            metered = _MeteredStream(response, reservation, self)
            return httpx2.Response(
                response.status_code, headers=response.headers, stream=metered, request=request
            )
        try:
            content = await response.aread()
        except BaseException:
            reservation.release()
            raise
        cost = cost_from_response(response.headers, _json(content), self.price_for)
        if response.is_success or cost.tokens_in is not None:
            self._settle(reservation, cost)
        else:
            reservation.release()  # an error with no usage: nothing was spent
        return httpx2.Response(
            response.status_code, headers=response.headers, content=content, request=request
        )

    async def aclose(self) -> None:
        await self.inner.aclose()

    def _settle(self, reservation: Reservation, cost: CallCost) -> None:
        try:
            charged = reservation.settle(cost)
        except LimitHit as hit:
            charged = cost.cost_usd if cost.cost_usd is not None else reservation.estimate_usd
            self.log.add(cost, charged)
            self.log.limit_hit = self.log.limit_hit or hit
            raise
        self.log.add(cost, charged)


class _MeteredStream(httpx2.AsyncByteStream):
    """Passes the raw bytes through untouched; feeds SSE `data:` events to StreamUsage;
    settles once, when closed."""

    def __init__(
        self, response: httpx2.Response, reservation: Reservation, meter: MeteringTransport
    ) -> None:
        self.response, self.reservation, self.meter = response, reservation, meter
        self.usage, self.pending, self.settled = StreamUsage(), b"", False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.response.stream:
            self._feed(chunk)
            yield chunk

    async def aclose(self) -> None:
        if hasattr(self.response.stream, "aclose"):
            await self.response.stream.aclose()
        if not self.settled:
            self.settled = True
            cost = self.usage.result(self.response.headers, self.meter.price_for)
            self.meter._settle(self.reservation, cost)

    def _feed(self, chunk: bytes) -> None:
        self.pending += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in self.pending:
            frame, self.pending = self.pending.split(b"\n\n", 1)
            for line in frame.split(b"\n"):
                if line.startswith(b"data:"):
                    event = _json(line[5:].strip())
                    if isinstance(event, Mapping):
                        self.usage.observe(event)


def _json(content: bytes | None) -> dict | None:
    try:
        value = json.loads(content) if content else None
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None
