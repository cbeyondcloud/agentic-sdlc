"""Tests for the gates adapter over Zyroflo's approval ledger (SP6 Task 3, Z6)."""

from pathlib import Path

import pytest

import sdlc_commons.ledger as ledger_module
from sdlc_commons.gates import ZyrofloGates, regime_block_on, self_approval_refusal
from sdlc_commons.ledger import RunIdentity, RunLedger

IDENTITY = RunIdentity(team="platform", policy_version="git:" + "a" * 40)
DESIGN = "---\nkind: design\n---\n# Design\n"


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


@pytest.fixture
def run_ledger(tmp_path: Path) -> RunLedger:
    ledger = RunLedger(tmp_path, "r-gates-1")
    ledger.open(
        identity=IDENTITY, kind="feature", project="demo", owner="platform", criticality="low"
    )
    return ledger


def test_an_approval_pins_and_a_change_voids_it(run_ledger):
    digest = run_ledger.write_artifact("handoffs/design-1.md", DESIGN)
    gates = ZyrofloGates(run_ledger)

    entry_hash = gates.record(
        "design-approval",
        "approve",
        "lead@acme.example",
        {"handoffs/design-1.md": digest},
        step="design",
    )
    assert entry_hash.startswith("sha256:")
    assert gates.state("design-approval") == (True, "")
    (run_ledger.run_dir / "handoffs" / "design-1.md").write_text("changed", encoding="utf-8")
    ok, why = gates.state("design-approval")
    assert not ok and "design-1.md" in why


def test_a_gate_with_no_decision_is_not_satisfied(run_ledger):
    ok, why = ZyrofloGates(run_ledger).state("design-approval")

    assert not ok and "no entry" in why


def test_a_reject_over_the_same_bytes_holds_the_gate(run_ledger):
    digest = run_ledger.write_artifact("handoffs/design-1.md", DESIGN)
    gates = ZyrofloGates(run_ledger)
    pins = {"handoffs/design-1.md": digest}
    gates.record(
        "design-approval",
        "reject",
        "lead@acme.example",
        pins,
        step="design",
        notes="use the existing queue",
    )
    gates.record("design-approval", "approve", "other@acme.example", pins, step="design")

    assert gates.state("design-approval")[0] is False


def test_waiting_and_decisions_are_in_the_events_and_the_chain_verifies(run_ledger):
    digest = run_ledger.write_artifact("handoffs/design-1.md", DESIGN)
    gates = ZyrofloGates(run_ledger)
    gates.waiting("design-approval", "design")
    gates.record(
        "design-approval",
        "approve",
        "lead@acme.example",
        {"handoffs/design-1.md": digest},
        step="design",
        extra={"regime": "team"},
    )

    kinds = [(e["event"], e.get("gate")) for e in run_ledger.events()]
    assert ("gate_waiting", "design-approval") in kinds
    assert ("gate_decided", "design-approval") in kinds
    assert run_ledger.verify() == []


def test_regime_block_on_comes_from_zyroflo():
    assert regime_block_on("solo") == frozenset({"critical", "high"})
    assert regime_block_on("team") == frozenset({"critical", "high"})
    assert regime_block_on("regulated") == frozenset({"critical", "high", "medium"})
    with pytest.raises(ValueError, match="regime"):
        regime_block_on("lenient")


def test_self_approval_is_refused_case_insensitively():
    assert self_approval_refusal("Dev@Acme.example", "dev@acme.example")
    assert self_approval_refusal("lead@acme.example", "dev@acme.example") is None
