"""Tests for the run-ledger adapter over Zyroflo's hash-chained ledger (M1-06)."""

import json
from pathlib import Path

import pytest

from sdlc_commons import ledger as ledger_module
from sdlc_commons.ledger import LedgerError, RunIdentity, RunLedger, digest, write_artifact

IDENTITY = RunIdentity(team="platform", policy_version="git:" + "a" * 40)


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


def open_ledger(target: Path, run_id: str = "r-test-1") -> RunLedger:
    ledger = RunLedger(target, run_id)
    ledger.open(identity=IDENTITY, kind="feature", project="demo", owner="platform",
                criticality="low")  # fmt: skip
    return ledger


def tear(log: Path, fragment: str = '{"event":"step_started","run_id":"r-te') -> str:
    with log.open("a", encoding="utf-8", newline="") as fh:
        fh.write(fragment)  # no newline: an append interrupted by a crash
    return fragment


# ── the chain ─────────────────────────────────────────────────────────────────


def test_open_and_record_then_verify_clean(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    ledger.record("step_started", phase="requirements", attempt=1, actor="agent")

    assert ledger.verify() == []
    assert [e["event"] for e in ledger.events()] == ["run_opened", "step_started"]


def test_events_go_to_the_active_run_folder(tmp_path: Path):
    ledger = open_ledger(tmp_path)

    assert ledger.folder == tmp_path / ".agentic" / "local" / "runs" / "r-test-1"
    assert (ledger.folder / "events.jsonl").is_file()


def test_tampered_event_is_detected_and_named(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    log = ledger.folder / "events.jsonl"
    record = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    record["owner"] = "mallory"
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


def test_opening_a_run_twice_is_refused(tmp_path: Path):
    ledger = open_ledger(tmp_path)

    with pytest.raises(LedgerError, match="already open"):
        ledger.open(identity=IDENTITY, kind="feature", project="demo", owner="platform",
                    criticality="low")  # fmt: skip
    assert len(ledger.events()) == 1


def test_an_event_cannot_be_backdated(tmp_path: Path):
    ledger = open_ledger(tmp_path)

    with pytest.raises(LedgerError, match="at"):
        ledger.record("step_started", phase="design", attempt=1, actor="agent",
                      at="2020-01-01T00:00:00Z")  # fmt: skip


# ── run ids (I1) ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("run_id", ["../../evil", "/abs/run", "C:x", "r1:x", "r 1", "", "r1.",
                                    "a/b", "-r1", "x" * 101, ".hidden"])  # fmt: skip
def test_a_bad_run_id_is_refused_before_any_folder_is_made(tmp_path: Path, run_id):
    with pytest.raises(LedgerError, match="run id"):
        RunLedger(tmp_path, run_id)

    assert list(tmp_path.iterdir()) == []


# ── no free text in events (I3) ───────────────────────────────────────────────


@pytest.mark.parametrize("fields", [
    {"owner": "alice@example.com"}, {"project": "my secret project"},
    {"kind": "a long free text note"}, {"criticality": "extreme"},
])  # fmt: skip
def test_run_opened_fields_must_be_ids(tmp_path: Path, fields):
    ledger = RunLedger(tmp_path, "r-free")
    values = {"kind": "feature", "project": "demo", "owner": "platform", "criticality": "low"}

    with pytest.raises(LedgerError) as error:
        ledger.open(identity=IDENTITY, **{**values, **fields})
    assert next(iter(fields.values())) not in str(error.value)  # the value is never echoed
    assert not (tmp_path / ".agentic" / "local" / "runs" / "r-free").exists()


@pytest.mark.parametrize(("phase", "fields"), [
    ("design", {"attempt": 1, "actor": "the user said: password is hunter2"}),
    ("free text phase", {"attempt": 1, "actor": "agent"}),
    ("build", {"attempt": 1, "actor": "agent", "outcome": "my prompt: ignore previous"}),
    ("build", {"attempt": 1, "actor": "agent", "artifacts": {"note": "free text here"}}),
])  # fmt: skip
def test_event_fields_must_be_ids_and_nothing_is_written(tmp_path: Path, phase, fields):
    ledger = open_ledger(tmp_path)
    event = "step_finished" if "outcome" in fields or "artifacts" in fields else "step_started"

    with pytest.raises(LedgerError) as error:
        ledger.record(event, phase=phase, **fields)
    assert "hunter2" not in str(error.value) and "ignore previous" not in str(error.value)
    assert len(ledger.events()) == 1


# ── artifacts ─────────────────────────────────────────────────────────────────


def test_artifacts_are_written_with_lf_so_digests_match_across_os(tmp_path: Path):
    # Review Focus 1: the same handoff produced on Windows (CRLF) and Linux (LF)
    # must have the same digest, or approvals made on one OS fail on the other.
    windows = write_artifact(tmp_path / "win" / "design.md", "line one\r\nline two\r\n")
    linux = write_artifact(tmp_path / "lin" / "design.md", "line one\nline two\n")

    assert windows == linux
    assert b"\r" not in (tmp_path / "win" / "design.md").read_bytes()
    assert digest(tmp_path / "win" / "design.md") == windows


# ── a crash mid-append (I2, Review Focus 2) ───────────────────────────────────


def test_torn_tail_is_detected_and_refused(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    tear(ledger.folder / "events.jsonl")

    assert any("torn" in problem for problem in ledger.verify())
    with pytest.raises(LedgerError):
        ledger.record("step_started", phase="design", attempt=1, actor="agent")


def test_recovering_a_torn_tail_preserves_it_and_restores_the_chain(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    fragment = tear(ledger.folder / "events.jsonl")

    (saved,) = ledger.recover_torn_tail()

    assert saved.read_text(encoding="utf-8") == fragment  # kept, not lost
    assert ledger.verify() == []
    ledger.record("step_started", phase="design", attempt=1, actor="agent")  # appends again
    assert ledger.verify() == []


def test_a_torn_approvals_ledger_is_detected_and_recovered(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    approvals = ledger.folder / "approvals.jsonl"
    approvals.write_text("", encoding="utf-8")
    fragment = tear(approvals, '{"kind":"approval","gate":"G')

    assert any("approvals.jsonl" in p and "torn" in p for p in ledger.verify())
    (saved,) = ledger.recover_torn_tail()
    assert saved.read_text(encoding="utf-8") == fragment
    assert approvals.read_bytes() == b""


def test_if_the_fragment_cannot_be_saved_the_ledger_is_left_untouched(tmp_path, monkeypatch):
    ledger = open_ledger(tmp_path)
    log = ledger.folder / "events.jsonl"
    tear(log)
    before = log.read_bytes()

    def disk_full(path: Path, data: bytes) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(ledger_module, "_save_durably", disk_full)
    with pytest.raises(LedgerError):
        ledger.recover_torn_tail()
    assert log.read_bytes() == before  # nothing truncated before the fragment was safe


def test_recover_does_nothing_on_a_healthy_ledger(tmp_path: Path):
    ledger = open_ledger(tmp_path)

    assert ledger.recover_torn_tail() == []
    assert ledger.verify() == []


def test_a_complete_last_line_is_never_treated_as_torn(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    log = ledger.folder / "events.jsonl"
    log.write_bytes(log.read_bytes().rstrip(b"\n"))  # valid JSON, just missing its newline

    assert ledger.recover_torn_tail() == []  # a whole record is never moved aside
    assert ledger.verify() == []


def test_a_log_that_is_one_torn_line_recovers_to_empty(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    log = ledger.folder / "events.jsonl"
    log.write_bytes(b'{"event":"run_op')

    (saved,) = ledger.recover_torn_tail()
    assert log.read_bytes() == b"" and saved.read_bytes() == b'{"event":"run_op'


@pytest.mark.parametrize("run_id", ["con", "CON", "aux.txt", "COM1", "lpt1", "nul", "prn.log"])
def test_windows_device_names_are_refused_as_run_ids(tmp_path: Path, run_id):
    with pytest.raises(LedgerError, match="run id"):
        RunLedger(tmp_path, run_id)


def test_ext_cannot_be_passed_by_hand(tmp_path: Path):
    ledger = open_ledger(tmp_path)

    with pytest.raises(LedgerError, match="ext"):
        ledger.record("step_started", phase="design", attempt=1, actor="agent",
                      ext={"agentic": {"team": "other"}})  # fmt: skip


# ── artefacts the engine pins (SP6, DF-09) and a non-record tail (DF-12) ───────


def test_an_artefact_is_written_inside_the_run_folder_and_digested(tmp_path: Path):
    ledger = open_ledger(tmp_path)

    digest_value = ledger.write_artifact("handoffs/design-1.md", "# Design\r\nText\r\n")
    path = ledger.run_dir / "handoffs" / "design-1.md"
    assert path.read_bytes() == b"# Design\nText\n"
    assert digest_value == ledger.digest("handoffs/design-1.md") == digest(path)


@pytest.mark.parametrize("rel", ["../escape.md", "/abs.md", "C:/abs.md", "", "a/../../b.md"])
def test_an_artefact_outside_the_run_folder_is_refused(tmp_path: Path, rel):
    ledger = open_ledger(tmp_path)

    with pytest.raises(LedgerError):
        ledger.write_artifact(rel, "x")
    assert not (tmp_path / "escape.md").exists()


def test_an_artefact_write_is_atomic(tmp_path: Path, monkeypatch):
    ledger = open_ledger(tmp_path)
    ledger.write_artifact("handoffs/design-1.md", "first")

    def power_cut(src, dst):
        raise OSError("power cut")

    monkeypatch.setattr(ledger_module.os, "replace", power_cut)
    with pytest.raises(OSError):
        ledger.write_artifact("handoffs/design-1.md", "second")
    folder = ledger.run_dir / "handoffs"
    assert (folder / "design-1.md").read_text(encoding="utf-8") == "first"
    assert [p.name for p in folder.iterdir()] == ["design-1.md"]


def test_a_json_tail_that_is_not_a_record_counts_as_torn(tmp_path: Path):  # DF-12
    ledger = open_ledger(tmp_path)
    ledger.record("step_started", "design")
    log = ledger.folder / "events.jsonl"
    tear(log, "12")  # a number cut short: valid JSON, not a record

    kept = ledger.recover_torn_tail()
    assert len(kept) == 1 and kept[0].read_bytes() == b"12"
    assert ledger.verify() == []
