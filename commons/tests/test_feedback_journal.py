"""The feedback journal (M3-11): Zyroflo's run-folder `feedback.jsonl`, with each of our rows
chained so the control plane verifies it like the run ledger."""

import json
from datetime import UTC, datetime
from pathlib import Path

from zyroflo.sdlc import feedback as zyroflo_feedback

from sdlc_commons.feedback import FeedbackJournal
from sdlc_commons.ledger import entry_hash

AT = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


def clock() -> datetime:
    return AT


def journal(tmp_path: Path, run_id: str = "r-learn-1") -> FeedbackJournal:
    return FeedbackJournal(tmp_path, run_id)


def test_file_is_zyroflos_feedback_jsonl(tmp_path: Path):
    found = journal(tmp_path)
    assert found.path == zyroflo_feedback.path_of(tmp_path, "r-learn-1")
    assert found.path.name == "feedback.jsonl"


def test_journal_chains_entries_and_verifies(tmp_path: Path):
    log = journal(tmp_path)
    first = log.append(
        "learning.feedback_captured", {"text": "use the queue"}, row_id="fb-1", clock=clock
    )
    second = log.append("learning.feedback_analysed", {"lesson": "x"}, row_id="fb-1:a", clock=clock)
    assert (first["seq"], first["prev"]) == (1, "")
    assert (second["seq"], second["prev"]) == (2, first["hash"])
    for entry in (first, second):
        assert entry["hash"] == entry_hash(entry)
    assert [e["id"] for e in log.entries()] == ["fb-1", "fb-1:a"]
    assert log.entries()[0]["at"] == "2026-10-25T09:00:00+00:00"
    assert log.verify() == []


def test_same_row_id_is_recorded_once(tmp_path: Path):
    log = journal(tmp_path)
    assert log.append("learning.feedback_captured", {"text": "a"}, row_id="fb-1", clock=clock)
    assert (
        log.append("learning.feedback_captured", {"text": "a"}, row_id="fb-1", clock=clock) is None
    )
    assert len(log.entries()) == 1


def test_zyroflo_rows_in_the_same_file_are_ignored(tmp_path: Path):
    log = journal(tmp_path)
    zyroflo_feedback.append(tmp_path, "r-learn-1", [{"id": "gate:G2:1", "kind": "gate"}])
    entry = log.append("learning.feedback_captured", {"text": "a"}, row_id="fb-1", clock=clock)
    assert entry["seq"] == 1
    assert [e["id"] for e in log.entries()] == ["fb-1"]
    assert log.verify() == []


def test_tampered_entry_fails_verify(tmp_path: Path):
    log = journal(tmp_path)
    log.append("learning.feedback_captured", {"text": "a"}, row_id="fb-1", clock=clock)
    log.append("learning.feedback_captured", {"text": "b"}, row_id="fb-2", clock=clock)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["text"] = "edited"
    lines[0] = json.dumps(row, sort_keys=True)
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    problems = log.verify()
    assert problems and "entry 1" in problems[0]


def test_an_unknown_event_name_is_refused(tmp_path: Path):
    import pytest

    with pytest.raises(ValueError, match="learning."):
        journal(tmp_path).append("gate", {}, row_id="x", clock=clock)
