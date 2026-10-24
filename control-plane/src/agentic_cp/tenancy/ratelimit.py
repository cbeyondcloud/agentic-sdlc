"""The per-tenant rate-limit hook. Authentication calls it once per request, after the tenant
is known, so limits are always per tenant and principal.

The default (DF-309) is `TokenBucketLimiter` from the settings: a bucket per principal of
`rate_limit_per_minute` requests (a minute's worth as burst, refilled continuously), and,
when `tenant_rate_limit_per_minute` is set, one bucket for all of a tenant's principals.
Buckets live in this process: with several replicas each has its own, so a deployment-wide
limit (and any limit before authentication, DF-410) belongs at the ingress. A deployment
passes its own limiter to `create_app(rate_limiter=...)` to replace it."""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after: int | None = None
    """Seconds until the caller may try again (sent as `Retry-After`)."""


class RateLimiter(Protocol):
    async def check(self, tenant_id: UUID, principal_id: UUID, route: str) -> RateDecision: ...


class NoLimit:
    """Every request is allowed (`AGENTIC_CP_RATE_LIMIT_PER_MINUTE=0`)."""

    async def check(self, tenant_id: UUID, principal_id: UUID, route: str) -> RateDecision:
        return RateDecision(allowed=True)


class _Buckets:
    """Token buckets by key, least recently used evicted beyond `max_keys` (an evicted key
    starts again full: the bound costs leniency, never a refusal)."""

    def __init__(self, per_minute: int, max_keys: int, clock: Callable[[], float]) -> None:
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0  # tokens per second
        self.max_keys = max_keys
        self.clock = clock
        self.levels: OrderedDict[object, tuple[float, float]] = OrderedDict()

    def level(self, key: object) -> float:
        now = self.clock()
        tokens, then = self.levels.get(key, (self.capacity, now))
        return min(self.capacity, tokens + (now - then) * self.rate)

    def wait(self, tokens: float) -> int:
        return max(1, math.ceil((1.0 - tokens) / self.rate))

    def store(self, key: object, tokens: float) -> None:
        self.levels[key] = (tokens, self.clock())
        self.levels.move_to_end(key)
        while len(self.levels) > self.max_keys:
            self.levels.popitem(last=False)


class TokenBucketLimiter:
    """Per-principal (and optionally per-tenant) token buckets in this process."""

    def __init__(
        self,
        per_minute: int,
        *,
        tenant_per_minute: int = 0,
        max_keys: int = 50_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_minute = per_minute
        self.principals = _Buckets(per_minute, max_keys, clock)
        self.tenants = _Buckets(tenant_per_minute, max_keys, clock) if tenant_per_minute else None

    def size(self) -> int:
        """The buckets held (bounded by `max_keys` per kind)."""
        return len(self.principals.levels) + (len(self.tenants.levels) if self.tenants else 0)

    async def check(self, tenant_id: UUID, principal_id: UUID, route: str) -> RateDecision:
        key = (tenant_id, principal_id)
        mine = self.principals.level(key)
        if mine < 1.0:
            self.principals.store(key, mine)
            return RateDecision(allowed=False, retry_after=self.principals.wait(mine))
        if self.tenants is not None:
            theirs = self.tenants.level(tenant_id)
            if theirs < 1.0:
                self.tenants.store(tenant_id, theirs)
                return RateDecision(allowed=False, retry_after=self.tenants.wait(theirs))
            self.tenants.store(tenant_id, theirs - 1.0)
        self.principals.store(key, mine - 1.0)
        return RateDecision(allowed=True)


def limiter_for(settings) -> RateLimiter:
    """The default limiter of a deployment's settings."""
    if settings.rate_limit_per_minute <= 0:
        return NoLimit()
    return TokenBucketLimiter(
        settings.rate_limit_per_minute, tenant_per_minute=settings.tenant_rate_limit_per_minute
    )
