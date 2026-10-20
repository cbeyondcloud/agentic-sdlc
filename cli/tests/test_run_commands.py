"""run (incl. --dry-run), resume, status, runs, stop, logs (M1-54). Every model call goes to
a fake TokenLean; nothing reaches a real provider."""

import pytest
from _cli_support import FakeGateway, answer, write

from agentic_cli import runtime
from agentic_cli.exit_codes import ExitCode

KEY = "tl-" + "fake-gateway-key-0002"  # gitleaks:allow (not a real key)


@pytest.fixture
def gateway(workspace, monkeypatch) -> FakeGateway:
    fake = FakeGateway({"ba": [answer("requirements")], "architect": [answer("design")]})
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setattr(runtime, "network_transport", fake.transport)
    return fake


def start(cli, *extra: str):
    result = cli("--json", "run", "deliver", "--input", "request=CSV export", *extra)
    return result, (result.json()["run_id"] if result.out.strip() else None)


def test_dry_run_spends_nothing_and_prints_the_resolved_plan(workspace, gateway, cli):
    result = cli("--json", "run", "deliver", "--input", "request=x", "--dry-run")
    assert result.code == ExitCode.OK, result.err
    plan = result.json()
    assert plan["preflight"]["passed"] and plan["regime"] == "team"
    ba, gate, design = plan["steps"]
    assert (ba["agent"], ba["tier"], ba["model"], ba["backend"]) == (
        "ba",
        "fast",
        "acme/model-f",
        "native",
    )
    assert ba["limits"] == {"budget_usd": 2.0, "max_turns": 10, "max_minutes": 10, "children": 0}
    assert gate["kind"] == "gate" and "team-lead" in gate["who_approves"]
    assert design["model"] == "acme/model-b"
    assert gateway.requests == []  # nothing spent
    assert not (workspace / ".agentic" / "local" / "runs").exists()  # nothing written
    assert not (workspace / ".sdlc-worktrees").exists()

    human = cli("run", "deliver", "--input", "request=x", "--dry-run")
    assert human.code == ExitCode.OK
    for shown in ("acme/model-f", "fast", "team-lead", "budget $2", "preflight passed"):
        assert shown in human.out, shown


def test_dry_run_with_a_preflight_problem_exits_3_with_the_report(workspace, policy_repo, cli):
    write(
        policy_repo / "pipelines" / "broken.yaml",
        "name: broken\nversion: 1.0.0\nsteps:\n  - id: one\n    agent: nobody\n",
    )
    result = cli("run", "broken", "--dry-run")
    assert result.code == ExitCode.INVALID
    assert "no agent named 'nobody'" in result.out


def test_a_run_pauses_at_its_gate_and_exits_4(workspace, gateway, cli):
    result, run_id = start(cli)
    assert result.code == ExitCode.PAUSED, result.err
    assert result.json()["status"] == "awaiting_gate"
    assert result.json()["pending"]["id"] == "requirements-approval"

    status = cli("--json", "status", run_id)
    assert status.code == ExitCode.OK
    shown = status.json()
    assert shown["pending"]["reason"].startswith("waiting for approval by team-lead")
    assert shown["history"][0]["step"] == "requirements"
    assert shown["cost_usd"] == gateway.cost_usd
    human = cli("status", run_id).out
    assert "requirements-approval" in human and "agentic approve" in human

    runs = cli("--json", "runs").json()
    assert [r["run_id"] for r in runs] == [run_id]


def test_a_budget_stop_exits_5_and_resume_continues_the_paused_run(workspace, gateway, cli):
    result, run_id = start(cli, "--budget", "0.01")
    assert result.code == ExitCode.STOPPED, result.out + result.err
    assert result.json()["pending"]["id"] == "budget"

    resumed = cli("--json", "resume", run_id, "--extra-budget", "1")
    assert resumed.code == ExitCode.PAUSED, resumed.err
    assert resumed.json()["pending"]["id"] == "requirements-approval"
    assert [r["agent"] for r in gateway.requests] == ["ba"]


def test_resume_of_a_gate_is_refused_with_a_hint(workspace, gateway, cli):
    _, run_id = start(cli)
    result = cli("resume", run_id)
    assert result.code == ExitCode.REFUSED and "approve" in result.err


def test_an_undeclared_input_is_refused(workspace, gateway, cli):
    result = cli("run", "deliver", "--input", "request=x", "--input", "colour=red")
    assert result.code == ExitCode.REFUSED and "colour" in result.err


def test_a_malformed_input_is_a_usage_error(workspace, gateway, cli):
    assert cli("run", "deliver", "--input", "request").code == ExitCode.USAGE


def test_a_live_run_without_a_gateway_is_refused(workspace, cli):
    result = cli("run", "deliver", "--input", "request=x")
    assert result.code == ExitCode.REFUSED and "gateway" in result.err


def test_stop_ends_the_run_and_runs_hides_it(workspace, gateway, cli):
    _, run_id = start(cli)
    stopped = cli("--json", "stop", run_id, "--reason", "not needed")
    assert stopped.code == ExitCode.OK and stopped.json()["status"] == "stopped"
    assert cli("--json", "runs").json() == []
    assert [r["run_id"] for r in cli("--json", "runs", "--all").json()] == [run_id]
    assert cli("resume", run_id).code == ExitCode.STOPPED


def test_logs_show_the_step_log(workspace, gateway, cli):
    _, run_id = start(cli)
    result = cli("logs", run_id, "--step", "requirements")
    assert result.code == ExitCode.OK and "step started" in result.out
    records = cli("--json", "logs", run_id).json()
    assert {r["step"] for r in records} == {"requirements"}


def test_status_of_an_unknown_run_is_an_error_with_a_hint(workspace, cli):
    result = cli("status", "no-such-run")
    assert result.code == ExitCode.ERROR and "agentic runs" in result.err


def test_status_without_a_run_lists_recent_runs(workspace, gateway, cli):
    _, run_id = start(cli)
    assert run_id in cli("status").out


def test_dry_run_names_deny_globs_the_os_sandbox_cannot_enforce(
    workspace, policy_repo, monkeypatch, cli
):
    from agentic.sandbox.readiness import Readiness

    monkeypatch.setattr(runtime, "machine_readiness", lambda: Readiness("linux", ()))
    write(
        policy_repo / "agents" / "ba.md",
        "---\nname: ba\ntype: analyst\nversion: 1.0.0\ndescription: BA.\n"
        'deny: { paths: ["**/*.pem", "secrets/**"] }\n---\nGo.\n',
    )
    plan = cli("--json", "run", "deliver", "--dry-run").json()
    assert plan["sandbox"].startswith("srt")
    assert plan["steps"][0]["unenforced_deny_paths"] == ["**/*.pem"]  # secrets/** is bound
    assert "file tools only" in cli("run", "deliver", "--dry-run").out
