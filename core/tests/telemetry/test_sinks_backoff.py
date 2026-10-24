"""A sink that keeps failing (M2-20 review F14; DF-499, DF-524, DF-535): one warning per
sink until the error changes or delivery recovers, a backoff before the background thread
reads the run ledger again, one last attempt at close, and a refusal a retry cannot fix
(the publish scan holding a run back) never reported as "will retry"."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from agentic.telemetry.sinks import ForwardingRecorder, SinkError


class Flaky:
    name = "control-plane"

    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = ConnectionError("unreachable")
        self.kept: list[int] = []

    def deliver(self, run_id, entries) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        self.kept += [entry["seq"] for entry in entries]


class Ledger:
    def __init__(self, folder: Path) -> None:
        self.run_dir = folder
        self.entries: list[dict] = []
        self.reads = 0

    def record(self, event, phase="", **fields):
        seq = len(self.entries) + 1
        prev = self.entries[-1]["hash"] if self.entries else ""
        entry = {"seq": seq, "prev": prev, "hash": f"h{seq}", "run_id": "r", "event": event}
        self.entries.append(entry)
        return entry

    def events(self):
        self.reads += 1
        return list(self.entries)


class HeldBack(SinkError):
    retryable = False


def test_a_repeated_failure_is_reported_once_until_it_changes(tmp_path):
    sink, seen = Flaky(), []
    recorder = ForwardingRecorder(Ledger(tmp_path), [sink], on_error=lambda n, e: seen.append(e))
    for _ in range(5):
        recorder.record("step_started")
    assert len(seen) == 1 and sink.calls == 5
    sink.error = ConnectionError("tls handshake failed")  # a different problem: said again
    recorder.record("step_started")
    assert len(seen) == 2
    sink.error = None
    recorder.record("step_started")
    assert sink.kept == [1, 2, 3, 4, 5, 6, 7]
    sink.error = ConnectionError("tls handshake failed")  # after a recovery: said again
    recorder.record("step_started")
    assert len(seen) == 3


def test_a_refusal_a_retry_cannot_fix_is_not_called_a_retry(tmp_path, caplog):
    sink = Flaky()
    sink.error = HeldBack("run r event 3 is held back by the publish scan")
    caplog.set_level(logging.INFO, logger="agentic.telemetry.sinks")
    ForwardingRecorder(Ledger(tmp_path), [sink]).record("step_started")
    assert "held back" in caplog.text and "will retry" not in caplog.text
    caplog.clear()
    sink.error = ConnectionError("unreachable")
    ForwardingRecorder(Ledger(tmp_path), [sink]).record("step_started")
    assert "will retry" in caplog.text


def test_the_background_thread_backs_off_while_failing(tmp_path):
    sink, ledger = Flaky(), Ledger(tmp_path)
    recorder = ForwardingRecorder(ledger, [sink], background=True, on_error=lambda n, e: None)
    recorder.record("step_started")
    deadline = time.monotonic() + 5
    while sink.calls < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    for _ in range(20):
        recorder.record("step_started")
        time.sleep(0.01)
    assert sink.calls <= 2, sink.calls  # not one ledger read per event while offline
    calls = sink.calls
    assert recorder.close(timeout=3) == {"control-plane": 21}
    assert sink.calls > calls  # close still makes a last attempt, at once


def test_delivery_resumes_after_the_backoff(tmp_path):
    sink, ledger = Flaky(), Ledger(tmp_path)
    recorder = ForwardingRecorder(ledger, [sink], background=True, on_error=lambda n, e: None)
    recorder.record("step_started")
    deadline = time.monotonic() + 5
    while sink.calls < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    sink.error = None
    recorder.record("step_started")
    deadline = time.monotonic() + 5
    while len(sink.kept) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sink.kept == [1, 2]
    assert recorder.close(timeout=3) == {"control-plane": 0}
