"""The per-tenant rate-limit hook. A deployment injects its limiter into `create_app`; the
default allows everything. Authentication calls it once per request, after the tenant is
known, so limits are always per tenant and principal."""

from __future__ import annotations

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
    """The default limiter: every request is allowed."""

    async def check(self, tenant_id: UUID, principal_id: UUID, route: str) -> RateDecision:
        return RateDecision(allowed=True)
