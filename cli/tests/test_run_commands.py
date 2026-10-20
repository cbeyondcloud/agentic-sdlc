"""run (incl. --dry-run), resume, status, runs, stop, logs (M1-54). Every model call goes to
a fake TokenLean; nothing reaches a real provider."""

import json

import pytest
from _cli_support import USER, FakeGateway, answer, git, write

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


# ── review fixes (sp14b final review) ────────────────────────────────────────


def committed(repo, rel: str, text: str) -> None:
    """A file in the repository, committed (a run refuses a checkout with changes)."""
    write(repo / rel, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"add {rel}")


def failing_gateway(monkeypatch, body: str) -> None:
    """The gateway answers every call with HTTP 400 and `body` (an echoing error page)."""
    import httpx2

    def handler(request):
        return httpx2.Response(400, text=body)

    monkeypatch.setattr(runtime, "network_transport", lambda: httpx2.MockTransport(handler))


def test_a_provider_error_echoing_the_key_reaches_neither_the_console_nor_run_json(
    workspace, gateway, monkeypatch, cli
):  # F1
    failing_gateway(monkeypatch, f"invalid api key {KEY}")
    result = cli("run", "deliver", "--input", "request=x", "--run-id", "leaky")
    assert result.code == ExitCode.PAUSED, result.err
    assert "step-error" in result.out
    as_json, human = cli("--json", "status", "leaky"), cli("status", "leaky")
    assert as_json.json()["pending"]["id"] == "step-error"
    for shown in (result.out, result.err, as_json.out, as_json.err, human.out, human.err):
        assert KEY not in shown
    run_json = workspace / ".agentic" / "local" / "runs" / "leaky" / "run.json"
    assert KEY not in run_json.read_text(encoding="utf-8")


def test_terminal_control_sequences_in_run_text_are_not_printed(
    workspace, gateway, monkeypatch, cli
):  # F2
    failing_gateway(monkeypatch, "bad request\x1b]0;pwned\x07 \x1b[2K\x1b[1Afake line\x9b2K")
    result = cli("run", "deliver", "--input", "request=x", "--run-id", "noisy")
    assert result.code == ExitCode.PAUSED, result.err
    shown = cli("status", "noisy")
    for text in (result.out, shown.out):
        assert "fake line" in text
        assert not any(c in text for c in "\x1b\x07\x9b"), repr(text)
    as_json = cli("--json", "status", "noisy").out
    assert "\x1b" not in as_json and "\x9b" not in as_json
    assert "fake line" in as_json


SMALL_BA = (
    "---\nname: ba\ntype: analyst\nversion: 1.0.1\ndescription: BA.\n"
    "limits: { budget_usd: 0.01 }\n---\nGo.\n"
)


def test_a_step_budget_stop_does_not_offer_more_run_budget(workspace, gateway, cli):  # F4
    committed(workspace, ".agentic/agents/ba.md", SMALL_BA)
    result, run_id = start(cli)
    assert result.code == ExitCode.STOPPED, result.out + result.err
    assert result.json()["pending"]["id"] == "step-budget"
    human = cli("status", run_id).out
    assert "--extra-budget" not in human and "limits.budget_usd" in human
    spent = len(gateway.requests)

    resumed = cli("resume", run_id, "--extra-budget", "5")
    assert resumed.code == ExitCode.REFUSED, resumed.out + resumed.err
    assert "own budget" in resumed.err
    assert len(gateway.requests) == spent  # nothing re-spent


def test_extra_budget_on_a_run_without_a_budget_is_refused(workspace, gateway, monkeypatch, cli):
    failing_gateway(monkeypatch, "temporarily broken")
    result, run_id = start(cli)
    assert result.json()["pending"]["id"] == "step-error"
    resumed = cli("resume", run_id, "--extra-budget", "5")
    assert resumed.code == ExitCode.REFUSED, resumed.out + resumed.err
    assert "no budget limit" in resumed.err


CONDITIONAL = """name: cond
version: 1.0.0
regime: {regime}
steps:
  - id: requirements
    agent: ba
  - id: design
    agent: architect
    when: "requirements.no_such_field == 1"
"""


def test_resume_skip_records_who_skipped(workspace, gateway, cli):  # F6
    committed(workspace, ".agentic/pipelines/cond.yaml", CONDITIONAL.format(regime="team"))
    result = cli("--json", "run", "cond", "--run-id", "cond-1")
    assert result.json()["pending"]["id"] == "condition", result.out + result.err
    done = cli("--json", "resume", "cond-1", "--skip")
    assert done.code == ExitCode.OK, done.out + done.err
    skipped = [r for r in done.json()["history"] if r["outcome"] == "skipped"]
    assert [r["step"] for r in skipped] == ["design"]
    assert USER in skipped[0]["detail"]
    approvals = workspace / ".agentic" / "local" / "runs" / "cond-1" / "approvals.jsonl"
    entry = json.loads(approvals.read_text(encoding="utf-8").splitlines()[-1])
    assert (entry["gate"], entry["approver"], entry["action"]) == ("skip-design", USER, "skip")


def test_resume_skip_of_your_own_regulated_run_is_refused(workspace, gateway, cli):
    committed(workspace, ".agentic/pipelines/cond.yaml", CONDITIONAL.format(regime="regulated"))
    result = cli("--json", "run", "cond", "--run-id", "cond-2")
    assert result.json()["pending"]["id"] == "condition", result.out + result.err
    refused = cli("resume", "cond-2", "--skip")
    assert refused.code == ExitCode.REFUSED and "four-eyes" in refused.err


def test_the_plan_does_not_claim_roles_are_checked(workspace, policy_repo, gateway, cli):  # F7
    write(
        policy_repo / "pipelines" / "deliver.yaml",
        PIPELINE_FOUR_EYES,
    )
    plan = cli("--json", "run", "deliver", "--dry-run").json()
    gate = plan["steps"][1]
    assert "a person holding" not in gate["who_approves"]
    assert "not checked" in gate["who_approves"]
    assert any(p["check"] == "identity" for p in plan["preflight"]["problems"])
    assert "not authenticated" in cli("run", "deliver", "--dry-run").out


PIPELINE_FOUR_EYES = """name: deliver
version: 1.0.0
regime: team
steps:
  - id: requirements
    agent: ba
  - gate: requirements-approval
    approver_role: team-lead
    four_eyes: true
  - id: design
    agent: architect
"""
