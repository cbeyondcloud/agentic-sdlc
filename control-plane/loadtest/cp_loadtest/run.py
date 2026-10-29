"""One measured load test: the driver, every observer, and the summary (M3-22)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import asdict

import httpx2

from cp_loadtest.observe import ContainerSampler, DbSampler, HeadWatcher, LoopLag
from cp_loadtest.seed import Population
from cp_loadtest.sim import Driver, Options
from cp_loadtest.stats import Recorder, Targets
from cp_loadtest.wire import make_http


async def measure(
    population: Population,
    bases: Sequence[str],
    options: Options,
    *,
    admin_dsn: str,
    engine: str | None = None,
    containers: Sequence[str] = (),
    targets: Targets = Targets(),  # noqa: B008 - frozen, never mutated
    connections: int = 200,
    metrics_token: str | None = None,
) -> dict:
    """Drive `options.runs` simulated runs spread over `bases` (one per replica) and return
    the summary: per-route latency and statuses, ingest lag, counters, target verdicts,
    database and container samples, and the driver's own loop lag."""
    recorder = Recorder()
    watcher = HeadWatcher(admin_dsn, recorder)
    database = DbSampler(admin_dsn)
    loop_lag = LoopLag()
    sampler = ContainerSampler(engine, list(containers))
    clients = [make_http(base, max_connections=connections) for base in bases]
    scrape: dict = {}
    await watcher.start()
    await database.start()
    await loop_lag.start()
    sampler.start()
    started = time.monotonic()
    try:
        driver = Driver(population, clients, options, recorder, watcher)
        scraping = None
        if metrics_token:
            middle = options.ramp_s + options.duration_s / 2
            scraping = asyncio.create_task(_scrape(clients[0], metrics_token, middle, scrape))
        await driver.run()
        if scraping is not None:
            await asyncio.gather(scraping, return_exceptions=True)
        await asyncio.sleep(2 * watcher.interval_s)  # the watcher's last reads
        await watcher.read_once()
        top = await database.top_statements()
    finally:
        elapsed = time.monotonic() - started
        sampler.stop()
        await loop_lag.stop()
        await database.stop()
        await watcher.stop()
        for client in clients:
            await client.aclose()
    summary = recorder.summary()
    summary["targets"] = targets.verdicts(summary)
    summary["database"] = database.summary() | {"top_statements": top}
    summary["containers"] = sampler.summary()
    summary["driver_loop_lag"] = loop_lag.summary()
    summary["metrics_scrape"] = scrape
    summary["events_never_visible"] = watcher.pending
    summary["elapsed_s"] = round(elapsed, 1)
    summary["options"] = asdict(options)
    summary["replicas"] = len(bases)
    summary["seeding"] = {"role_import_seconds": population.import_seconds}
    return summary


async def _scrape(http: httpx2.AsyncClient, token: str, after_s: float, into: dict) -> None:
    """One Prometheus scrape in the middle of the run (DF-609: the per-tenant refresh)."""
    await asyncio.sleep(after_s)
    started = time.perf_counter()
    response = await http.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    into.update(status=response.status_code, seconds=round(time.perf_counter() - started, 3))
