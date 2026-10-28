"""Prometheus metrics (M2-12; spec §14.3: bounded labels only).

Each app owns a `CollectorRegistry` (never the process-global one), so two apps in one
process - tests, or a second replica in a test - never share counts. What is measured:

- every HTTP request by method, route template and status class, and its duration
  (`MetricsMiddleware`, pure ASGI: it watches the response start and leaves the body, so a
  streamed export still streams);
- ingest: the lag from an event's own time to its receipt, events accepted and replayed,
  and rejections by problem code (codes are constants in our code: a bounded set);
- kill-switch state reads, by whether any switch applied;
- per tenant, read from the database at scrape time but at most every
  `metrics_refresh_seconds`: mail outbox pending, failed and the oldest pending mail's age,
  and the active switches by mode and source (`auto-trip` or `human`).

Labels never carry a run, user, team or token id. Tenants are labelled by slug, and only
the first `metrics_max_tenants` slugs in order: the rest are summed under `_other`.

This is the one module that imports `prometheus_client` (an import contract)."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from agentic_cp.ingest.store import AUDITED, event_time
from agentic_cp.killswitch.store import AUTO_TRIP_ACTOR
from agentic_cp.settings import Settings
from agentic_cp.tables import kill_switches, notifications
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import current_tenant

log = logging.getLogger("agentic_cp.metrics")

#: The route label of a request no route matched (never its raw path).
UNMATCHED = "<unmatched>"
#: The tenant label of every tenant beyond `metrics_max_tenants`.
OTHER = "_other"
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
#: Ingest lag buckets (seconds): a CLI that sends at once, after a retry, after a laptop
#: was offline for an afternoon or a night.
_LAG_BUCKETS = (1, 5, 15, 60, 300, 900, 3600, 4 * 3600, 24 * 3600)


@dataclass
class TenantCounts:
    """One tenant's (or `_other`'s) database gauges."""

    pending: int = 0
    failed: int = 0
    oldest_pending: float = 0.0
    switches: dict[tuple[str, str], int] = field(default_factory=dict)

    def add(self, other: TenantCounts) -> None:
        self.pending += other.pending
        self.failed += other.failed
        self.oldest_pending = max(self.oldest_pending, other.oldest_pending)
        for key, count in other.switches.items():
            self.switches[key] = self.switches.get(key, 0) + count


def fold(counts: Mapping[str, TenantCounts], cap: int) -> dict[str, TenantCounts]:
    """The first `cap` slugs (in order) as they are; the rest summed under `_other`."""
    folded: dict[str, TenantCounts] = {}
    for index, slug in enumerate(sorted(counts)):
        if index < cap:
            folded[slug] = counts[slug]
        else:
            folded.setdefault(OTHER, TenantCounts()).add(counts[slug])
    return folded


def _status_class(status: int) -> str:
    return f"{status // 100}xx" if 100 <= status < 600 else "other"


class Metrics:
    """The app's metrics and its registry."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry = CollectorRegistry(auto_describe=True)
        reg = {"registry": self.registry}
        self.requests = Counter(
            "agentic_cp_http_requests",
            "HTTP requests by method, route template and status class",
            ["method", "route", "status"],
            **reg,
        )
        self.duration = Histogram(
            "agentic_cp_http_request_duration_seconds",
            "HTTP request duration (to the end of the body) by method and route template",
            ["method", "route"],
            **reg,
        )
        self.ingest_lag = Histogram(
            "agentic_cp_ingest_lag_seconds",
            "Time from a run event's own timestamp to its receipt (accepted events only)",
            buckets=_LAG_BUCKETS,
            **reg,
        )
        self.ingest_events = Counter(
            "agentic_cp_ingest_events",
            "Run events received, by result (accepted or duplicate)",
            ["result"],
            **reg,
        )
        self.ingest_rejected = Counter(
            "agentic_cp_ingest_rejected",
            "Ingest batches refused, by problem code",
            ["code"],
            **reg,
        )
        # The codes an alert watches exist at 0 from the start: a child made at its first
        # increment appears as 1, `increase()` sees no rise and the first tampering refusal
        # after a restart would never alert (review F4).
        for code in AUDITED:
            self.ingest_rejected.labels(code).inc(0)
        self.kill_state_reads = Counter(
            "agentic_cp_killswitch_state_reads",
            "Kill-switch state reads by CLIs, by whether any switch applied (active or none)",
            ["result"],
            **reg,
        )
        self.refresh_errors = Counter(
            "agentic_cp_metrics_refresh_errors",
            "Database gauge refreshes that failed (the previous values stay)",
            **reg,
        )
        self.outbox_pending = Gauge(
            "agentic_cp_outbox_pending", "Mails waiting to be sent", ["tenant"], **reg
        )
        self.outbox_failed = Gauge(
            "agentic_cp_outbox_failed", "Mails given up on (status failed)", ["tenant"], **reg
        )
        self.outbox_oldest = Gauge(
            "agentic_cp_outbox_oldest_pending_seconds",
            "Age of the oldest mail still waiting to be sent (0: none)",
            ["tenant"],
            **reg,
        )
        self.switches = Gauge(
            "agentic_cp_killswitches_active",
            "Active kill and pause switches by mode and source (auto-trip or human)",
            ["tenant", "mode", "source"],
            **reg,
        )
        self._refreshed_at: datetime | None = None

    # -- observations from the request path --

    def observe_ingest(self, entries: Iterable[Mapping], accepted: int, duplicates: int, now):
        """`entries` are the batch's newly accepted events (their `at` gives the lag)."""
        if accepted:
            self.ingest_events.labels("accepted").inc(accepted)
        if duplicates:
            self.ingest_events.labels("duplicate").inc(duplicates)
        for entry in entries:
            at = event_time(entry.get("at"))
            if at is not None:
                self.ingest_lag.observe(max(0.0, (now - at).total_seconds()))

    def observe_rejection(self, code: str) -> None:
        self.ingest_rejected.labels(code[:64]).inc()

    def observe_kill_state(self, active: bool) -> None:
        self.kill_state_reads.labels("active" if active else "none").inc()

    # -- database gauges --

    async def refresh(self, engine: AsyncEngine, clock: Callable[[], datetime]) -> None:
        """Re-read the per-tenant gauges unless they were read within the interval."""
        now = clock()
        interval = self.settings.metrics_refresh_seconds
        if self._refreshed_at is not None and (now - self._refreshed_at).total_seconds() < interval:
            return
        self._refreshed_at = now  # a concurrent scrape keeps the values being read
        try:
            counts = await read_tenant_counts(engine, now)
        except (OSError, SQLAlchemyError) as error:
            self._refreshed_at = None
            self.refresh_errors.inc()
            log.warning("metrics: database gauges not refreshed (%s)", type(error).__name__)
            return
        self._publish(fold(counts, self.settings.metrics_max_tenants))

    def _publish(self, counts: Mapping[str, TenantCounts]) -> None:
        for gauge in (self.outbox_pending, self.outbox_failed, self.outbox_oldest, self.switches):
            gauge.clear()
        for slug, tenant in counts.items():
            self.outbox_pending.labels(slug).set(tenant.pending)
            self.outbox_failed.labels(slug).set(tenant.failed)
            self.outbox_oldest.labels(slug).set(tenant.oldest_pending)
            for (mode, source), count in tenant.switches.items():
                self.switches.labels(slug, mode, source).set(count)

    def exposition(self) -> tuple[bytes, str]:
        """The text format and its content type."""
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


async def _tenant_ids(engine: AsyncEngine) -> list[UUID]:
    async with engine.begin() as conn:  # no tenant: the definer function only
        return [row[0] for row in await conn.execute(select(func.active_tenant_ids()))]


async def read_tenant_counts(engine: AsyncEngine, now: datetime) -> dict[str, TenantCounts]:
    """Each active tenant's gauges by slug, each read inside that tenant's context."""
    found: dict[str, TenantCounts] = {}
    for tenant_id in await _tenant_ids(engine):
        async with tenant_tx(engine, tenant_id) as conn:
            slug = (await current_tenant(conn)).slug
            counts = TenantCounts()
            mails = await conn.execute(
                select(
                    notifications.c.status,
                    func.count(),
                    func.min(notifications.c.created_at),
                )
                .where(notifications.c.status.in_(("pending", "failed")))
                .group_by(notifications.c.status)
            )
            for status, count, oldest in mails:
                if status == "pending":
                    counts.pending = count
                    counts.oldest_pending = max(0.0, (now - oldest).total_seconds())
                else:
                    counts.failed = count
            source = (kill_switches.c.created_by == AUTO_TRIP_ACTOR).label("auto")
            switches = await conn.execute(
                select(kill_switches.c.mode, source, func.count())
                .where(kill_switches.c.cleared_at.is_(None))
                .group_by(kill_switches.c.mode, source)
            )
            tally: dict[tuple[str, str], int] = defaultdict(int)
            for mode, auto, count in switches:
                tally[(mode, "auto-trip" if auto else "human")] += count
            counts.switches = dict(tally)
            found[slug] = counts
    return found


class MetricsMiddleware:
    """Counts every HTTP request by method, route template and status class. The route
    template is read after the app ran (the router records the matched route in the
    scope); a request no route matched is `<unmatched>`."""

    def __init__(self, app, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 500
        started = time.perf_counter()

        async def watch(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, watch)
        finally:
            route = getattr(scope.get("route"), "path", None) or UNMATCHED
            method = scope.get("method", "")
            method = method if method in _METHODS else "other"
            self.metrics.requests.labels(method, route, _status_class(status)).inc()
            self.metrics.duration.labels(method, route).observe(time.perf_counter() - started)
