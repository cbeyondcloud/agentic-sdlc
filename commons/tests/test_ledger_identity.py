"""Tests for the framework's ids on ledger events (M1-07, Zyroflo Z1 `ext`)."""

import json
from pathlib import Path

import pytest
from tools.sdlc import events

from sdlc_commons import ledger as ledger_module
from sdlc_commons.ledger import AgentIdentity, LedgerError, RunIdentity, RunLedger

POLICY = "git:" + "a" * 40
PAYMENTS = RunIdentity(team="payments", policy_version=POLICY)
DEVELOPER = AgentIdentity(
    name="developer",
    version="1.2.0",
    tier="balanced",
    skills=("superpowers/test-driven-development",),
)


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


def open_ledger(target: Path, run_id: str = "r-id-1", identity=PAYMENTS) -> RunLedger:
    ledger = RunLedger(target, run_id)
    ledger.open(identity=identity, kind="feature", project="demo", owner="platform",
                criticality="low")  # fmt: skip
    return ledger


def agentic(event: dict) -> dict:
    return event.get("ext", {}).get("agentic", {})


def test_run_identity_rides_every_event_and_the_chain_verifies(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    ledger.record("step_started", phase="requirements", attempt=1, actor="agent")

    for event in ledger.events():
        assert agentic(event) == {"team": "payments", "policy_version": POLICY}
    assert ledger.verify() == []


def test_agent_identity_is_added_to_that_event_only(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    ledger.record("step_started", phase="build", agent=DEVELOPER, attempt=1, actor="agent")

    opened, started = ledger.events()
    assert "agent" not in agentic(opened)
    assert agentic(started) == {
        "team": "payments",
        "policy_version": POLICY,
        "agent": "developer@1.2.0",
        "tier": "balanced",
        "skills": ["superpowers/test-driven-development"],
    }


def test_a_child_agent_names_its_parent(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    child = AgentIdentity(name="test-writer", version="0.1.0", parent="developer@1.2.0")
    ledger.record("step_started", phase="build", agent=child, attempt=1, actor="agent")

    assert agentic(ledger.events()[-1])["parent"] == "developer@1.2.0"


def test_editing_an_id_breaks_the_chain(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    log = ledger.folder / "events.jsonl"
    record = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    record["ext"]["agentic"]["team"] = "platform"
    log.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

    assert any("event 1 (run_opened)" in problem for problem in ledger.verify())


def test_events_zyroflo_writes_itself_carry_the_run_identity(tmp_path: Path):
    # gates (approve.record) and Zyroflo's own moves append without our adapter
    open_ledger(tmp_path)
    written = events.append(tmp_path, "r-id-1", "run_closed", status="closed")

    assert agentic(written) == {"team": "payments", "policy_version": POLICY}


def test_each_run_gets_its_own_identity(tmp_path: Path):
    open_ledger(tmp_path, "r-pay")
    platform = RunIdentity(team="platform", policy_version="sha256:" + "b" * 64)
    other = open_ledger(tmp_path, "r-plat", identity=platform)

    assert agentic(other.events()[0])["team"] == "platform"
    assert agentic(RunLedger(tmp_path, "r-pay").events()[0])["team"] == "payments"


# ── the identity is required, kept and never changed (I4) ─────────────────────


def test_nothing_is_recorded_for_a_run_that_was_neither_opened_nor_resumed(tmp_path: Path):
    open_ledger(tmp_path)
    ledger_module._IDENTITIES.clear()  # a new process

    with pytest.raises(LedgerError, match="resume"):
        RunLedger(tmp_path, "r-id-1").record("step_started", phase="x", attempt=1, actor="agent")


def test_a_run_resumed_in_a_new_process_keeps_its_identity(tmp_path: Path):
    open_ledger(tmp_path)
    ledger_module._IDENTITIES.clear()  # a new process

    resumed = RunLedger(tmp_path, "r-id-1")
    assert resumed.resume() == PAYMENTS
    resumed.record("step_started", phase="build", attempt=1, actor="agent")
    assert agentic(resumed.events()[-1])["team"] == "payments"
    assert resumed.verify() == []


def test_a_different_identity_for_a_run_is_refused(tmp_path: Path):
    open_ledger(tmp_path)
    ledger_module._IDENTITIES.clear()
    platform = RunIdentity(team="platform", policy_version=POLICY)

    with pytest.raises(LedgerError, match="already belongs"):
        RunLedger(tmp_path, "r-id-1").open(identity=platform, kind="feature", project="demo",
                                           owner="platform", criticality="low")  # fmt: skip


def test_verify_flags_events_whose_identity_differs_from_the_run(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    ledger_module._IDENTITIES["r-id-1"] = RunIdentity(team="platform", policy_version=POLICY)
    events.append(tmp_path, "r-id-1", "run_closed", status="closed")  # written as another team

    assert any("event 2" in p and "identity" in p for p in ledger.verify())


def test_release_forgets_the_identity_once_the_run_is_done(tmp_path: Path):
    ledger = open_ledger(tmp_path)
    ledger.release()

    assert "r-id-1" not in ledger_module._IDENTITIES


# ── bad ids are refused before anything is written ───────────────────────────


@pytest.mark.parametrize(
    "identity",
    [
        RunIdentity(team="Payments Team", policy_version=POLICY),
        RunIdentity(team="payments", policy_version="latest"),
        RunIdentity(team="payments", policy_version="git:abc"),
    ],
    ids=["team-with-spaces", "policy-not-a-version", "short-commit"],
)
def test_a_bad_run_identity_is_refused_before_anything_is_written(tmp_path: Path, identity):
    with pytest.raises(LedgerError):
        open_ledger(tmp_path, "r-bad", identity=identity)

    assert not (tmp_path / ".agentic" / "local" / "runs" / "r-bad").exists()
    assert "r-bad" not in ledger_module._IDENTITIES


@pytest.mark.parametrize(
    "agent",
    [
        AgentIdentity(name="Developer Agent", version="1.2.0"),
        AgentIdentity(name="developer", version="1.2 beta"),
        AgentIdentity(name="developer", version="1.2.0", skills=("a skill",)),
        AgentIdentity(name="developer", version="1.2.0", parent="someone"),
    ],
    ids=["name-with-spaces", "version-with-space", "skill-with-space", "parent-without-version"],
)
def test_a_bad_agent_identity_is_refused_and_nothing_is_appended(tmp_path: Path, agent):
    ledger = open_ledger(tmp_path)

    with pytest.raises(LedgerError):
        ledger.record("step_started", phase="build", agent=agent, attempt=1, actor="agent")

    assert [e["event"] for e in ledger.events()] == ["run_opened"]
