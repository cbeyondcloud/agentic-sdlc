"""`entry_hash` is Zyroflo's ledger formula, exposed for chains kept outside a run folder
(the control plane's audit table, SP15): one verifier checks both chains."""

import hashlib
import json
from pathlib import Path

import pytest

from sdlc_commons import ledger as ledger_module
from sdlc_commons.ledger import RunIdentity, RunLedger, entry_hash


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


def test_entry_hash_is_zyroflos(tmp_path: Path):
    ledger = RunLedger(tmp_path, "r-entry-hash")
    ledger.open(
        identity=RunIdentity(team="platform", policy_version="git:" + "c" * 40),
        kind="feature",
        project="demo",
        owner="platform",
        criticality="low",
    )
    ledger.record("step_started", phase="requirements", attempt=1)
    for event in ledger.events():
        assert entry_hash({k: v for k, v in event.items() if k != "hash"}) == event["hash"]
        assert entry_hash(event) == event["hash"]  # the hash field itself is ignored


def test_entry_hash_is_canonical_json_sha256():
    entry = {"seq": 1, "event": "x", "after": {"b": 2, "a": "é"}, "prev": ""}
    blob = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert entry_hash(entry) == "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
