"""Where a run's events go: the run folder first, then any other sink (M1-44; spec §7.3, §14.5).

The run ledger (`sdlc_commons.ledger.RunLedger`, Zyroflo's format) is the local sink
and the source of truth: every event is appended there, hash-chained, before anything
else sees it. Other sinks - the control plane in M2 (M2-20) - are fed FROM that chain,
never by a second one:

  * `EventSink` - a destination: it is handed entries exactly as chained (`seq`,
    `prev`, `hash` and all), so it can verify the chain on ingest;
  * `forward` - delivers the entries a sink has not had yet, in order, after checking
    that they continue from its cursor (the last `seq` and `hash` it accepted). The
    cursor moves only after the sink accepts, so a failure re-delivers from the same
    place next time: at least once, in order; a sink de-duplicates by (run, seq, hash);
  * `ForwardingRecorder` - the engine's `RunRecorder`: it records in the ledger, then
    forwards to every sink. A sink failure is reported and never stops the run or
    loses an event - the event is already in `events.jsonl` and goes on the next
    forward. `flush()` forwards without recording (events Zyroflo appended itself,
    such as gate entries, are picked up this way). With `background=True` (a network
    sink, M2-20) `record` only wakes a delivery thread, so a slow sink never holds up
    the run (DF-304); `close()` waits a bounded time and says what stays queued. A
    `screen` (the publish scan, M2-49) sees each event's fields before the ledger
    chains them and may mask them: a chained entry can never change on its way out.

The ledger and the cursor ARE the offline queue: nothing is copied anywhere else, so a
sink that was unreachable gets every event it missed, once and in order, on the next
forward or `agentic runs sync`. The cursor is written durably (fsync before the
rename); an empty or unreadable one - a crash mid-write - means "deliver everything
again", which a de-duplicating sink takes as a no-op (DF-179). The entry at the cursor
must still be the one the sink took (DF-172). To clear a `SinkError` about a changed
chain, delete `sinks/<name>.json` in the run folder: the next forward re-delivers from
the start, and a sink that de-duplicates by (run, seq, hash) reports the conflict.
"""

import json
import logging
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
_LOG = logging.getLogger("agentic.telemetry.sinks")


class SinkError(Exception):
    """The entries cannot be forwarded: the chain does not continue from the cursor."""


class SinkBehind(SinkError):
    """Raised by a sink whose last entry is before the batch it was given (its store lost
    events, or a cursor was copied): `forward` re-delivers from that head (R21)."""

    def __init__(self, seq: int, hash: str) -> None:
        super().__init__(f"the sink holds events up to {seq}")
        self.seq, self.hash = seq, hash


class EventSink(Protocol):
    name: str  # a slug: names the sink's cursor file

    def deliver(self, run_id: str, entries: Sequence[Mapping[str, object]]) -> None:
        """Accept every entry, in order, or raise (then none count as delivered)."""
        ...


class ChainedLedger(Protocol):
    """The run ledger as the recorder needs it (`sdlc_commons.ledger.RunLedger`)."""

    @property
    def run_dir(self) -> Path: ...

    def record(self, event: str, phase: str = "", **fields: object) -> object: ...

    def events(self) -> list[dict]: ...

    def write_artifact(self, rel: str, text: str) -> str: ...

    def digest(self, rel: str) -> str: ...


def forward(
    run_id: str, entries: Sequence[Mapping[str, object]], sink: EventSink, cursor: Path
) -> int:
    """Deliver the entries after `sink`'s cursor; returns how many were delivered."""
    position = _load(cursor)
    start, head = position["seq"], position["hash"]
    if start > len(entries):
        raise SinkError(
            f"{sink.name}: the cursor is at event {start} but the run has {len(entries)}"
        )
    if start and (_seq(entries[start - 1]) != start or entries[start - 1].get("hash") != head):
        raise SinkError(  # DF-172: the cursor's own entry, not only what follows it
            f"{sink.name}: event {start} at the cursor does not follow what the sink already "
            f"has - the local chain changed under the cursor; nothing was sent"
        )
    pending = _following(sink, entries, start, head)
    if not pending:
        return 0
    try:
        sink.deliver(run_id, pending)
    except SinkBehind as behind:  # R21: catch the sink up from its own head, once
        if behind.seq >= start or (
            behind.seq and entries[behind.seq - 1].get("hash") != behind.hash
        ):
            raise SinkError(
                f"{sink.name}: its head (event {behind.seq}) is not in the local chain - "
                "the two chains forked; nothing was sent"
            ) from None
        pending = _following(sink, entries, behind.seq, behind.hash)
        sink.deliver(run_id, pending)
    last = pending[-1]
    _save(cursor, {"seq": _seq(last), "hash": str(last.get("hash", ""))})
    return len(pending)


def _following(sink, entries, seq: int, last: str) -> list:
    """The entries after `seq`, checked to continue the chain from (`seq`, `last`)."""
    pending = [entry for entry in entries if _seq(entry) > seq]
    for entry in pending:
        if _seq(entry) != seq + 1 or entry.get("prev", "") != last:
            raise SinkError(
                f"{sink.name}: event {_seq(entry)} does not follow event {seq} it already "
                f"has - the local chain changed under the cursor; nothing was sent"
            )
        seq, last = _seq(entry), str(entry.get("hash", ""))
    return pending


class ForwardingRecorder:
    """A `RunRecorder` that records in the run ledger, then forwards to each sink."""

    def __init__(
        self,
        ledger: ChainedLedger,
        sinks: Sequence[EventSink],
        *,
        on_error: Callable[[str, Exception], None] | None = None,
        background: bool = False,
        screen: Callable[[str, dict], dict] | None = None,
    ) -> None:
        names = [sink.name for sink in sinks]
        if bad := [name for name in names if not _NAME.fullmatch(name)]:
            raise ValueError(f"a sink name is a slug (a-z, 0-9, '-'), not {bad[0]!r}")
        if len(set(names)) != len(names):
            raise ValueError(f"sink names must be unique: {names}")
        self.ledger, self.sinks = ledger, list(sinks)
        self.on_error = on_error or _report
        self.screen = screen
        self._flushing = threading.Lock()  # one forward per sink cursor at a time
        self._wake, self._stop, self._idle = threading.Event(), threading.Event(), threading.Event()
        self._idle.set()
        # wake and idle change together under this lock, so a request made while the thread
        # finishes a flush is never lost between its "anything new?" check and going idle
        self._state = threading.Lock()
        self._thread: threading.Thread | None = None
        if background and self.sinks:
            self._thread = threading.Thread(
                target=self._deliver_in_background, name="agentic-sinks", daemon=True
            )
            self._thread.start()

    @property
    def run_dir(self) -> Path:
        return self.ledger.run_dir

    def record(self, event: str, phase: str = "", **fields: object) -> object:
        if self.screen is not None:  # before the chain: a chained entry never changes (M2-49)
            fields = self.screen(event, fields)
        entry = self.ledger.record(event, phase, **fields)
        if self._thread is not None:
            self._request()  # the delivery thread forwards it; the run goes on
        else:
            self.flush()
        return entry

    def flush(self) -> dict[str, int]:
        """Forward to every sink what it has not had yet; returns how many each got."""
        if not self.sinks:
            return {}
        with self._flushing:
            entries = self.ledger.events()
            run_id = str(entries[0].get("run_id", "")) if entries else ""
            sent = {}
            for sink in self.sinks:
                try:
                    sent[sink.name] = forward(run_id, entries, sink, self._cursor(sink))
                except Exception as error:  # a sink never stops the run (the event is on disk)
                    sent[sink.name] = 0
                    self.on_error(sink.name, error)
            return sent

    def pending(self) -> dict[str, int]:
        """How many recorded events each sink has not had yet (its offline queue)."""
        total = len(self.ledger.events())
        queued = {}
        for sink in self.sinks:
            try:
                queued[sink.name] = max(total - _load(self._cursor(sink))["seq"], 0)
            except SinkError:
                queued[sink.name] = total
        return queued

    def close(self, timeout: float = 5.0) -> dict[str, int]:
        """Give the delivery thread up to `timeout` seconds to forward what is pending,
        then stop it; returns what stays queued per sink (the next forward, or
        `agentic sync`, delivers it)."""
        if self._thread is not None:
            # one more flush, even with nothing recorded since: entries Zyroflo appended
            # itself (gate decisions) never woke the thread
            self._request()
            self._idle.wait(timeout)
            self._stop.set()
            self._wake.set()
            self._thread.join(timeout=1.0)
            self._thread = None
        return self.pending()

    def _request(self) -> None:
        with self._state:
            self._idle.clear()
            self._wake.set()

    def _deliver_in_background(self) -> None:
        while True:
            self._wake.wait()
            with self._state:
                self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.flush()
            except Exception as error:  # the ledger itself could not be read
                self.on_error("all", error)
            with self._state:
                if not self._wake.is_set():
                    self._idle.set()

    def write_artifact(self, rel: str, text: str) -> str:
        return self.ledger.write_artifact(rel, text)

    def digest(self, rel: str) -> str:
        return self.ledger.digest(rel)

    def _cursor(self, sink: EventSink) -> Path:
        return self.ledger.run_dir / "sinks" / f"{sink.name}.json"


def _report(name: str, error: Exception) -> None:
    _LOG.warning("sink %s: delivery failed, will retry on the next event (%s)", name, error)


def _seq(entry: Mapping[str, object]) -> int:
    value = entry.get("seq")
    return value if isinstance(value, int) else 0


def _load(cursor: Path) -> dict:
    """The sink's position; the start (deliver everything) when there is none, or when
    the file is empty or unreadable - a crash mid-write, which must not leave the sink
    stuck (DF-179: at least once already requires sinks to de-duplicate)."""
    try:
        data = json.loads(cursor.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"seq": 0, "hash": ""}
    except (OSError, ValueError) as error:
        _LOG.warning("sink cursor %s cannot be read (%s): delivering from the start", cursor, error)
        return {"seq": 0, "hash": ""}
    if not isinstance(data, dict) or not isinstance(data.get("seq"), int) or data["seq"] < 0:
        _LOG.warning("sink cursor %s is not a cursor: delivering from the start", cursor)
        return {"seq": 0, "hash": ""}
    return {"seq": data["seq"], "hash": str(data.get("hash", ""))}


def _save(cursor: Path, position: dict) -> None:
    """Durably and atomically: the new cursor is flushed to disk before it replaces the
    old one, so a crash leaves the old cursor (the batch is sent again, never lost) or the
    new one whole (DF-179)."""
    cursor.parent.mkdir(parents=True, exist_ok=True)
    temporary = cursor.with_name(f"{cursor.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(position) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, cursor)
