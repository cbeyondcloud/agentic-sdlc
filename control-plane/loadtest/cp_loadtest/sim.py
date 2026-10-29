"""Simulated runs: what N CLIs driving feature-delivery runs send the control plane (M3-22).

Each slot is one person with one run open at a time. A run does what `agentic run` does in
org mode, in order:

  * on the person's first run, `agentic init`: `/me`, `/policy/active` and the bundle
    (digest checked); on every run, `follow_active_policy`'s `/policy/active`;
  * registration (`POST /runs`, every org check); a refused run waits and tries again;
  * while the CLI process runs: a heartbeat at once and every `heartbeat_s`
    (`agentic_cli.heartbeat`), and a kill-switch poll at once and every `kill_poll_s`
    with the last answer's ETag (`agentic_cli.killswitch`);
  * the template's events at the planned times, each handed to a `Sink` - the background
    delivery of `ForwardingRecorder(background=True)` over `ControlPlaneSink`;
  * at a gate the process ends: the beat and poll loops stop and one `waiting` beat is
    sent; a person's decision starts a new process (new loops, no cached ETag).

A closed run is followed by the slot's next run, so `runs` stay open until the end. At the
end nothing new is produced and each sink gets `drain_s` to deliver what it holds."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx2

from agentic.telemetry.sinks import BACKOFF_MAX_S, BACKOFF_MIN_S
from agentic_cli.control_plane import ControlPlaneSink
from cp_loadtest.chain import RunChain
from cp_loadtest.observe import HeadWatcher
from cp_loadtest.profile import Timing, load_template, plan_run
from cp_loadtest.seed import PIPELINE, Population, SimUser, registration
from cp_loadtest.stats import Recorder
from cp_loadtest.wire import Refused, Wire, WireError

#: The CLI sink's batching (at most 200 entries and about 512 KiB per request), reused as is.
_CHUNKER = ControlPlaneSink(None)  # type: ignore[arg-type]  # only its _chunk is used


@dataclass(frozen=True)
class Options:
    runs: int
    duration_s: float
    ramp_s: float
    timing: Timing
    seed: int = 1
    drain_s: float = 30.0
    max_runs: int | None = None  # stop starting runs after this many (tests)
    retry_s: float = 5.0  # a slot whose run could not start waits this long

    def start_offsets(self, rng: random.Random) -> list[float]:
        """When each slot starts its first run: an even ramp over `ramp_s`, jittered."""
        if self.ramp_s <= 0:
            return [0] * self.runs
        step = self.ramp_s / self.runs
        return [min(self.ramp_s, (n + rng.random()) * step) for n in range(self.runs)]


class Sink:
    """One run's background delivery: everything after the cursor, in the CLI sink's chunks,
    at least once and in order; a gap behind the batch catches up from the server's head
    (R21); a failure waits a backoff (1 s doubling to 60 s) and tries again."""

    def __init__(
        self, wire: Wire, run_id: str, recorder: Recorder, watcher: HeadWatcher | None
    ) -> None:
        self.wire = wire
        self.run_id = run_id
        self.recorder = recorder
        self.watcher = watcher
        self.entries: list[dict] = []
        self.produced_at: list[float] = []
        self.cursor = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def produced(self, entry: dict, at: float) -> None:
        self.entries.append(entry)
        self.produced_at.append(at)
        if self.watcher is not None:
            self.watcher.expect(self.run_id, entry["seq"], at)
        self._wake.set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        backoff = 0.0
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self.cursor < len(self.entries):
                if await self.deliver():
                    backoff = 0.0
                else:
                    backoff = min(max(backoff * 2, BACKOFF_MIN_S), BACKOFF_MAX_S)
                    await asyncio.sleep(backoff)

    async def deliver(self) -> bool:
        """Send what is pending; True when the server holds all of it."""
        while self.cursor < len(self.entries):
            chunk = _CHUNKER._chunk(self.entries[self.cursor :], 0, _CHUNKER.batch)
            try:
                result = await self.wire.ingest(self.run_id, chunk)
            except Refused as error:
                first = chunk[0]["seq"]
                if error.code == "ingest.gap" and error.head and error.head[0] < first - 1:
                    self.cursor = error.head[0]
                    continue
                self.recorder.count(f"ingest_refused_{error.code or error.status}")
                return False
            except WireError:
                return False
            done = time.monotonic()
            fresh_from = self.cursor + len(chunk) - result.accepted
            for n in range(fresh_from, self.cursor + len(chunk)):
                self.recorder.commit_lag.append(done - self.produced_at[n])
            self.recorder.count("events_committed", result.accepted)
            self.cursor += len(chunk)
        return True

    async def close(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while self.cursor < len(self.entries) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.recorder.count("events_undelivered", len(self.entries) - self.cursor)


class _Process:
    """The beat and kill-poll loops of one CLI process driving the run."""

    def __init__(self, wire: Wire, run_id: str, user: SimUser, timing: Timing) -> None:
        self.wire, self.run_id, self.user, self.timing = wire, run_id, user, timing
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._beats()), asyncio.create_task(self._polls())]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def beat(self, state: str) -> None:
        now = datetime.now(UTC)
        try:
            await self.wire.heartbeat(self.run_id, beat_at=now, sent_at=now, state=state)
        except WireError:
            pass  # logged once by the CLI and tried at the next beat

    async def _beats(self) -> None:
        while True:
            await self.beat("running")
            await asyncio.sleep(self.timing.heartbeat_s)

    async def _polls(self) -> None:
        etag = None
        repo = registration_repo(self.user)
        while True:
            try:
                _, etag = await self.wire.kill_state(
                    team=self.user.team, repo=repo, pipeline=PIPELINE, etag=etag
                )
            except WireError:
                pass  # the CLI keeps its last answer for up to 60 s
            await asyncio.sleep(self.timing.kill_poll_s)


def registration_repo(user: SimUser) -> str:
    return f"git.example.test/{user.team}/service"


class Driver:
    def __init__(
        self,
        population: Population,
        clients: Sequence[httpx2.AsyncClient],
        options: Options,
        recorder: Recorder,
        watcher: HeadWatcher | None = None,
        *,
        template: list[dict] | None = None,
    ) -> None:
        self.population = population
        self.clients = list(clients)
        self.options = options
        self.recorder = recorder
        self.watcher = watcher
        self.template = template or load_template()
        self.started = 0
        self.end = 0.0

    async def run(self) -> None:
        options = self.options
        if len(self.population.users) < options.runs:
            raise ValueError("every slot needs its own person: seed at least `runs` users")
        rng = random.Random(options.seed)
        self.end = time.monotonic() + options.ramp_s + options.duration_s
        offsets = options.start_offsets(rng)
        await asyncio.gather(*(self._slot(n, offsets[n]) for n in range(options.runs)))

    async def _slot(self, n: int, offset: float) -> None:
        user = self.population.users[n]
        http = self.clients[n % len(self.clients)]
        rng = random.Random(self.options.seed * 1_000_003 + n)
        await asyncio.sleep(offset)
        count = 0
        limit = self.options.max_runs
        while time.monotonic() < self.end and (limit is None or self.started < limit):
            self.started += 1
            await self._run(n, count, user, http, rng)
            count += 1

    async def _wait(self, seconds: float) -> bool:
        """Sleep `seconds`; False (after sleeping to the end) when the test ends first."""
        left = self.end - time.monotonic()
        if seconds > left:
            await asyncio.sleep(max(0.0, left))
            return False
        await asyncio.sleep(seconds)
        return True

    async def _run(
        self, slot: int, count: int, user: SimUser, http: httpx2.AsyncClient, rng
    ) -> None:
        recorder, timing = self.recorder, self.options.timing
        wire = Wire(http, user.token, recorder)
        run_id = f"lt-{slot:04d}-{count:03d}-{rng.getrandbits(32):08x}"
        try:
            if count == 0:
                await wire.me()
                await wire.bundle(await wire.active_policy())
            else:
                await wire.active_policy()
            preflight = await wire.register(registration(self.population, user, run_id))
        except WireError:
            recorder.count("runs_start_failed")
            await self._wait(self.options.retry_s)
            return
        if not preflight.registered:
            recorder.count("runs_refused")
            for check in preflight.checks:
                if not check.passed:
                    recorder.count(f"refused_{check.check}")
            await self._wait(self.options.retry_s)
            return
        recorder.count("runs_registered")
        chain = RunChain(
            self.template,
            run_id=run_id,
            team=user.team,
            policy_version=self.population.policy_version[user.tenant],
        )
        sink = Sink(wire, run_id, recorder, self.watcher)
        sink.start()
        process = _Process(wire, run_id, user, timing)
        process.start()
        try:
            for action in plan_run(self.template, timing, rng):
                if not await self._wait(action.delay_s):
                    break
                if action.kind == "event":
                    sink.produced(chain.next(datetime.now(UTC)), time.monotonic())
                    recorder.count("events_produced")
                elif action.kind == "gate":
                    await process.stop()
                    await process.beat("waiting")
                else:
                    process.start()
        finally:
            await process.stop()
            await sink.close(self.options.drain_s)
        if chain.done:
            recorder.count("runs_closed")
