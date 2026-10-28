"""`verify_entries` (SP15 Task 9): Zyroflo's chain rules over entries held in memory, so the
control plane verifies an ingested batch exactly as `RunLedger.verify` checks a run folder,
continuing from the head it already stored."""

import copy
from pathlib import Path

import pytest

from sdlc_commons import ledger as ledger_module
from sdlc_commons.ledger import RunIdentity, RunLedger, entry_hash, verify_entries

POLICY = "git:" + "c" * 40


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


def _ledger(tmp_path: Path, run_id: str = "r-verify", team: str = "payments") -> list[dict]:
    ledger = RunLedger(tmp_path / run_id, run_id)
    ledger.open(
        identity=RunIdentity(team=team, policy_version=POLICY),
        kind="feature",
        project="demo",
        owner=team,
        criticality="low",
    )
    ledger.record("step_started", phase="requirements", attempt=1)
    ledger.record("step_finished", phase="requirements", attempt=1, outcome="pass")
    ledger.record("gate_waiting", gate="requirements-approved", hard=True)
    ledger.record("step_started", phase="design", attempt=1)
    ledger.record("run_closed", status="done")
    return ledger.events()


def test_verify_entries_accepts_a_real_ledger(tmp_path):
    entries = _ledger(tmp_path)
    assert len(entries) == 6
    assert verify_entries(entries, run_id="r-verify") == []


def test_verify_entries_continues_from_a_head(tmp_path):
    entries = _ledger(tmp_path)
    head = (3, entries[2]["hash"])
    assert verify_entries(entries[3:], run_id="r-verify", after=head) == []
    wrong = (3, "sha256:" + "0" * 64)
    assert any("event 4" in p for p in verify_entries(entries[3:], run_id="r-verify", after=wrong))


def test_verify_entries_names_an_edited_entry(tmp_path):
    entries = copy.deepcopy(_ledger(tmp_path))
    entries[2]["outcome"] = "fail"  # edited after hashing
    problems = verify_entries(entries, run_id="r-verify")
    assert any(p.startswith("event 3") and "hash" in p for p in problems), problems


def test_verify_entries_names_a_removed_or_reordered_entry(tmp_path):
    entries = _ledger(tmp_path)
    assert any("event 3" in p for p in verify_entries(entries[:2] + entries[3:], run_id="r-verify"))
    swapped = [entries[0], entries[2], entries[1], *entries[3:]]
    assert verify_entries(swapped, run_id="r-verify")


def test_verify_entries_refuses_unknown_event_type(tmp_path):
    entries = copy.deepcopy(_ledger(tmp_path))
    entries[1]["event"] = "secret_scan"
    entries[1]["hash"] = entry_hash(entries[1])  # rehashed, yet still not a known event
    problems = verify_entries(entries[:2], run_id="r-verify")
    assert any("event 2" in p and "secret_scan" in p for p in problems), problems


def test_verify_entries_refuses_an_undeclared_field(tmp_path):
    entries = copy.deepcopy(_ledger(tmp_path))
    entries[1]["note"] = "free text"
    entries[1]["hash"] = entry_hash(entries[1])
    assert any("note" in p for p in verify_entries(entries[:2], run_id="r-verify"))


def test_verify_entries_refuses_another_runs_entry(tmp_path):
    entries = _ledger(tmp_path)
    problems = verify_entries(entries, run_id="another-run")
    assert any("run" in p for p in problems)


def test_verify_entries_refuses_identity_change(tmp_path):
    entries = copy.deepcopy(_ledger(tmp_path))
    entries[3]["ext"]["agentic"]["team"] = "billing"
    entries[3]["hash"] = entry_hash(entries[3])
    entries[4]["prev"] = entries[3]["hash"]
    entries[4]["hash"] = entry_hash(entries[4])
    entries[5]["prev"] = entries[4]["hash"]
    entries[5]["hash"] = entry_hash(entries[5])
    problems = verify_entries(entries, run_id="r-verify")
    assert any("event 4" in p and "identity" in p for p in problems), problems


def test_verify_entries_checks_identity_against_the_stored_run(tmp_path):
    entries = _ledger(tmp_path)
    head = (3, entries[2]["hash"])
    problems = verify_entries(
        entries[3:], run_id="r-verify", after=head, identity=("billing", POLICY)
    )
    assert any("identity" in p for p in problems)


def test_verify_entries_refuses_a_bad_ext_value(tmp_path):
    entries = copy.deepcopy(_ledger(tmp_path))
    entries[0]["ext"]["agentic"]["policy_version"] = "main"
    entries[0]["hash"] = entry_hash(entries[0])
    assert any("policy_version" in p for p in verify_entries(entries[:1], run_id="r-verify"))


def test_verify_entries_refuses_non_mappings():
    assert verify_entries(["x"], run_id="r") == ["event 1: not an event (a JSON object)"]
