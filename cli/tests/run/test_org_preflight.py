"""Org runs (M2-21, M2-19, M2-10): preflight registers the run with the control plane's org
checks before any spend, and the engine obeys kill and pause switches while it runs."""

import hashlib
import json
import os
import shutil

import pytest
from _cli_support import FakeGateway, answer, git
from cp_fakes import CP_URL, TOKEN, VERSION, FakeControlPlane

from agentic.policy import tree_digest
from agentic_cli import control_plane, runtime
from agentic_cli.commands import gates
from agentic_cli.config import user_config
from agentic_cli.exit_codes import ExitCode
from agentic_cli.policy_source import follow_active_policy

KEY = "tl-" + "fake-gateway-key-0021"  # gitleaks:allow (not a real key)


@pytest.fixture
def gateway(workspace, monkeypatch) -> FakeGateway:
    fake = FakeGateway({"ba": [answer("requirements")], "architect": [answer("design")]})
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setattr(runtime, "network_transport", fake.transport)
    return fake


def _org_config(tmp_path, url: str | None = CP_URL) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    line = f"control_plane_url: {url}\n" if url else ""
    (home / "config.yaml").write_text(f"org: acme\n{line}", encoding="utf-8")


@pytest.fixture
def org(workspace, tmp_path, monkeypatch) -> FakeControlPlane:
    """The workspace in org mode, signed in to a fake control plane (token from the env)."""
    fake = FakeControlPlane(email="dev@example.invalid")
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    git(workspace, "remote", "add", "origin", "https://github.com/acme/app.git")
    _org_config(tmp_path)
    return fake


def start(cli, *extra: str):
    return cli("--json", "run", "deliver", "--input", "request=CSV export", *extra)


def test_a_run_registers_with_the_org_checks_first(org, gateway, cli, policy_repo):
    result = start(cli)
    assert result.code == ExitCode.PAUSED, result.err  # at its gate, as in standalone
    (body,) = org.registrations
    pipeline = (policy_repo / "pipelines" / "deliver.yaml").read_bytes()
    assert body["run_id"] == result.json()["run_id"]
    assert (body["team"], body["repo"], body["pipeline"]) == (
        "payments",
        "github.com/acme/app",
        "deliver",
    )
    assert body["pipeline_file"] == "pipelines/deliver.yaml"
    assert body["pipeline_sha256"] == "sha256:" + hashlib.sha256(pipeline).hexdigest()
    assert sorted(body["agents"]) == ["architect@2.1.0", "ba@1.0.0"]
    assert body["backends"] == ["native"] and body["dry_run"] is False
    assert body["policy_version"].startswith(("git:", "sha256:"))


def test_registration_sends_the_overlay(org, gateway, cli, policy_repo):
    """M2-23 (D33): the approved hash covers the base and the overlay, so the overlay
    applied for this repository goes to the control plane by path and hash, with the
    version of the pipeline the team gets (M2-15)."""
    rel = "teams/payments/pipelines/overlays/app.yaml"
    overlay = policy_repo / rel
    overlay.parent.mkdir(parents=True)
    overlay.write_bytes(b"pipelines:\n  deliver:\n    max_minutes: 30\n")
    assert start(cli).code == ExitCode.PAUSED
    (body,) = org.registrations
    assert body["overlay_file"] == rel
    assert body["overlay_sha256"] == "sha256:" + hashlib.sha256(overlay.read_bytes()).hexdigest()
    assert body["pipeline_file"] == "pipelines/deliver.yaml"  # the base file, as approved
    assert body["pipeline_version"] == "1.0.0"


def test_registration_without_an_overlay_sends_none(org, gateway, cli):
    assert start(cli).code == ExitCode.PAUSED
    (body,) = org.registrations
    assert body["overlay_file"] is None and body["overlay_sha256"] is None


def test_the_run_budget_is_registered_for_budget_alerts(org, gateway, cli):
    """M2-16: the control plane alerts at 80 % and 100 % of the budget it was told."""
    assert start(cli, "--budget", "12.5").code == ExitCode.PAUSED
    assert org.registrations[0]["budget_usd"] == "12.5"


@pytest.mark.parametrize("check", ["rbac", "killswitch", "policy", "pipeline", "concurrency"])
def test_a_failing_org_check_refuses_the_run_before_any_spend(org, gateway, cli, check):
    org.failing_checks = {check: f"the {check} check says no"}
    result = start(cli)
    assert result.code == ExitCode.INVALID
    assert f"org-{check}" in result.out and f"the {check} check says no" in result.out
    assert gateway.requests == [] and org.registrations == []


def test_an_old_cli_is_refused_with_the_upgrade_message(org, gateway, cli):
    org.min_cli_version = "99.0.0"
    result = start(cli)
    assert result.code == ExitCode.REFUSED
    assert "99.0.0" in result.err and "upgrade" in result.err
    assert gateway.requests == []


def test_a_dry_run_asks_with_dry_run_and_shows_the_checks(org, cli):
    result = cli("--json", "run", "deliver", "--input", "request=x", "--dry-run")
    assert result.code == ExitCode.OK, result.err
    plan = result.json()
    assert {c["check"] for c in plan["org"]["checks"]} >= {"rbac", "killswitch", "budget"}
    assert plan["org"]["team_budget_remaining_usd"] is None  # not measured: null, never 0
    sent = [r for r in org.requests if r.url.path == "/v1/runs"]
    assert len(sent) == 1 and b'"dry_run":true' in sent[0].content.replace(b" ", b"")
    assert org.registrations == []


def test_an_unreachable_control_plane_refuses_the_run(org, gateway, cli):
    org.down = True
    result = start(cli)
    assert result.code == ExitCode.INVALID
    assert "org checks cannot run" in result.out and gateway.requests == []


def test_not_signed_in_refuses_the_run(org, gateway, cli, monkeypatch):
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_TOKEN")
    result = start(cli)
    assert result.code == ExitCode.INVALID
    assert "org checks cannot run" in result.out and gateway.requests == []


def test_org_mode_without_a_control_plane_warns_that_org_checks_were_skipped(
    workspace, tmp_path, cli
):
    _org_config(tmp_path, url=None)
    result = cli("--json", "run", "deliver", "--input", "request=x", "--dry-run")
    assert result.code == ExitCode.OK, result.err
    problems = result.json()["preflight"]["problems"]
    assert any(p["check"] == "org" and p["severity"] == "warning" for p in problems)


def test_a_signed_in_person_cannot_skip_the_org_checks_by_removing_the_url(
    workspace, tmp_path, cli, gateway
):
    """DF-519: someone who signed in to a control plane cannot unset control_plane_url to run
    without RBAC, the kill switch, caps and the minimum version (spec §8.6)."""
    _org_config(tmp_path, url=None)
    control_plane.record_login(tmp_path / "home", CP_URL, "acme", "dev@example.invalid")
    result = start(cli)
    assert result.code == ExitCode.INVALID
    assert "control_plane_url" in result.out and CP_URL in result.out
    assert gateway.requests == []
    dry = cli("--json", "run", "deliver", "--input", "request=x", "--dry-run")
    assert dry.code == ExitCode.INVALID
    (problem,) = [p for p in dry.json()["preflight"]["problems"] if p["check"] == "org"]
    assert problem["severity"] == "error"


def test_a_control_plane_policy_cannot_be_run_without_its_control_plane(
    workspace, tmp_path, cli, gateway, monkeypatch
):
    """DF-519: the org policy cached from a control plane (`fetched.json`) names it, so a
    run on that policy without its control plane is refused too."""
    _org_config(tmp_path, url=None)
    cache = tmp_path / "home" / "policy" / "acme"
    shutil.copytree(workspace.parent / "org-policy", cache / "repo")
    stamp = {
        "source_kind": "control-plane",
        "url": CP_URL,
        "tenant": "acme",
        "tree_digest": tree_digest(cache / "repo"),
    }
    (cache / "fetched.json").write_text(json.dumps(stamp), encoding="utf-8")
    monkeypatch.delenv("AGENTIC_POLICY_REPO")
    dry = cli("--json", "run", "deliver", "--input", "request=x", "--dry-run")
    assert dry.code == ExitCode.INVALID, dry.out
    (problem,) = [p for p in dry.json()["preflight"]["problems"] if p["check"] == "org"]
    assert problem["severity"] == "error" and CP_URL in problem["detail"]


def _approvals(workspace, run_id) -> list[dict]:
    path = workspace / ".agentic" / "local" / "runs" / run_id / "approvals.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def test_an_org_approval_is_checked_and_records_the_role_held(org, gateway, cli, workspace):
    """DF-409: in org mode the gate's role comes from the control plane, and the approval
    ledger records the role the gate needed and the role the approver held."""
    run_id = start(cli).json()["run_id"]
    refused = cli("approve", run_id)  # a developer: the gate needs team-lead
    assert refused.code == ExitCode.REFUSED and "team-lead" in refused.err
    org.assignments = [{"role": "team-lead", "scope": "team", "team": "payments"}]
    approved = cli("--json", "approve", run_id)
    assert approved.code == ExitCode.PAUSED or approved.code == ExitCode.OK, approved.err
    (entry,) = [e for e in _approvals(workspace, run_id) if e["verdict"] == "approve"]
    assert entry["approver"] == "dev@example.invalid"
    assert (entry["required_role"], entry["role_held"]) == ("team-lead", "team-lead")


def test_four_eyes_compares_the_signed_in_person_who_started_and_who_approves(
    org, gateway, cli, policy_repo
):
    """Review F1: in org mode the run's starter is the signed-in person, as the approver
    is, so a git address that differs from the control-plane address cannot approve the
    person's own regulated run."""
    path = policy_repo / "pipelines" / "deliver.yaml"
    text = path.read_text(encoding="utf-8").replace("regime: team", "regime: regulated")
    path.write_bytes(text.encode("utf-8"))
    org.email = "dev@corp.example"  # git user.email is dev@example.invalid
    org.assignments = [{"role": "team-lead", "scope": "team", "team": "payments"}]
    started = start(cli)
    assert started.code == ExitCode.PAUSED, started.err
    assert started.json()["starter"] == "dev@corp.example"
    refused = cli("approve", started.json()["run_id"])
    assert refused.code == ExitCode.REFUSED and "four-eyes" in refused.err


def test_a_run_refused_locally_never_takes_a_concurrency_slot(org, gateway, cli):
    """Review F3: every local refusal comes before the run is registered, so a typo does
    not hold one of the user's concurrency slots for a day."""
    missing = cli("--json", "run", "deliver")  # the pipeline needs --input request=...
    assert missing.code == ExitCode.REFUSED and "missing input" in missing.err
    unknown = start(cli, "--input", "colour=blue")
    assert unknown.code == ExitCode.REFUSED and "unknown input" in unknown.err
    assert org.registrations == []
    first = start(cli, "--run-id", "dup-1")
    assert first.code == ExitCode.PAUSED, first.err
    again = start(cli, "--run-id", "dup-1")
    assert again.code == ExitCode.REFUSED and "already exists" in again.err
    assert len(org.registrations) == 1


def test_a_kill_switch_kills_the_run_and_it_never_resumes(org, gateway, cli):
    org.add_switch("agent", "ba", mode="kill", reason="bad prompt release")
    result = start(cli)
    assert result.code == ExitCode.STOPPED, result.err
    data = result.json()
    assert data["status"] == "killed" and "bad prompt release" in data["end_reason"]
    assert gateway.requests == []
    again = cli("resume", data["run_id"])
    assert again.code == ExitCode.REFUSED and "killed" in again.err


def test_a_pause_switch_holds_the_run_and_resume_continues_once_cleared(org, gateway, cli):
    switch = org.add_switch("team", "payments", mode="pause", reason="maintenance")
    result = start(cli)
    assert result.code == ExitCode.PAUSED, result.err
    data = result.json()
    assert data["pending"]["id"] == "hold" and "maintenance" in data["pending"]["reason"]
    held = cli("--json", "resume", data["run_id"])
    assert held.code == ExitCode.PAUSED and held.json()["pending"]["id"] == "hold"
    switch["active"] = False
    done = cli("--json", "resume", data["run_id"])
    assert done.code == ExitCode.PAUSED, done.err
    assert done.json()["pending"]["id"] == "requirements-approval"
    assert [r["agent"] for r in gateway.requests] == ["ba"]


# ── a paused run keeps the policy it started on (review F3, M2-53) ──────────────────────

NEWER = "git:" + "cd" * 20


def _cached_control_plane_policy(org, workspace, tmp_path, monkeypatch) -> None:
    """The org policy as `agentic init` caches it from the control plane, at VERSION."""
    cache = tmp_path / "home" / "policy" / "acme"
    shutil.copytree(workspace.parent / "org-policy", cache / "repo")
    org.policy_root = workspace.parent / "org-policy"
    stamp = {
        "source_kind": "control-plane",
        "url": CP_URL,
        "tenant": "acme",
        "version": VERSION,
        "tree_digest": tree_digest(cache / "repo"),
    }
    (cache / "fetched.json").write_text(json.dumps(stamp), encoding="utf-8")
    monkeypatch.delenv("AGENTIC_POLICY_REPO")


def _another_run_follows_a_newer_policy(org, tmp_path) -> None:
    """An admin's sync moves the tenant on, and another `agentic run` in the same home
    follows it (M2-53): the cache now holds the newer version."""
    org.version = NEWER
    home = tmp_path / "home"
    assert follow_active_policy(home, user_config(home), os.environ) == (VERSION, NEWER)


@pytest.mark.parametrize("command", ["approve", "reject", "edit"])
def test_a_gate_decision_is_refused_once_the_org_policy_moved_on(
    org, gateway, cli, workspace, tmp_path, monkeypatch, command
):
    """Spec §1.3/§12.1: the run record names the policy the run used. A run paused at a gate
    on one policy version does not carry on under another one (another `agentic run` in the
    same home follows the tenant's active policy) with the old version on its record. An
    edit is refused before the editor opens."""
    _cached_control_plane_policy(org, workspace, tmp_path, monkeypatch)
    monkeypatch.setattr(gates, "edit_file", lambda path: pytest.fail("the editor opened"))
    org.assignments = [{"role": "team-lead", "scope": "team", "team": "payments"}]
    started = start(cli)
    assert started.code == ExitCode.PAUSED, started.err
    run_id = started.json()["run_id"]
    assert started.json()["policy_version"] == VERSION
    _another_run_follows_a_newer_policy(org, tmp_path)
    asked = len(gateway.requests)
    extra = ["--feedback", "not this way"] if command == "reject" else []
    refused = cli(command, run_id, *extra)
    assert refused.code == ExitCode.REFUSED, refused.err
    assert VERSION in refused.err and NEWER in refused.err
    assert f"agentic stop {run_id}" in refused.err
    assert len(gateway.requests) == asked  # no step ran under the new policy
    status = cli("--json", "status", run_id).json()
    assert status["pending"]["id"] == "requirements-approval"
    stopped = cli("--json", "stop", run_id, "--reason", "the org policy changed")
    assert stopped.code == ExitCode.OK, stopped.err
    assert stopped.json()["status"] == "stopped"


def test_resume_is_refused_once_the_org_policy_moved_on(
    org, gateway, cli, workspace, tmp_path, monkeypatch
):
    _cached_control_plane_policy(org, workspace, tmp_path, monkeypatch)
    switch = org.add_switch("team", "payments", mode="pause", reason="maintenance")
    held = start(cli)
    assert held.code == ExitCode.PAUSED, held.err
    switch["active"] = False
    _another_run_follows_a_newer_policy(org, tmp_path)
    refused = cli("resume", held.json()["run_id"])
    assert refused.code == ExitCode.REFUSED, refused.err
    assert VERSION in refused.err and NEWER in refused.err
    assert gateway.requests == []


def test_a_paused_run_on_the_current_policy_continues(
    org, gateway, cli, workspace, tmp_path, monkeypatch
):
    _cached_control_plane_policy(org, workspace, tmp_path, monkeypatch)
    org.assignments = [{"role": "team-lead", "scope": "team", "team": "payments"}]
    run_id = start(cli).json()["run_id"]
    approved = cli("--json", "approve", run_id)
    assert approved.code in (ExitCode.OK, ExitCode.PAUSED), approved.err
    assert [r["agent"] for r in gateway.requests] == ["ba", "architect"]
