"""The load harness end to end, small (M3-22): 20 simulated runs for 30 s against the app
served by uvicorn on 127.0.0.1, on the test database. It keeps the harness working; the
thresholds are generous, the real targets are for the 1000-run test (see
`docs/load-test/M3-22-report.md`). Skips without Postgres, as every database test."""

import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from apiclient import settings_for
from cp_loadtest.profile import Timing
from cp_loadtest.run import measure
from cp_loadtest.seed import remove_tree, seed
from cp_loadtest.sim import Options

from agentic_cp.app import create_app, utc_now

pytestmark = pytest.mark.anyio

SMOKE = Options(
    runs=20,
    duration_s=30,
    ramp_s=5,
    timing=Timing(step_s=1, gate_s=1, heartbeat_s=3, kill_poll_s=2, jitter=0.3),
    drain_s=15,
)


class LiveServer:
    """The app under uvicorn in a thread with its own event loop (and so its own engine)."""

    def __init__(self, app) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 60
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError("the control plane did not start")
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=60)


async def test_smoke_20_runs_30_s(db):
    workdir = Path(tempfile.mkdtemp(prefix="cp-loadtest-smoke-"))
    settings = settings_for(db)
    try:
        population = await seed(
            db.owner,
            db.app,
            settings,
            tenants=2,
            teams=2,
            users=SMOKE.runs,
            workdir=workdir,
            clock=utc_now,
        )
        server = LiveServer(create_app(settings))
        base = server.start()
        try:
            summary = await measure(population, [base], SMOKE, admin_dsn=db.admin_dsn)
        finally:
            server.stop()
    finally:
        remove_tree(workdir)
    assert summary["error_rate"] <= 0.01, summary["errors_by_status"]
    counters = summary["counters"]
    assert counters["runs_registered"] >= SMOKE.runs, counters
    assert counters.get("runs_closed", 0) >= 1, counters
    assert counters.get("events_undelivered", 0) == 0, counters
    assert counters["events_committed"] == counters["events_produced"], counters
    lag = summary["ingest_lag_visible_s"]
    assert lag["count"] == counters["events_produced"] and lag["p95"] <= 10, lag
    for route in ("POST /v1/runs", "POST /v1/runs/{run_id}/heartbeat", "GET /v1/killswitch/state"):
        assert summary["routes"][route]["count"] > 0, route
    assert set(summary["targets"]) >= {"ingest_lag_p95_s", "error_rate"}
    assert summary["database"]["samples"] >= 1
    assert summary["driver_loop_lag"]["p99_ms"] is not None
