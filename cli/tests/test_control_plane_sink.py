"""The control-plane sink (M2-20): run ledger entries reach the control plane through
`ControlPlaneSink`, in batches, idempotently; a refusal keeps the cursor; a server behind
the cursor is caught up (R21); org-mode runs get the sink, on a background thread (R20);
`agentic sync` replays what stayed queued."""

from __future__ import annotations

import json
from pathlib import Path

import keyring
import pytest
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic import auth
from agentic.telemetry.sinks import SinkBehind, forward
from agentic_cli import control_plane
from agentic_cli.config import agentic_home
from agentic_cli.control_plane import (
    CONTROL_PLANE_TOKEN,
    ChainRejected,
    ControlPlaneClient,
    ControlPlaneSink,
    record_login,
)
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main
from agentic_cli.runtime import Runtime
from agentic_cli.workspace import Workspace
from sdlc_commons import ledger as ledger_module
from sdlc_commons.ledger import RunIdentity, RunLedger

POLICY = "git:" + "c" * 40


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


def chain(n: int) -> list[dict]:
    entries, prev = [], ""
    for seq in range(1, n + 1):
        entry = {"seq": seq, "prev": prev, "hash": f"h{seq}", "run_id": "r"}
        entries.append(entry)
        prev = entry["hash"]
    return entries


@pytest.fixture
def fake():
    return FakeControlPlane()


def _sink(fake, batch=200):
    return ControlPlaneSink(ControlPlaneClient(CP_URL, TOKEN, transport=fake.transport), batch)


def test_sink_delivers_entries_via_ingest(fake):
    _sink(fake).deliver("r", chain(3))
    assert [e["seq"] for e in fake.events["r"]] == [1, 2, 3]
    assert ControlPlaneSink.name == "control-plane"


def test_sink_sends_in_batches(fake):
    _sink(fake, batch=2).deliver("r", chain(5))
    posts = [r for r in fake.requests if r.url.path == "/v1/runs/r/events"]
    assert [len(json.loads(r.content)["entries"]) for r in posts] == [2, 2, 1]


def test_sink_raises_on_chain_rejection_so_the_cursor_stays(fake, tmp_path):
    fake.refuse_ingest = {
        "status": 422,
        "title": "x",
        "detail": "broken",
        "code": "ingest.chain_broken",
        "problems": ["event 1: hash"],
    }
    cursor = tmp_path / "control-plane.json"
    with pytest.raises(ChainRejected):
        forward("r", chain(2), _sink(fake), cursor)
    assert not cursor.exists()


def test_sink_gap_becomes_sink_behind_and_is_caught_up(fake, tmp_path):
    cursor = tmp_path / "control-plane.json"
    entries = chain(4)
    forward("r", entries[:3], _sink(fake), cursor)
    fake.events["r"] = fake.events["r"][:1]  # the control plane lost events 2 and 3
    with pytest.raises(SinkBehind) as caught:
        _sink(fake).deliver("r", entries[3:])
    assert (caught.value.seq, caught.value.hash) == (1, "h1")
    assert forward("r", entries, _sink(fake), cursor) == 3
    assert [e["seq"] for e in fake.events["r"]] == [1, 2, 3, 4]


@pytest.fixture
def org(home, repo, monkeypatch, fake):
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    for name in ("AGENTIC_CONTROL_PLANE_URL", "AGENTIC_CONTROL_PLANE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    config = agentic_home()
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text(
        f"org: acme\ncontrol_plane_url: {CP_URL}\nteam: payments\n", encoding="utf-8"
    )
    auth.Credentials().store(CONTROL_PLANE_TOKEN, TOKEN)
    record_login(config, CP_URL, "acme", "dev@acme.example")
    return repo


def test_runtime_wires_the_sink_in_org_mode_with_a_control_plane(org):
    rt = Runtime(Workspace.load(org, home=agentic_home()))
    sinks = rt.sinks()
    assert [s.name for s in sinks] == ["control-plane"] and rt.sink_problem is None


def test_not_signed_in_runs_without_the_sink_and_says_so(org):
    keyring.get_keyring().stored.clear()
    rt = Runtime(Workspace.load(org, home=agentic_home()))
    assert rt.sinks() == []
    assert "agentic login --for control-plane" in rt.sink_problem


def test_standalone_runs_have_no_sink(home, repo, monkeypatch):
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_URL", CP_URL)
    rt = Runtime(Workspace.load(repo, home=agentic_home()))
    assert rt.sinks() == [] and rt.sink_problem is None


def _ledger_run(repo: Path, run_id: str, events: int = 3) -> list[dict]:
    ledger = RunLedger(repo, run_id)
    ledger.open(
        identity=RunIdentity(team="payments", policy_version=POLICY),
        kind="feature",
        project="app",
        owner="payments",
        criticality="low",
    )
    for n in range(events):
        ledger.record("step_started", phase="requirements", attempt=n + 1)
    entries = ledger.events()
    ledger.release()
    return entries


def test_sync_command_delivers_queued_runs_once(org, fake, monkeypatch, capsys):
    entries = _ledger_run(org, "run-offline")
    monkeypatch.chdir(org)
    assert main(["--json", "sync"]) == ExitCode.OK
    out = json.loads(capsys.readouterr().out)
    assert out["runs"]["run-offline"] == {"sent": 4, "queued": 0}
    assert [e["hash"] for e in fake.events["run-offline"]] == [e["hash"] for e in entries]
    assert main(["--json", "sync", "run-offline"]) == ExitCode.OK
    assert json.loads(capsys.readouterr().out)["runs"]["run-offline"] == {"sent": 0, "queued": 0}


def test_sync_reports_a_run_that_stays_queued(org, fake, monkeypatch, capsys):
    _ledger_run(org, "run-down")
    fake.down = True
    monkeypatch.chdir(org)
    assert main(["--json", "sync"]) == ExitCode.ERROR
    out = json.loads(capsys.readouterr().out)
    assert out["runs"]["run-down"]["queued"] == 4 and "error" in out["runs"]["run-down"]


def test_sync_without_a_control_plane_is_invalid(home, repo, monkeypatch, capsys):
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    monkeypatch.chdir(repo)
    assert main(["--json", "sync"]) == ExitCode.INVALID
    assert "control_plane_url" in json.loads(capsys.readouterr().err)["hint"]
