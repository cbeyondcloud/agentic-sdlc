"""Preflight refuses a run whose Developer or QA agent may not run the repo's test command
(DF-359, owner decision 2026-11-07): before any spend, naming the step, the agent, the
command and the allow-list line to add, and where. Deny still beats allow."""

import pytest
from _cli_support import write

from agentic_cli.exit_codes import ExitCode

BUILDER = """
  builder:
    description: Implements a design.
    tier: fast
    backends: [native]
    tools: [read_files, edit_files, run_shell]
    shell: ["go test", "git status"]
    consumes: []
    produces: dev-report
    limits:
      max: { budget_usd: 1, max_turns: 10, max_minutes: 10 }
      children_max: { max_total: 0, max_concurrent: 0, max_depth: 0,
                      per_child: { budget_usd: 0, max_turns: 0, max_minutes: 0 } }
"""

PIPELINE = """name: build
version: 1.0.0
regime: team
steps:
  - id: implement
    agent: dev
"""


@pytest.fixture
def build(policy_repo):
    types = policy_repo / "policy" / "agent-types.yaml"
    write(types, types.read_text(encoding="utf-8") + BUILDER)
    write(policy_repo / "pipelines" / "build.yaml", PIPELINE)

    def agent(extra: str = "") -> None:
        text = "name: dev\ntype: builder\nversion: 1.0.0\ndescription: Builds.\n" + extra
        write(policy_repo / "agents" / "dev.md", f"---\n{text}---\nYou build.\n")

    agent()
    return agent


def dry_run(cli):
    return cli("run", "build", "--dry-run")


def test_a_test_command_the_agent_may_run_passes(workspace, build, cli):
    write(workspace / "go.mod", "module example.com/x\n")
    result = dry_run(cli)
    assert result.code == ExitCode.OK, result.out + result.err
    assert "test-command" not in result.out


def test_a_repo_without_a_test_command_passes(workspace, build, cli):
    assert dry_run(cli).code == ExitCode.OK


def test_a_test_command_beyond_the_type_fails_preflight_with_the_line_to_add(
    workspace, build, policy_repo, cli
):
    write(workspace / "Cargo.toml", "[package]\nname = 'x'\n")
    result = dry_run(cli)
    assert result.code == ExitCode.INVALID, result.out + result.err
    text = " ".join(result.out.split())
    for shown in (
        "implement: test-command",
        "agent dev (type builder)",
        "`cargo test` (from Cargo.toml)",
        'Add the line "cargo test"',
        "`shell:` of type builder in",
        "agent-types.yaml (org policy)",
    ):
        assert shown in text, shown
    assert "agent file" not in text  # the agent sets no shell: of its own


def test_an_agent_that_narrows_its_type_is_told_to_add_it_to_its_own_file(workspace, build, cli):
    build('shell: ["git status"]\n')
    write(workspace / "go.mod", "module example.com/x\n")
    result = dry_run(cli)
    assert result.code == ExitCode.INVALID, result.out + result.err
    text = " ".join(result.out.split())
    assert 'Add the line "go test" to `shell:` in' in text
    assert "dev.md (org agent file)" in text
    assert "of type builder" not in text  # the type already lists it


def test_a_denied_test_command_says_deny_beats_allow(workspace, build, cli):
    build('deny: { shell: ["go test"] }\n')
    write(workspace / "go.mod", "module example.com/x\n")
    result = dry_run(cli)
    assert result.code == ExitCode.INVALID, result.out + result.err
    text = " ".join(result.out.split())
    assert "`go test ./...`" in text and "deny rule beats an allow rule" in text
    assert "Add the line" not in text


def test_a_live_run_is_refused_before_any_spend(workspace, build, cli, monkeypatch):
    from _cli_support import FakeGateway

    from agentic_cli import runtime

    fake = FakeGateway({})
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", "tl-" + "fake-key-0003")  # gitleaks:allow
    monkeypatch.setattr(runtime, "network_transport", fake.transport)
    write(workspace / "Cargo.toml", "[package]\nname = 'x'\n")
    result = cli("run", "build")
    assert result.code == ExitCode.INVALID, result.out + result.err
    assert "cargo test" in result.out and fake.requests == []
