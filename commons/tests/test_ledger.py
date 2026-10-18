"""Tests for the run-ledger adapter over Zyroflo's hash-chained ledger (M1-06)."""

import json
from pathlib import Path

import pytest

from sdlc_commons.ledger import LedgerError, RunLedger, digest, write_artifact


def open_ledger(target: Path, run_id: str = "r-test-1") -> RunLedger:
    ledger = RunLedger(target, run_id)
    ledger.open(kind="feature", project="demo", owner="dev@example.com", criticality="low")
    return ledger


def test_open_and_record_then_verify_clean(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    ledger.record("step_started", phase="requirements", attempt=1, actor="agent")

    assert ledger.verify() == []
    assert [e["event"] for e in ledger.events()] == ["run_opened", "step_started"]


def test_events_go_to_the_active_run_folder(tmp_path: Path):
    ledger = open_ledger(tmp_path)

    assert ledger.folder == tmp_path / ".sdlc" / "runs" / "r-test-1"
    assert (ledger.folder / "events.jsonl").is_file()


def test_tampered_event_is_detected_and_named(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    log = ledger.folder / "events.jsonl"
    record = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    record["owner"] = "mallory@example.com"
    log.write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )

    problems = ledger.verify()

    assert len(problems) == 1
    assert "event 1 (run_opened)" in problems[0]
    assert "edited" in problems[0]


def test_unknown_event_field_is_a_ledger_error(tmp_path: Path):
    ledger = open_ledger(tmp_path)

    with pytest.raises(LedgerError) as error:
        ledger.record("step_started", phase="design", attempt=1, actor="agent", team="payments")

    assert "team" in str(error.value)  # not a core field; our ids go in ext (M1-07)


def test_artifacts_are_written_with_lf_so_digests_match_across_os(tmp_path: Path):
    # Review Focus 1: the same handoff produced on Windows (CRLF) and Linux (LF)
    # must have the same digest, or approvals made on one OS fail on the other.
    windows = write_artifact(tmp_path / "win" / "design.md", "line one\r\nline two\r\n")
    linux = write_artifact(tmp_path / "lin" / "design.md", "line one\nline two\n")

    assert windows == linux
    assert b"\r" not in (tmp_path / "win" / "design.md").read_bytes()
    assert digest(tmp_path / "win" / "design.md") == windows


def test_torn_tail_is_detected_and_refused(tmp_path: Path):
    # Review Focus 2: a crash in the middle of an append leaves half a JSON line.
    ledger = open_ledger(tmp_path)
    with (ledger.folder / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"event":"step_started","run_id":"r-te')  # no newline: interrupted

    assert any("unreadable" in problem or "torn" in problem for problem in ledger.verify())
    with pytest.raises(LedgerError):
        ledger.record("step_started", phase="design", attempt=1, actor="agent")


def test_recovering_a_torn_tail_preserves_it_and_restores_the_chain(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    fragment = '{"event":"step_started","run_id":"r-te'
    with (ledger.folder / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(fragment)

    saved = ledger.recover_torn_tail()

    assert saved is not None and saved.read_text(encoding="utf-8") == fragment  # kept, not lost
    assert ledger.verify() == []
    ledger.record("step_started", phase="design", attempt=1, actor="agent")  # appends again
    assert ledger.verify() == []


def test_recover_does_nothing_on_a_healthy_ledger(tmp_path: Path):
    ledger = open_ledger(tmp_path)

    assert ledger.recover_torn_tail() is None
    assert ledger.verify() == []


def test_a_complete_last_line_is_never_treated_as_torn(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    log = ledger.folder / "events.jsonl"
    log.write_bytes(log.read_bytes().rstrip(b"\n"))  # valid JSON, just missing its newline

    assert ledger.recover_torn_tail() is None  # a whole record is never moved aside
    assert ledger.verify() == []
