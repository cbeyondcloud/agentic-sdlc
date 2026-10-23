"""Sink delivery for M2-20: the run ledger plus the cursor is the offline queue; a delivery
never blocks the run (DF-304, R20); the cursor's own entry is checked (DF-172); a lost or
torn cursor re-delivers from the start (DF-179); a sink behind the cursor is caught up from
its head (R21)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from agentic.telemetry.sinks import ForwardingRecorder, SinkBehind, SinkError, forward


def chain(n: int) -> list[dict]:
    entries, prev = [], ""
    for seq in range(1, n + 1):
        entry = {"seq": seq, "prev": prev, "hash": f"h{seq}", "run_id": "r"}
        entries.append(entry)
        prev = entry["hash"]
    return entries


class Dedup:
    """A sink that keeps each (seq, hash) once, as the control plane does."""

    name = "control-plane"

    def __init__(self) -> None:
        self.kept: dict[int, str] = {}
        self.calls = 0
        self.offline = False
        self.slow = 0.0

    def deliver(self, run_id, entries) -> None:
        self.calls += 1
        if self.slow:
            time.sleep(self.slow)
        if self.offline:
            raise ConnectionError("unreachable")
        for entry in entries:
            self.kept.setdefault(entry["seq"], entry["hash"])


class Ledger:
    """A minimal run ledger: entries in memory, a run folder on disk."""

    def __init__(self, folder: Path) -> None:
        self.run_dir = folder
        self.entries: list[dict] = []

    def record(self, event, phase="", **fields):
        seq = len(self.entries) + 1
        prev = self.entries[-1]["hash"] if self.entries else ""
        entry = {"seq": seq, "prev": prev, "hash": f"h{seq}", "run_id": "r", "event": event}
        self.entries.append(entry)
        return entry

    def events(self):
        return list(self.entries)


def test_cursor_hash_mismatch_is_refused(tmp_path):
    """DF-172: the entry at the cursor must be the one the sink took."""
    cursor = tmp_path / "c.json"
    cursor.write_text(json.dumps({"seq": 2, "hash": "not-h2"}), encoding="utf-8")
    sink = Dedup()
    with pytest.raises(SinkError, match="does not follow"):
        forward("r", chain(2), sink, cursor)  # nothing newer, yet the cursor's entry changed
    assert sink.calls == 0


@pytest.mark.parametrize("content", ["", "{not json", "[]"])
def test_empty_cursor_redelivers_from_the_start(tmp_path, content):
    """DF-179: a torn or unreadable cursor means 'deliver everything again' (the sink
    de-duplicates), never a sink stuck until someone deletes the file."""
    cursor = tmp_path / "c.json"
    cursor.write_text(content, encoding="utf-8")
    sink = Dedup()
    assert forward("r", chain(3), sink, cursor) == 3
    assert json.loads(cursor.read_text(encoding="utf-8")) == {"seq": 3, "hash": "h3"}


def test_gap_rewinds_to_the_server_head(tmp_path):
    """R21: the sink's head is behind the cursor; the entries after it are sent again."""
    cursor = tmp_path / "c.json"
    entries = chain(4)
    sink = Dedup()
    assert forward("r", entries[:2], sink, cursor) == 2
    sink.kept = {1: "h1"}  # the control plane lost event 2 (restored from a backup)
    calls = []

    class Behind(Dedup):
        def deliver(self, run_id, batch):
            calls.append([e["seq"] for e in batch])
            if batch[0]["seq"] > 2:
                raise SinkBehind(1, "h1")
            super().deliver(run_id, batch)

    behind = Behind()
    behind.kept = dict(sink.kept)
    assert forward("r", entries, behind, cursor) == 3
    assert calls == [[3, 4], [2, 3, 4]]
    assert json.loads(cursor.read_text(encoding="utf-8"))["seq"] == 4


def test_a_head_outside_the_local_chain_is_a_fork(tmp_path):
    cursor = tmp_path / "c.json"
    entries = chain(3)
    forward("r", entries[:2], Dedup(), cursor)

    class Forked(Dedup):
        def deliver(self, run_id, batch):
            raise SinkBehind(1, "some-other-hash")

    with pytest.raises(SinkError, match="forked"):
        forward("r", entries, Forked(), cursor)


def test_events_queued_offline_replay_once_in_order(tmp_path):
    """M2-20 done criterion: events recorded while the control plane is down are
    delivered once each, in order, when it is back (the ledger is the queue)."""
    ledger, sink = Ledger(tmp_path), Dedup()
    errors = []
    recorder = ForwardingRecorder(ledger, [sink], on_error=lambda n, e: errors.append(e))
    sink.offline = True
    for _ in range(3):
        recorder.record("step_started")
    assert sink.kept == {} and len(errors) == 3
    sink.offline = False
    recorder.record("step_finished")
    assert list(sink.kept) == [1, 2, 3, 4]
    assert recorder.pending() == {"control-plane": 0}


def test_background_recorder_does_not_block_on_a_slow_sink(tmp_path):
    """DF-304, R20: `record` returns at once; delivery happens on the sink thread."""
    ledger, sink = Ledger(tmp_path), Dedup()
    sink.slow = 0.5
    recorder = ForwardingRecorder(ledger, [sink], background=True)
    started = time.monotonic()
    for _ in range(4):
        recorder.record("step_started")
    assert time.monotonic() - started < 0.4
    assert recorder.close(timeout=10) == {"control-plane": 0}
    assert list(sink.kept) == [1, 2, 3, 4]


def test_close_delivers_entries_written_straight_to_the_ledger(tmp_path):
    """Gate entries are appended by Zyroflo itself, so no `record` woke the thread:
    `close` still forwards them before it stops (review F4)."""
    ledger, sink = Ledger(tmp_path), Dedup()
    recorder = ForwardingRecorder(ledger, [sink], background=True)
    recorder.record("step_started")
    for _ in range(50):  # let the thread go idle after its flush
        if sink.kept:
            break
        time.sleep(0.02)
    ledger.record("gate_decided")  # not through the recorder
    assert recorder.close(timeout=10) == {"control-plane": 0}
    assert list(sink.kept) == [1, 2]


def test_close_waits_for_a_slow_final_delivery(tmp_path):
    ledger, sink = Ledger(tmp_path), Dedup()
    sink.slow = 1.5  # longer than any fixed join, shorter than the grace
    recorder = ForwardingRecorder(ledger, [sink], background=True)
    recorder.record("run_closed")
    assert recorder.close(timeout=10) == {"control-plane": 0}


def test_close_reports_what_stays_queued(tmp_path):
    ledger, sink = Ledger(tmp_path), Dedup()
    sink.offline = True
    recorder = ForwardingRecorder(ledger, [sink], background=True, on_error=lambda n, e: None)
    for _ in range(3):
        recorder.record("step_started")
    assert recorder.close(timeout=2) == {"control-plane": 3}


def test_background_flushes_never_overlap(tmp_path):
    ledger = Ledger(tmp_path)
    active, overlaps = [0], []
    lock = threading.Lock()

    class Watch(Dedup):
        def deliver(self, run_id, entries):
            with lock:
                active[0] += 1
                if active[0] > 1:
                    overlaps.append(True)
            time.sleep(0.01)
            with lock:
                active[0] -= 1
            super().deliver(run_id, entries)

    sink = Watch()
    recorder = ForwardingRecorder(ledger, [sink], background=True)
    for _ in range(20):
        recorder.record("step_started")
        recorder.flush()  # the caller may flush too (gate entries)
    recorder.close(timeout=10)
    assert overlaps == [] and list(sink.kept) == list(range(1, 21))
