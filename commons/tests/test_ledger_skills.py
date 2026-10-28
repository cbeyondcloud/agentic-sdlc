"""Skill activations ride the run ledger (M2-45, M1-07): `RunLedger.record(skills=...)`
puts them in the event's `ext.agentic.skills`, inside the hash chain, so the control plane
receives them with the event and verifies them like any other id."""

import pytest

from sdlc_commons.ledger import AgentIdentity, LedgerError, RunIdentity, RunLedger, verify_entries

IDENTITY = RunIdentity(team="platform", policy_version="git:" + "a" * 40)
ENTRY = "personal/habits/2/" + "b" * 64


def opened(tmp_path, run_id="r-skills-1") -> RunLedger:
    ledger = RunLedger(tmp_path, run_id)
    ledger.open(
        identity=IDENTITY, kind="feature", project="demo", owner="platform", criticality="low"
    )
    return ledger


def test_skills_ride_ext_and_verify(tmp_path):
    ledger = opened(tmp_path)
    ledger.record("step_finished", phase="develop", outcome="completed", skills=[ENTRY])
    last = ledger.events()[-1]
    assert last["ext"]["agentic"]["skills"] == [ENTRY]
    assert last["ext"]["agentic"]["team"] == "platform"  # the run identity is kept
    assert ledger.verify() == []
    assert verify_entries(ledger.events(), run_id="r-skills-1") == []


def test_skills_join_an_agents_own(tmp_path):
    ledger = opened(tmp_path)
    agent = AgentIdentity("developer", "1.0.0", skills=("shared/acme/tdd/1.0/" + "c" * 64,))
    ledger.record("step_finished", phase="develop", agent=agent, skills=[ENTRY])
    block = ledger.events()[-1]["ext"]["agentic"]
    assert block["agent"] == "developer@1.0.0"
    assert block["skills"] == ["shared/acme/tdd/1.0/" + "c" * 64, ENTRY]


def test_no_skills_adds_nothing(tmp_path):
    ledger = opened(tmp_path)
    ledger.record("step_finished", phase="develop", outcome="completed")
    assert "skills" not in ledger.events()[-1]["ext"]["agentic"]


def test_a_malformed_entry_is_refused(tmp_path):
    ledger = opened(tmp_path)
    with pytest.raises(LedgerError):
        ledger.record("step_finished", phase="develop", skills=["Personal Skill"])
