"""`agentic resume <run> --add-budget <usd>` (DF-357, owner decision 2026-11-07): a step
paused at its own budget gets more, clamped and said so; a bare resume of such a pause
prints the command instead of retrying with the same budget."""

import pytest
from _cli_support import USER, FakeGateway, answer, git, write

from agentic_cli import runtime
from agentic_cli.exit_codes import ExitCode

KEY = "tl-" + "fake-gateway-key-0004"  # gitleaks:allow (not a real key)
SMALL_BA = (
    "---\nname: ba\ntype: analyst\nversion: 1.0.1\ndescription: BA.\n"
    "limits: { budget_usd: 0.01 }\n---\nGo.\n"
)


@pytest.fixture
def gateway(workspace, monkeypatch) -> FakeGateway:
    fake = FakeGateway({"ba": [answer("requirements")], "architect": [answer("design")]})
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setattr(runtime, "network_transport", fake.transport)
    return fake


@pytest.fixture
def paused(workspace, gateway, cli) -> str:
    """A run whose BA step (0.01 USD of its type's 2) paused at its own budget."""
    write(workspace / ".agentic" / "agents" / "ba.md", SMALL_BA)
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "a small BA")
    result = cli("--json", "run", "deliver", "--input", "request=CSV export")
    assert result.code == ExitCode.STOPPED, result.out + result.err
    assert result.json()["pending"]["id"] == "step-budget"
    return result.json()["run_id"]


def test_a_bare_resume_of_a_step_budget_pause_prints_the_flag(paused, gateway, cli):
    spent = len(gateway.requests)
    result = cli("resume", paused)
    assert result.code == ExitCode.REFUSED, result.out + result.err
    err = " ".join(result.err.split())
    assert f"agentic resume {paused} --add-budget <USD>" in err
    assert len(gateway.requests) == spent  # not retried with the same slice


def test_add_budget_resumes_the_step_and_records_who(paused, gateway, cli):
    result = cli("--json", "resume", paused, "--add-budget", "0.5")
    assert result.code == ExitCode.PAUSED, result.out + result.err  # at the next gate
    shown = cli("--json", "status", paused).json()
    assert shown["pending"]["id"] == "requirements-approval"
    (grant,) = shown["budget_grants"]
    assert grant["by"] == USER and grant["granted_usd"] == "0.5"
    assert grant["limited_by"] is None


def test_a_clamped_grant_says_so(paused, gateway, cli):
    result = cli("resume", paused, "--add-budget", "50")
    assert result.code == ExitCode.PAUSED, result.out + result.err
    out = " ".join((result.out + result.err).split())
    assert "granted 1.99 of 50 USD" in out and "policy cap" in out


def test_status_offers_the_flag_for_a_step_budget_pause(paused, cli):
    human = " ".join(cli("status", paused).out.split())
    assert f"agentic resume {paused} --add-budget <USD>" in human


def test_add_budget_is_refused_for_another_pause(workspace, gateway, cli):
    result = cli("--json", "run", "deliver", "--input", "request=x")
    run_id = result.json()["run_id"]  # waits at its gate
    resumed = cli("resume", run_id, "--add-budget", "1")
    assert resumed.code == ExitCode.REFUSED


def test_add_budget_is_an_amount(paused, cli):
    assert cli("resume", paused, "--add-budget", "lots").code == ExitCode.USAGE
