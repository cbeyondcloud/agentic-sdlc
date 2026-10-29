"""A run's ledger records a policy change mid-run (DF-697 remainder, owner decision 105): a
standalone or `--policy` run resumed after its owner edited the policy carries on, and the
first event after the resume names the old and the new version (`policy_from`,
`policy_to`). The run's identity (the version it started on) never changes."""

from pathlib import Path

import pytest

import sdlc_commons.ledger as ledger_module
from sdlc_commons.ledger import LedgerError, RunIdentity, RunLedger

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


@pytest.fixture
def run_ledger(tmp_path: Path) -> RunLedger:
    ledger = RunLedger(tmp_path, "r-policy-1")
    ledger.open(
        identity=RunIdentity(team="standalone", policy_version=OLD),
        kind="feature",
        project="demo",
        owner="standalone",
        criticality="low",
    )
    return ledger


def test_a_policy_change_is_recorded_with_both_versions(run_ledger):
    run_ledger.record("step_started", "build", ext_ids={"policy_from": OLD, "policy_to": NEW})
    [changed] = [e for e in run_ledger.events() if e["event"] == "step_started"]
    block = changed["ext"]["agentic"]
    assert (block["policy_from"], block["policy_to"]) == (OLD, NEW)
    assert block["policy_version"] == OLD  # the identity stays the version it started on
    assert changed["step"] == "build"  # at which step the change took effect
    assert run_ledger.verify() == []


@pytest.mark.parametrize("bad", ["latest", "git:abc", "sha256:" + "z" * 64])
def test_only_a_policy_version_can_be_recorded(run_ledger, bad):
    with pytest.raises(LedgerError):
        run_ledger.record("step_started", "build", ext_ids={"policy_from": OLD, "policy_to": bad})
