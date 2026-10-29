"""What the load test watches besides its own requests (M3-22).

* `HeadWatcher` - a separate database connection (a superuser: it reads every tenant's
  runs past row-level security) reads the run heads every `interval_s`. The first read
  that shows a run's `head_seq` at or past an event's seq is the moment the event was
  committed and visible to any reader (the dashboards read the same rows); the lag from
  the simulated CLI producing it is `Recorder.visible_lag`. Its resolution is the interval.
* `DbSampler` - Postgres connections by state, sessions waiting on a lock, commits/s.
* `ContainerSampler` - CPU and memory of the stack's containers (`docker stats` or
  `podman stats`, on a thread, as the CLI call blocks for about two seconds).
* `LoopLag` - how late the driver's own event loop wakes up: a busy driver would inflate
  every latency it measures, so the report shows it was not the bottleneck."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import threading
import time
from collections import defaultdict

import asyncpg

from cp_loadtest.stats import Recorder, percentile

_SCHEMA = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_UNITS = {"b": 1 / 2**20, "kib": 1 / 1024, "mib": 1, "gib": 1024, "kb": 1e3 / 2**20}
_UNITS |= {"mb": 1e6 / 2**20, "gb": 1e9 / 2**20}


def _dsn(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


class HeadWatcher:
    def __init__(
        self, admin_dsn: str, recorder: Recorder, *, interval_s: float = 0.25, schema="agentic"
    ) -> None:
        if not _SCHEMA.fullmatch(schema):
            raise ValueError(f"unsafe schema name {schema!r}")
        self._dsn = _dsn(admin_dsn)
        self._query = f"select run_id, head_seq from {schema}.runs where run_id = any($1::text[])"
        self.recorder = recorder
        self.interval_s = interval_s
        self._expected: dict[str, list[tuple[int, float]]] = defaultdict(list)
        self._task: asyncio.Task | None = None
        self._conn: asyncpg.Connection | None = None

    @property
    def pending(self) -> int:
        return sum(len(items) for items in self._expected.values())

    def expect(self, run_id: str, seq: int, produced: float) -> None:
        """Event `seq` of `run_id` was produced at `produced` (time.monotonic)."""
        self._expected[run_id].append((seq, produced))

    async def start(self) -> None:
        self._conn = await asyncpg.connect(self._dsn)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop reading, after one last read of what is still expected."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._conn is not None:
            if self._expected:
                await self.read_once()
            await self._conn.close()

    async def _loop(self) -> None:
        while True:
            if self._expected:
                await self.read_once()
            await asyncio.sleep(self.interval_s)

    async def read_once(self) -> None:
        rows = await self._conn.fetch(self._query, list(self._expected))
        seen = time.monotonic()
        for run_id, head in rows:
            items = self._expected.get(run_id)
            if not items:
                continue
            visible = [produced for seq, produced in items if seq <= head]
            for produced in visible:
                self.recorder.visible_lag.append(seen - produced)
            self.recorder.count("events_visible", len(visible))
            left = [(seq, produced) for seq, produced in items if seq > head]
            if left:
                self._expected[run_id] = left
            else:
                del self._expected[run_id]


class DbSampler:
    QUERY = """
        select count(*) as total,
               count(*) filter (where state = 'active') as active,
               count(*) filter (where state = 'idle') as idle,
               count(*) filter (where state like 'idle in transaction%') as idle_in_tx,
               count(*) filter (where wait_event_type = 'Lock') as lock_waits,
               (select xact_commit from pg_stat_database where datname = current_database())
                   as commits,
               (select sessions from pg_stat_database where datname = current_database())
                   as sessions
        from pg_stat_activity
        where datname = current_database() and backend_type = 'client backend'
    """

    TOP = """
        select calls, round(total_exec_time)::bigint as total_ms,
               round(mean_exec_time::numeric, 2)::float as mean_ms, rows,
               left(regexp_replace(query, '\\s+', ' ', 'g'), 200) as query
        from pg_stat_statements
        where dbid = (select oid from pg_database where datname = current_database())
        order by total_exec_time desc
        limit $1
    """

    def __init__(self, admin_dsn: str, *, interval_s: float = 5.0) -> None:
        self._dsn = _dsn(admin_dsn)
        self.interval_s = interval_s
        self.samples: list[dict] = []
        self.statements = False  # pg_stat_statements is preloaded (the stack's Postgres)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        conn = await asyncpg.connect(self._dsn)
        try:
            preloaded = await conn.fetchval("select current_setting('shared_preload_libraries')")
            if "pg_stat_statements" in (preloaded or ""):
                await conn.execute("create extension if not exists pg_stat_statements")
                await conn.execute("select pg_stat_statements_reset()")
                self.statements = True
        finally:
            await conn.close()
        self._task = asyncio.create_task(self._loop())

    async def top_statements(self, limit: int = 15) -> list[dict]:
        """The statements that took the most database time since `start` ([] without the
        extension)."""
        if not self.statements:
            return []
        conn = await asyncpg.connect(self._dsn)
        try:
            return [dict(row) for row in await conn.fetch(self.TOP, limit)]
        finally:
            await conn.close()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        conn = await asyncpg.connect(self._dsn)
        try:
            while True:
                row = await conn.fetchrow(self.QUERY)
                self.samples.append({"t": time.monotonic(), **dict(row)})
                await asyncio.sleep(self.interval_s)
        finally:
            await conn.close()

    def summary(self) -> dict:
        if not self.samples:
            return {}
        out = {}
        for name in ("total", "active", "idle_in_tx", "lock_waits"):
            values = [s[name] for s in self.samples]
            out[f"connections_{name}_max" if name != "lock_waits" else "lock_waits_max"] = max(
                values
            )
            if name in ("total", "active"):
                out[f"connections_{name}_avg"] = round(sum(values) / len(values), 1)
        first, last = self.samples[0], self.samples[-1]
        if last["t"] > first["t"]:
            out["commits_per_s"] = round(
                (last["commits"] - first["commits"]) / (last["t"] - first["t"]), 1
            )
        out["sessions_opened"] = last["sessions"] - first["sessions"]  # new logins, all roles
        out["samples"] = len(self.samples)
        return out


def memory_mib(text: str) -> float | None:
    """`123.4MiB` (the used part of `docker stats`' MemUsage) in MiB."""
    match = re.fullmatch(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*", text)
    if not match or match.group(2).lower() not in _UNITS:
        return None
    return round(float(match.group(1)) * _UNITS[match.group(2).lower()], 1)


class ContainerSampler:
    def __init__(self, engine: str | None, names: list[str], *, interval_s: float = 5.0):
        self.engine = engine
        self.names = names
        self.interval_s = interval_s
        self.samples: dict[str, list[tuple[float, float | None]]] = defaultdict(list)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        if self.engine and self.names:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=30)

    def _loop(self) -> None:
        command = [self.engine, "stats", "--no-stream", "--format", "{{json .}}", *self.names]
        while not self._stop.is_set():
            done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            for line in done.stdout.splitlines():
                self.record(line)
            self._stop.wait(self.interval_s)

    def record(self, line: str) -> None:
        try:
            data = json.loads(line)
        except ValueError:
            return
        name = str(data.get("Name") or data.get("name") or "")
        cpu = str(data.get("CPUPerc") or data.get("CPU") or "").rstrip("%")
        used = str(data.get("MemUsage") or "").split("/")[0]
        try:
            self.samples[name].append((float(cpu), memory_mib(used)))
        except ValueError:
            return

    def summary(self) -> dict:
        out = {}
        for name, values in sorted(self.samples.items()):
            cpu = [c for c, _ in values]
            mem = [m for _, m in values if m is not None]
            out[name] = {
                "cpu_pct_avg": round(sum(cpu) / len(cpu), 1),
                "cpu_pct_p95": percentile(cpu, 95),
                "cpu_pct_max": max(cpu),
                "mem_mib_max": max(mem) if mem else None,
                "samples": len(values),
            }
        return out


class LoopLag:
    def __init__(self, *, interval_s: float = 0.1) -> None:
        self.interval_s = interval_s
        self.late: list[float] = []
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            before = time.perf_counter()
            await asyncio.sleep(self.interval_s)
            self.late.append(max(0.0, time.perf_counter() - before - self.interval_s))

    def summary(self) -> dict:
        def ms(value):
            return None if value is None else round(value * 1000, 1)

        return {
            "p50_ms": ms(percentile(self.late, 50)),
            "p99_ms": ms(percentile(self.late, 99)),
            "max_ms": ms(max(self.late) if self.late else None),
        }
