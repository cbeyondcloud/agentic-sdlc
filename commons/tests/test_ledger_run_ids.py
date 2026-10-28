"""Listing a repository's runs, active and archived, and their chain files (M2-26)."""

import shutil
from pathlib import Path

from sdlc_commons.ledger import RunIdentity, RunLedger, run_ids

IDENTITY = RunIdentity(team="platform", policy_version="git:" + "a" * 40)


def _open(target: Path, run_id: str) -> RunLedger:
    ledger = RunLedger(target, run_id)
    ledger.open(
        identity=IDENTITY, kind="feature", project="demo", owner="platform", criticality="low"
    )
    ledger.release()
    return ledger


def test_run_ids_lists_active_and_archived(tmp_path):
    assert run_ids(tmp_path) == []
    _open(tmp_path, "r-b")
    archived = _open(tmp_path, "r-a")
    destination = archived.folder.parent / "archive" / "r-a"
    destination.parent.mkdir(parents=True)
    shutil.move(str(archived.folder), destination)
    (archived.folder.parent / "not-a-run").mkdir()
    assert run_ids(tmp_path) == ["r-a", "r-b"]
    assert RunLedger(tmp_path, "r-a").folder == destination


def test_chain_files_names_events_and_approvals(tmp_path):
    ledger = _open(tmp_path, "r-1")
    files = ledger.chain_files()
    assert [path.name for path in files] == ["events.jsonl", "approvals.jsonl"]
    assert files[0].is_file() and files[0].parent == ledger.folder
