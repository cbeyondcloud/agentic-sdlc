"""The background worker (`python -m agentic_cp worker`): one leader at a time, looping over
the active tenants.

Leadership is a session advisory lock held on one connection for the worker's life; a
second worker waits. Every pass asks `active_tenant_ids()` (a registered definer function:
the worker never reads another tenant's rows) and syncs each tenant inside its own
context. One tenant's failure is logged with its slug and never stops the others."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from agentic.orgapi import SyncOutcome
from agentic_cp.alerts.autotrip import auto_trip_job
from agentic_cp.gitops import GitRunner
from agentic_cp.keys.store import retry_job
from agentic_cp.learning.digest import digest_job
from agentic_cp.notify.delivery import deliver_job
from agentic_cp.policy_sync.service import sync_once
from agentic_cp.rbac.repos import refresh_due
from agentic_cp.routing.apply import routing_job
from agentic_cp.settings import Settings
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import current_tenant

log = logging.getLogger("agentic_cp.worker")

#: Per-tenant jobs run after the policy sync: the CODEOWNERS refresh (M2-03), auto-trip
#: (M2-11; before the outbox, so its mail goes out in the same pass), the learning digest
#: (M3-15; once a day per person, also before the outbox), the mail outbox (M2-07), pending
#: proxy-key revocations (M2-17) and the gateway's routing (M2-09; after the sync, so a
#: version accepted in this pass is applied in it).
TenantJob = Callable[[AsyncEngine, GitRunner, UUID, Settings, Callable[[], datetime]], Awaitable]
EXTRA_JOBS: list[TenantJob] = [
    refresh_due,
    auto_trip_job,
    digest_job,
    deliver_job,
    retry_job,
    routing_job,
]
LEADER_KEY = "agentic.worker"


async def active_tenants(engine: AsyncEngine) -> list[UUID]:
    async with engine.begin() as conn:  # no tenant: the definer function only
        return [row[0] for row in await conn.execute(select(func.active_tenant_ids()))]


async def _slug(engine: AsyncEngine, tenant_id: UUID) -> str:
    async with tenant_tx(engine, tenant_id) as conn:
        return (await current_tenant(conn)).slug


async def run_pass(
    engine: AsyncEngine, git: GitRunner, settings: Settings, clock: Callable[[], datetime]
) -> dict[str, SyncOutcome | str]:
    """Sync every active tenant once; returns each tenant's outcome (or error) by slug."""
    results: dict[str, SyncOutcome | str] = {}
    for tenant_id in await active_tenants(engine):
        slug = str(tenant_id)
        try:
            slug = await _slug(engine, tenant_id)
            outcome = await sync_once(engine, git, tenant_id, settings, clock)
            if outcome.kind == "failed":
                log.warning("tenant %s: policy sync failed: %s", slug, "; ".join(outcome.problems))
            results[slug] = outcome
            for job in EXTRA_JOBS:
                await job(engine, git, tenant_id, settings, clock)
        except Exception as error:  # one tenant never stops the others
            log.warning("tenant %s: worker pass failed (%s)", slug, type(error).__name__)
            log.debug("tenant %s: worker pass failure", slug, exc_info=error)
            results[slug] = type(error).__name__
    return results


async def run_forever(
    engine: AsyncEngine,
    git: GitRunner,
    settings: Settings,
    clock: Callable[[], datetime],
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """Lead (or wait to lead), then run a pass every `policy_poll_seconds` until `stop`."""
    stop = stop or asyncio.Event()
    async with engine.connect() as leader:
        key = func.hashtextextended(LEADER_KEY, 0)
        while not stop.is_set():
            if (await leader.execute(select(func.pg_try_advisory_lock(key)))).scalar():
                await leader.commit()
                break
            await leader.commit()
            log.info("another worker leads; waiting")
            await _sleep(stop, settings.policy_poll_seconds)
        try:
            while not stop.is_set():
                await run_pass(engine, git, settings, clock)
                await _sleep(stop, settings.policy_poll_seconds)
        finally:
            await leader.execute(select(func.pg_advisory_unlock(key)))
            await leader.commit()


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
