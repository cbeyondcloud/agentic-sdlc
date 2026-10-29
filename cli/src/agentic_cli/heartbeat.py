"""The run heartbeat (M3-07): proof that a CLI is still driving a run.

While a command drives a run (`run`, `resume`, a gate decision), `Heartbeat` runs a daemon
thread that beats at once and then every `run_health.heartbeat_seconds` of the policy:

  * locally, it rewrites `<run folder>/heartbeat.json` (a temporary file, then
    `os.replace`), so `agentic logs` on this machine can tell a run whose process died
    from one still going (`local_state`, DF-278);
  * in org mode with a control plane, it posts the beat (`ControlPlaneClient.heartbeat`)
    on its own client, so the control plane can release a dead run's concurrency slot.

Nothing here can block or fail the run: the engine never waits on the thread, a post has a
short timeout, and an error - an unreachable control plane, a refused beat - is logged
once until it changes and tried again at the next beat. Beats are not queued: a beat is a
sign of life now, and the next one is always newer. `finish` sends one `waiting` beat when
the command ends with the run waiting for a person or a pause switch, so the control plane
knows it is paused, not dead."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

HEARTBEAT_FILE = "heartbeat.json"
#: Run statuses that mean "waiting for a person or a pause switch".
WAITING = frozenset({"awaiting_gate", "paused"})
#: While the run's folder does not exist yet (the engine creates it), look again this soon.
FOLDER_WAIT_S = 1.0
#: How long leaving the context waits for a beat in flight (a post times out in 5 s).
JOIN_S = 6.0
#: A local beat older than this many intervals (plus a margin) is stale.
STALE_BEATS, STALE_MARGIN_S = 3, 5
_REPLACE_TRIES, _REPLACE_WAIT_S = 5, 0.05
_LOG = logging.getLogger("agentic.heartbeat")


class Poster(Protocol):
    def heartbeat(
        self, run_id: str, *, beat_at: datetime, sent_at: datetime, state: str
    ) -> object: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Heartbeat:
    """The heartbeat of one run around one engine call (a context manager)."""

    def __init__(
        self,
        run_dir: Path,
        run_id: str,
        *,
        interval_s: float,
        poster: Poster | None = None,
        wall: Callable[[], datetime] = _utc_now,
        join_s: float = JOIN_S,
    ) -> None:
        self.run_dir, self.run_id = Path(run_dir), run_id
        self.interval_s, self.poster, self.wall, self.join_s = interval_s, poster, wall, join_s
        self.thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reported: str | None = None  # the last failure logged: logged again on change
        self._ended: str | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def __enter__(self) -> Heartbeat:
        self._stop.clear()
        self.thread = threading.Thread(target=self._loop, name="agentic-heartbeat", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=self.join_s)  # a post in flight may finish first
        if self._ended is not None:  # after the thread: no running beat lands after it
            self.finish(self._ended)
        close = getattr(self.poster, "close", None)
        if close is not None and not self.running:  # never under a post still in flight
            close()

    def ended(self, status: str) -> None:
        """The run's status when the engine call returned: leaving the context then sends
        the `waiting` beat (`finish`) once the thread has stopped."""
        self._ended = status

    def finish(self, status: str) -> None:
        """After the engine call: one `waiting` beat when the run stopped to wait."""
        if status in WAITING:
            self.beat("waiting")

    def beat(self, state: str = "running") -> bool:
        """One beat now; True when the run's folder exists and the beat file was written."""
        taken = self.wall()
        written = self._write(taken, state)
        if self.poster is not None:
            try:
                self.poster.heartbeat(self.run_id, beat_at=taken, sent_at=self.wall(), state=state)
            except Exception as error:  # never stops the run: the next beat tries again
                self._report(f"run {self.run_id}: heartbeat not sent: {error}")
            else:
                if self._reported is not None:
                    _LOG.info("run %s: heartbeats reach the control plane again", self.run_id)
                    self._reported = None
        return written

    def _loop(self) -> None:
        while not self._stop.is_set():
            written = self.beat()
            self._stop.wait(self.interval_s if written else FOLDER_WAIT_S)

    def _write(self, taken: datetime, state: str) -> bool:
        if not self.run_dir.is_dir():  # the engine creates it; a beat never does
            return False
        record = {
            "run_id": self.run_id,
            "pid": os.getpid(),
            "at": taken.isoformat(),
            "state": state,
            "interval_s": self.interval_s,
            "stale_after_s": STALE_BEATS * self.interval_s + STALE_MARGIN_S,
        }
        target = self.run_dir / HEARTBEAT_FILE
        temporary = target.with_name(f"{HEARTBEAT_FILE}.{os.getpid()}.tmp")
        try:
            temporary.write_bytes((json.dumps(record) + "\n").encode("utf-8"))
            _replace(temporary, target)
        except OSError as error:  # a reader holding it on Windows: the next beat writes it
            temporary.unlink(missing_ok=True)
            self._report(f"run {self.run_id}: {target} not written: {error}")
            return True
        return True

    def _report(self, message: str) -> None:
        if message != self._reported:
            self._reported = message
            _LOG.warning("%s", message)


def _replace(source: Path, target: Path) -> None:
    """`os.replace`, retried briefly while a reader has `target` open (Windows)."""
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _REPLACE_TRIES - 1:
                raise
            time.sleep(_REPLACE_WAIT_S)


def local_state(
    run_dir: Path, *, now: Callable[[], datetime] = _utc_now
) -> Literal["alive", "stale"] | None:
    """Whether the run's last local beat is recent: `alive`, `stale`, or None when there is
    no beat file (a run started by an older CLI, or not beating yet) or it cannot be read
    right now (unknown, never stale)."""
    found = _last_beat(run_dir)
    if found is None:
        return None
    taken, limit = found
    return "stale" if (now() - taken).total_seconds() > limit else "alive"


def stale_for(run_dir: Path, *, now: Callable[[], datetime] = _utc_now) -> timedelta | None:
    """How long a run whose local heartbeat went stale has been silent; None when it is
    alive or unknown (`local_state`)."""
    found = _last_beat(run_dir)
    if found is None:
        return None
    taken, limit = found
    silent = now() - taken
    return silent if silent.total_seconds() > limit else None


def _last_beat(run_dir: Path) -> tuple[datetime, float] | None:
    """The beat file's time and staleness limit, or None when absent or unreadable."""
    try:
        record = json.loads((Path(run_dir) / HEARTBEAT_FILE).read_text(encoding="utf-8"))
        taken = datetime.fromisoformat(record["at"])
        limit = float(record["stale_after_s"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None if taken.tzinfo is None else (taken, limit)


__all__ = ["HEARTBEAT_FILE", "WAITING", "Heartbeat", "Poster", "local_state", "stale_for"]
