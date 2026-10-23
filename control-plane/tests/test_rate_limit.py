"""The default rate limiter (DF-309): a token bucket per principal (and, when configured, per
tenant), in this process. It refills continuously, keeps one principal's flood from using
another's budget, answers 429 with `Retry-After`, and holds a bounded number of buckets."""

from __future__ import annotations

import uuid

import pytest

from agentic_cp.app import create_app
from agentic_cp.settings import Settings
from agentic_cp.tenancy.ratelimit import NoLimit, TokenBucketLimiter

pytestmark = pytest.mark.anyio

TENANT, OTHER_TENANT = uuid.uuid4(), uuid.uuid4()
ALICE, BOB = uuid.uuid4(), uuid.uuid4()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _take(limiter, tenant, principal, times: int) -> list[bool]:
    return [(await limiter.check(tenant, principal, "/v1/x")).allowed for _ in range(times)]


async def test_a_principal_gets_its_burst_then_refills():
    clock = Clock()
    limiter = TokenBucketLimiter(per_minute=60, clock=clock)
    assert await _take(limiter, TENANT, ALICE, 60) == [True] * 60
    refused = await limiter.check(TENANT, ALICE, "/v1/x")
    assert not refused.allowed and refused.retry_after == 1
    clock.now += 2  # 60 per minute: one per second
    assert await _take(limiter, TENANT, ALICE, 3) == [True, True, False]


async def test_one_principal_never_uses_anothers_budget():
    limiter = TokenBucketLimiter(per_minute=5, clock=Clock())
    assert await _take(limiter, TENANT, ALICE, 6) == [True] * 5 + [False]
    assert await _take(limiter, TENANT, BOB, 5) == [True] * 5
    assert await _take(limiter, OTHER_TENANT, ALICE, 5) == [True] * 5  # a key is per tenant


async def test_the_tenant_cap_limits_all_its_principals_together():
    limiter = TokenBucketLimiter(per_minute=100, tenant_per_minute=6, clock=Clock())
    assert await _take(limiter, TENANT, ALICE, 4) == [True] * 4
    assert await _take(limiter, TENANT, BOB, 3) == [True, True, False]
    assert await _take(limiter, OTHER_TENANT, BOB, 6) == [True] * 6


async def test_a_refused_request_does_not_spend_the_other_bucket():
    limiter = TokenBucketLimiter(per_minute=2, tenant_per_minute=3, clock=Clock())
    assert await _take(limiter, TENANT, ALICE, 4) == [True, True, False, False]
    assert await _take(limiter, TENANT, BOB, 2) == [True, False]  # tenant had 1 left


async def test_buckets_are_bounded():
    clock = Clock()
    limiter = TokenBucketLimiter(per_minute=1, clock=clock, max_keys=3)
    for _ in range(10):
        await limiter.check(TENANT, uuid.uuid4(), "/v1/x")
    assert limiter.size() <= 3
    assert limiter  # an empty limiter is still a limiter (app.state.rate_limiter or ...)


async def test_the_app_limits_by_default_from_settings(api):
    def app_with(**env):
        values = {"AGENTIC_CP_DATABASE_URL": "postgresql://x@h/db", **env}
        return create_app(Settings.from_env(values))

    assert isinstance(app_with().state.rate_limiter, TokenBucketLimiter)
    assert app_with().state.rate_limiter.per_minute == 1200
    off = app_with(AGENTIC_CP_RATE_LIMIT_PER_MINUTE="0")
    assert isinstance(off.state.rate_limiter, NoLimit)
    tight = api.with_settings(rate_limit_per_minute=2)
    try:
        replies = [await tight.get("/v1/me", tight.a.admin_token) for _ in range(3)]
    finally:
        await tight.http.aclose()
    assert [r.status_code for r in replies] == [200, 200, 429]
    assert replies[2].json()["code"] == "rate.limited"
    assert int(replies[2].headers["Retry-After"]) >= 1
