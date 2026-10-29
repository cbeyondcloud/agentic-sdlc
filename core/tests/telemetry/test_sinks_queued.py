"""`queued(run_dir, sink)` (M3-08): how many recorded events a sink has not taken yet - the
same arithmetic as `ForwardingRecorder.pending`, read from the run folder alone, so `agentic
gc` can keep every run whose events are still queued for the control plane."""

import json
from pathlib import Path

from agentic.telemetry.sinks import queued


def run_folder(tmp_path: Path, events: int, cursor: object | None = None) -> Path:
    folder = tmp_path / "r-1"
    folder.mkdir()
    lines = "".join(json.dumps({"seq": n, "hash": f"h{n}"}) + "\n" for n in range(1, events + 1))
    (folder / "events.jsonl").write_text(lines, encoding="utf-8")
    if cursor is not None:
        (folder / "sinks").mkdir()
        text = cursor if isinstance(cursor, str) else json.dumps(cursor)
        (folder / "sinks" / "control-plane.json").write_text(text, encoding="utf-8")
    return folder


def test_nothing_delivered_counts_every_event(tmp_path: Path):
    assert queued(run_folder(tmp_path, 3), "control-plane") == 3


def test_part_delivered(tmp_path: Path):
    folder = run_folder(tmp_path, 5, {"seq": 2, "hash": "h2"})
    assert queued(folder, "control-plane") == 3


def test_all_delivered(tmp_path: Path):
    folder = run_folder(tmp_path, 4, {"seq": 4, "hash": "h4"})
    assert queued(folder, "control-plane") == 0


def test_an_unreadable_cursor_counts_every_event(tmp_path: Path):
    folder = run_folder(tmp_path, 2, "{not json")
    assert queued(folder, "control-plane") == 2


def test_no_ledger_has_nothing_queued(tmp_path: Path):
    assert queued(tmp_path, "control-plane") == 0


def test_a_torn_last_line_still_counts(tmp_path: Path):
    """A half-written last event is still an event the sink has not had."""
    folder = run_folder(tmp_path, 2, {"seq": 2, "hash": "h2"})
    with (folder / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 3, "ha')
    assert queued(folder, "control-plane") == 1
