"""The heartbeat wired into the commands that drive a run (M3-07, Review Focus 4): `run`,
`resume` and the gate verbs beat while the engine works - into the run folder always, to
the control plane in org mode - and a command that leaves the run waiting sends one
`waiting` beat last. A control plane that refuses beats never changes how the run goes."""

import json

import pytest
from _cli_support import FakeGateway, answer, git
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic_cli import control_plane, runtime
from agentic_cli.exit_codes import ExitCode
from agentic_cli.heartbeat import HEARTBEAT_FILE
from agentic_cli.runtime import Runtime
from agentic_cli.workspace import Workspace

KEY = "tl-" + "fake-gateway-key-0707"  # gitleaks:allow (not a real key)


@pytest.fixture
def gateway(workspace, monkeypatch) -> FakeGateway:
    fake = FakeGateway({"ba": [answer("requirements")], "architect": [answer("design")]})
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setattr(runtime, "network_transport", fake.transport)
    return fake


@pytest.fixture
def org(workspace, tmp_path, monkeypatch) -> FakeControlPlane:
    fake = FakeControlPlane(email="dev@example.invalid")
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    git(workspace, "remote", "add", "origin", "https://github.com/acme/app.git")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(f"org: acme\ncontrol_plane_url: {CP_URL}\n", "utf-8")
    return fake


def start(cli):
    result = cli("--json", "run", "deliver", "--input", "request=CSV export")
    assert result.code == ExitCode.PAUSED, result.err  # at its first gate
    return result.json()["run_id"]


def beat_file(workspace, run_id) -> dict:
    path = workspace / ".agentic" / "local" / "runs" / run_id / HEARTBEAT_FILE
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_standalone_run_beats_into_its_folder(workspace, gateway, cli):
    run_id = start(cli)
    written = beat_file(workspace, run_id)
    assert written["run_id"] == run_id and written["state"] == "waiting"
    assert written["interval_s"] == 60  # the policy's run_health default


def test_an_org_run_beats_to_the_control_plane_and_ends_waiting(org, gateway, cli):
    org.assignments = [{"role": "team-lead", "scope": "team", "team": "payments"}]
    run_id = start(cli)
    states = [b["state"] for b in org.heartbeats if b["run_id"] == run_id]
    assert states[0] == "running" and states[-1] == "waiting"
    approved = cli("--json", "approve", run_id)  # a gate decision drives the run too
    assert approved.code in (ExitCode.OK, ExitCode.PAUSED), approved.err
    states = [b["state"] for b in org.heartbeats if b["run_id"] == run_id]
    assert states.count("waiting") == (1 if approved.code == ExitCode.OK else 2)
    assert "running" in states[states.index("waiting") + 1 :]


def test_a_refused_approve_leaves_the_run_waiting(org, gateway, cli):
    """Review F1: the command's first beat is `running`; a decision the engine refuses
    (here: the person lost the role) still ends with `waiting`, or the control plane would
    sweep a run that is only waiting at its gate."""
    org.assignments = [{"role": "team-lead", "scope": "team", "team": "payments"}]
    run_id = start(cli)
    before = len(org.heartbeats)
    org.assignments = []
    refused = cli("--json", "approve", run_id)
    assert refused.code == ExitCode.REFUSED, refused.err
    assert cli("--json", "status", run_id).json()["status"] == "awaiting_gate"
    states = [b["state"] for b in org.heartbeats[before:] if b["run_id"] == run_id]
    assert states and states[-1] == "waiting"


def test_a_refused_resume_at_a_gate_leaves_the_run_waiting(org, gateway, cli):
    org.assignments = [{"role": "team-lead", "scope": "team", "team": "payments"}]
    run_id = start(cli)
    before = len(org.heartbeats)
    refused = cli("--json", "resume", run_id)  # a gate is decided, not resumed
    assert refused.code != ExitCode.OK
    assert cli("--json", "status", run_id).json()["status"] == "awaiting_gate"
    states = [b["state"] for b in org.heartbeats[before:] if b["run_id"] == run_id]
    assert states and states[-1] == "waiting"


def test_refused_heartbeats_never_change_the_run(org, gateway, cli):
    org.refuse_heartbeats = True
    run_id = start(cli)
    assert [b["run_id"] for b in org.heartbeats][:1] == [run_id]
    assert cli("--json", "status", run_id).json()["status"] == "awaiting_gate"


def test_resume_beats_too(org, gateway, cli):
    switch = org.add_switch("team", "payments", mode="pause", reason="maintenance")
    held = cli("--json", "run", "deliver", "--input", "request=CSV export")
    assert held.code == ExitCode.PAUSED, held.err
    run_id = held.json()["run_id"]
    before = len(org.heartbeats)
    switch["active"] = False
    resumed = cli("--json", "resume", run_id)
    assert resumed.code == ExitCode.PAUSED, resumed.err  # on to its gate
    assert [b["state"] for b in org.heartbeats[before:]][-1] == "waiting"
    assert "running" in [b["state"] for b in org.heartbeats[before:]]


def test_the_beat_interval_comes_from_the_policy(workspace, policy_repo):
    org_yaml = policy_repo / "policy" / "org.yaml"
    org_yaml.write_text(
        org_yaml.read_text(encoding="utf-8") + "run_health:\n  heartbeat_seconds: 20\n", "utf-8"
    )
    beats = Runtime(Workspace.load()).heartbeat("run-x")
    assert beats.interval_s == 20 and beats.poster is None  # standalone: no control plane
