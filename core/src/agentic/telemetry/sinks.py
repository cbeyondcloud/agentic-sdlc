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
    such as gate entries, are picked up this way).
"""

import json
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
_LOG = logging.getLogger("agentic.telemetry.sinks")


class SinkError(Exception):
    """The entries cannot be forwarded: the chain does not continue from the cursor."""


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
    seq, last = position["seq"], position["hash"]
    if seq > len(entries):
        raise SinkError(f"{sink.name}: the cursor is at event {seq} but the run has {len(entries)}")
    pending = [entry for entry in entries if _seq(entry) > seq]
    for entry in pending:
        if _seq(entry) != seq + 1 or entry.get("prev", "") != last:
            raise SinkError(
                f"{sink.name}: event {_seq(entry)} does not follow event {seq} it already "
                f"has - the local chain changed under the cursor; nothing was sent"
            )
        seq, last = _seq(entry), str(entry.get("hash", ""))
    if not pending:
        return 0
    sink.deliver(run_id, pending)
    _save(cursor, {"seq": seq, "hash": last})
    return len(pending)


class ForwardingRecorder:
    """A `RunRecorder` that records in the run ledger, then forwards to each sink."""

    def __init__(
        self,
        ledger: ChainedLedger,
        sinks: Sequence[EventSink],
        *,
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> None:
        names = [sink.name for sink in sinks]
        if bad := [name for name in names if not _NAME.fullmatch(name)]:
            raise ValueError(f"a sink name is a slug (a-z, 0-9, '-'), not {bad[0]!r}")
        if len(set(names)) != len(names):
            raise ValueError(f"sink names must be unique: {names}")
        self.ledger, self.sinks = ledger, list(sinks)
        self.on_error = on_error or _report

    @property
    def run_dir(self) -> Path:
        return self.ledger.run_dir

    def record(self, event: str, phase: str = "", **fields: object) -> object:
        entry = self.ledger.record(event, phase, **fields)
        self.flush()
        return entry

    def flush(self) -> dict[str, int]:
        """Forward to every sink what it has not had yet; returns how many each got."""
        if not self.sinks:
            return {}
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
    try:
        data = json.loads(cursor.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"seq": 0, "hash": ""}
    except (OSError, ValueError) as error:
        raise SinkError(f"sink cursor {cursor} cannot be read ({error})") from error
    if not isinstance(data, dict) or not isinstance(data.get("seq"), int):
        raise SinkError(f"sink cursor {cursor} is not a cursor")
    return {"seq": data["seq"], "hash": str(data.get("hash", ""))}


def _save(cursor: Path, position: dict) -> None:
    """Atomically: a crash leaves the old cursor, so the batch is sent again, never lost."""
    cursor.parent.mkdir(parents=True, exist_ok=True)
    temporary = cursor.with_name(f"{cursor.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(position) + "\n", encoding="utf-8")
    os.replace(temporary, cursor)
