"""Seeding, one simulated run, the sink's catch-up and the head watcher (M3-22), against the
app in process on a test database (they skip without Postgres, as every database test)."""

import asyncio
import random
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from apiclient import FakeClock, settings_for
from cp_loadtest.chain import RunChain
from cp_loadtest.observe import HeadWatcher
from cp_loadtest.profile import Timing, load_template
from cp_loadtest.seed import registration, remove_tree, seed
from cp_loadtest.sim import Driver, Options, Sink
from cp_loadtest.stats import Recorder
from cp_loadtest.wire import Wire, make_http
from sqlalchemy import select

from agentic_cp.app import create_app
from agentic_cp.tables import run_events, runs
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

FAST = Timing(step_s=0.01, gate_s=0.01, heartbeat_s=0.05, kill_poll_s=0.05, jitter=0.0)


@pytest.fixture
def workdir():
    path = Path(tempfile.mkdtemp(prefix="cp-loadtest-"))
    yield path
    remove_tree(path)


async def _seeded(db, workdir, *, tenants=1, teams=1, users=2):
    settings = settings_for(db)
    population = await seed(
        db.owner,
        db.app,
        settings,
        tenants=tenants,
        teams=teams,
        users=users,
        workdir=workdir,
        clock=FakeClock(),
    )
    app = create_app(settings, engine=db.app)
    return population, app


def _http(app) -> httpx2.AsyncClient:
    return make_http("http://cp.test", transport=httpx2.ASGITransport(app=app))


async def test_seed_makes_runnable_users(db, workdir):
    population, app = await _seeded(db, workdir, tenants=2, teams=2, users=8)
    assert len(population.users) == 8
    assert {u.tenant for u in population.users} == {"lt-01", "lt-02"}
    assert {u.team for u in population.users} == {"team-01", "team-02"}
    assert population.import_seconds and all(s >= 0 for s in population.import_seconds)
    async with _http(app) as http:
        for user in population.users[:3]:
            wire = Wire(http, user.token, Recorder())
            me = await wire.me()
            assert me.user.email == user.email
            body = registration(population, user, f"lt-dry-{user.email[:6]}") | {"dry_run": True}
            preflight = await wire.register(body)
            failed = [c for c in preflight.checks if not c.passed]
            assert failed == [] and not preflight.registered


async def test_one_run_reports_every_event(db, workdir):
    population, app = await _seeded(db, workdir, users=1)
    recorder = Recorder()
    watcher = HeadWatcher(db.admin_dsn, recorder, interval_s=0.05)
    options = Options(runs=1, duration_s=60, ramp_s=0, timing=FAST, max_runs=1, drain_s=10)
    async with _http(app) as http:
        await watcher.start()
        try:
            await Driver(population, [http], options, recorder, watcher).run()
        finally:
            await watcher.stop()
    summary = recorder.summary()
    assert summary["errors"] == 0, summary
    counters = summary["counters"]
    assert counters["runs_registered"] == counters["runs_closed"] == 1
    assert counters["events_produced"] == counters["events_committed"] == 40
    assert summary["ingest_lag_commit_s"]["count"] == 40
    assert summary["ingest_lag_visible_s"]["count"] == 40
    assert summary["routes"]["POST /v1/runs/{run_id}/heartbeat"]["count"] >= 5  # 1 + gates
    tenant = population.tenant_ids["lt-01"]
    async with tenant_tx(db.owner, tenant) as conn:
        row = (await conn.execute(select(runs))).one()
        stored = (await conn.execute(select(run_events.c.seq))).scalars().all()
    assert row.status == "completed" and row.head_seq == 40 and sorted(stored) == list(range(1, 41))


async def test_sink_catches_up_after_gap(db, workdir):
    population, app = await _seeded(db, workdir, users=1)
    user = population.users[0]
    recorder = Recorder()
    async with _http(app) as http:
        wire = Wire(http, user.token, recorder)
        run_id = "lt-gap-0001"
        assert (await wire.register(registration(population, user, run_id))).registered
        chain = RunChain(
            load_template(),
            run_id=run_id,
            team=user.team,
            policy_version=population.policy_version[user.tenant],
        )
        sink = Sink(wire, run_id, recorder, watcher=None)
        for _ in range(5):
            sink.produced(chain.next(_utc()), time.monotonic())
        sink.cursor = 3  # as if a copied cursor said the server holds 3 events: it holds none
        await sink.deliver()
    summary = recorder.summary()
    assert summary["routes"]["POST /v1/runs/{run_id}/events"]["statuses"]["409"] == 1
    assert recorder.counters["events_committed"] == 5 and sink.cursor == 5
    assert summary["ingest_lag_commit_s"]["count"] == 5


async def test_head_watcher_marks_visibility(db, workdir):
    population, app = await _seeded(db, workdir, users=1)
    user = population.users[0]
    recorder = Recorder()
    watcher = HeadWatcher(db.admin_dsn, recorder, interval_s=0.05)
    await watcher.start()
    try:
        async with _http(app) as http:
            wire = Wire(http, user.token, recorder)
            run_id = "lt-watch-0001"
            await wire.register(registration(population, user, run_id))
            chain = RunChain(
                load_template(),
                run_id=run_id,
                team=user.team,
                policy_version=population.policy_version[user.tenant],
            )
            entries = [chain.next(_utc()) for _ in range(3)]
            produced = time.monotonic()
            for entry in entries:
                watcher.expect(run_id, entry["seq"], produced)
            await asyncio.sleep(0.2)
            assert recorder.visible_lag == []  # nothing is visible before it is sent
            await wire.ingest(run_id, entries)
            for _ in range(100):
                if len(recorder.visible_lag) == 3:
                    break
                await asyncio.sleep(0.05)
    finally:
        await watcher.stop()
    assert len(recorder.visible_lag) == 3 and all(lag >= 0.2 for lag in recorder.visible_lag)
    assert watcher.pending == 0


def _utc() -> datetime:
    return datetime.now(UTC)


def test_ramp_spreads_the_first_starts():
    offsets = Options(runs=50, duration_s=1, ramp_s=20, timing=FAST).start_offsets(random.Random(3))
    assert len(offsets) == 50 and all(0 <= o <= 20 for o in offsets)
    assert max(offsets) - min(offsets) > 10  # spread, not a thundering herd
    assert Options(runs=3, duration_s=1, ramp_s=0, timing=FAST).start_offsets(random.Random(3)) == [
        0,
        0,
        0,
    ]
