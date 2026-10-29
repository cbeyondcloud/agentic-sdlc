"""The CLI's run heartbeat (M3-07, Review Focus 4): while a command drives a run, a daemon
thread beats at once and every `heartbeat_seconds`: it rewrites `<run>/heartbeat.json` and,
in org mode with a control plane, posts the beat. Nothing it does can block or fail the
run: a control plane that cannot be reached is logged once and tried again at the next
beat. A command that ends with the run waiting (a gate, a pause) sends one `waiting` beat.
`local_state` reads the beat file back for `agentic logs`."""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest

from agentic_cli.control_plane import ControlPlaneUnavailable
from agentic_cli.heartbeat import HEARTBEAT_FILE, Heartbeat, local_state, stale_for

NOW = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)


class Poster:
    def __init__(self, error: Exception | None = None) -> None:
        self.beats: list[dict] = []
        self.error = error
        self.closed = False
        self.posted = threading.Event()

    def heartbeat(self, run_id, *, beat_at, sent_at, state):
        self.beats.append(
            {"run_id": run_id, "beat_at": beat_at, "sent_at": sent_at, "state": state}
        )
        self.posted.set()
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def run_dir(tmp_path):
    folder = tmp_path / "runs" / "run-1"
    folder.mkdir(parents=True)
    return folder


def test_a_beat_writes_the_file_and_posts(run_dir):
    poster, clock = Poster(), Clock()
    Heartbeat(run_dir, "run-1", interval_s=60, poster=poster, wall=clock).beat()
    written = json.loads((run_dir / HEARTBEAT_FILE).read_text(encoding="utf-8"))
    assert written["run_id"] == "run-1" and written["at"] == NOW.isoformat()
    assert written["state"] == "running" and written["interval_s"] == 60
    assert written["stale_after_s"] == 185  # three beats and a margin
    assert poster.beats == [{"run_id": "run-1", "beat_at": NOW, "sent_at": NOW, "state": "running"}]


def test_without_a_poster_only_the_file_is_written(run_dir):
    Heartbeat(run_dir, "run-1", interval_s=60, wall=Clock()).beat()
    assert (run_dir / HEARTBEAT_FILE).is_file()


def test_a_folder_that_does_not_exist_yet_is_not_created(tmp_path):
    """A run's folder is the engine's to create: the beat waits for it."""
    missing = tmp_path / "runs" / "later"
    poster = Poster()
    assert Heartbeat(missing, "later", interval_s=60, poster=poster, wall=Clock()).beat() is False
    assert not missing.exists() and len(poster.beats) == 1


def test_an_unreachable_control_plane_is_logged_once_and_never_raised(run_dir, caplog):
    poster = Poster(ControlPlaneUnavailable("cannot reach the control plane"))
    beats = Heartbeat(run_dir, "run-1", interval_s=60, poster=poster, wall=Clock())
    with caplog.at_level(logging.WARNING, logger="agentic.heartbeat"):
        for _ in range(3):
            beats.beat()
    assert len(poster.beats) == 3 and (run_dir / HEARTBEAT_FILE).is_file()
    warnings = [r.getMessage() for r in caplog.records if r.name == "agentic.heartbeat"]
    assert len(warnings) == 1 and "cannot reach the control plane" in warnings[0]


def test_any_poster_error_is_contained(run_dir):
    poster = Poster(RuntimeError("a bug in the poster"))
    Heartbeat(run_dir, "run-1", interval_s=60, poster=poster, wall=Clock()).beat()  # no raise


@pytest.mark.parametrize(("status", "sent"), [("awaiting_gate", 1), ("paused", 1)])
def test_a_command_ending_at_a_wait_sends_one_waiting_beat(run_dir, status, sent):
    poster = Poster()
    Heartbeat(run_dir, "run-1", interval_s=60, poster=poster, wall=Clock()).finish(status)
    assert [b["state"] for b in poster.beats] == ["waiting"] * sent
    written = json.loads((run_dir / HEARTBEAT_FILE).read_text(encoding="utf-8"))
    assert written["state"] == "waiting"


@pytest.mark.parametrize("status", ["completed", "failed", "stopped", "killed", "running"])
def test_any_other_ending_sends_nothing(run_dir, status):
    """A finished run is closed by its run_closed event; one still `running` was interrupted
    and nothing drives it, so the control plane should see it go quiet."""
    poster = Poster()
    Heartbeat(run_dir, "run-1", interval_s=60, poster=poster, wall=Clock()).finish(status)
    assert poster.beats == []


def test_the_thread_beats_at_once_and_stops_on_exit(run_dir):
    poster = Poster()
    with Heartbeat(run_dir, "run-1", interval_s=60, poster=poster) as beats:
        assert poster.posted.wait(5), "no beat at the start"
        assert beats.running
    assert not beats.running
    assert poster.closed
    count = len(poster.beats)
    assert count == 1  # the next beat is a minute away; none after the exit


def test_the_waiting_beat_is_sent_after_the_thread_stopped(run_dir):
    """`ended` inside the block: the last beat the control plane gets says waiting - no
    running beat of the thread can land after it."""
    poster = Poster()
    with Heartbeat(run_dir, "run-1", interval_s=60, poster=poster) as beats:
        assert poster.posted.wait(5)
        beats.ended("awaiting_gate")
    assert [b["state"] for b in poster.beats] == ["running", "waiting"]
    assert poster.closed


@pytest.mark.parametrize(("status", "sent"), [("awaiting_gate", "waiting"), ("running", None)])
def test_a_failed_engine_call_ends_on_the_runs_own_status(run_dir, status, sent):
    """Review F1: the engine refused (a decision without the role, `resume` at a gate) and
    `ended` never ran - the run's stored status still decides the last beat."""
    poster = Poster()
    with pytest.raises(RuntimeError):
        with Heartbeat(run_dir, "run-1", interval_s=60, poster=poster, status=lambda: status):
            assert poster.posted.wait(5)
            raise RuntimeError("refused")
    assert [b["state"] for b in poster.beats][1:] == ([sent] if sent else [])


def test_an_unreadable_status_after_a_failure_sends_nothing_more(run_dir):
    def broken() -> str:
        raise OSError("run.json is being replaced")

    poster = Poster()
    with pytest.raises(RuntimeError):
        with Heartbeat(run_dir, "run-1", interval_s=60, poster=poster, status=broken):
            assert poster.posted.wait(5)
            raise RuntimeError("refused")
    assert [b["state"] for b in poster.beats] == ["running"]


def test_the_thread_is_a_daemon_and_never_holds_the_command(run_dir):
    slow = threading.Event()

    class Hanging(Poster):
        def heartbeat(self, run_id, **kwargs):
            super().heartbeat(run_id, **kwargs)
            slow.wait(30)  # a control plane that never answers

    poster = Hanging()
    started = datetime.now(UTC)
    with Heartbeat(run_dir, "run-1", interval_s=60, poster=poster, join_s=0.2) as beats:
        assert poster.posted.wait(5)
    assert (datetime.now(UTC) - started) < timedelta(seconds=10)
    assert beats.thread is None or beats.thread.daemon
    slow.set()


def test_local_state_reads_the_beat_file(run_dir):
    clock = Clock()
    assert local_state(run_dir, now=clock) is None  # no beat yet: unknown
    Heartbeat(run_dir, "run-1", interval_s=60, wall=clock).beat()
    clock.now = NOW + timedelta(seconds=180)
    assert local_state(run_dir, now=clock) == "alive"
    clock.now = NOW + timedelta(seconds=186)
    assert local_state(run_dir, now=clock) == "stale"


def test_stale_for_says_how_long_a_stale_run_has_been_silent(run_dir):
    clock = Clock()
    assert stale_for(run_dir, now=clock) is None  # no beat file: unknown, never stale
    Heartbeat(run_dir, "run-1", interval_s=60, wall=clock).beat()
    clock.now = NOW + timedelta(seconds=100)
    assert stale_for(run_dir, now=clock) is None  # alive
    clock.now = NOW + timedelta(minutes=10)
    assert stale_for(run_dir, now=clock) == timedelta(minutes=10)


def test_an_unreadable_beat_file_is_unknown(run_dir):
    (run_dir / HEARTBEAT_FILE).write_text("{half", encoding="utf-8")
    assert local_state(run_dir) is None
    (run_dir / HEARTBEAT_FILE).write_text('{"at": "yesterday"}', encoding="utf-8")
    assert local_state(run_dir) is None
