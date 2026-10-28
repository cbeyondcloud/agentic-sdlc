"""The claude-code lock-down options (M1-29; spike S2): every setting S2 proved, on every
run. Pure data - no Claude Code process is started here."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from agentic.backends.claude_code import ClaudeCodeSettings, lock_down
from agentic.engine.engine import StepContext
from agentic.transport import BudgetLedger, CallIdentity

SETTINGS = ClaudeCodeSettings(
    gateway_url="https://tokenlean.acme.internal", api_key="tl-key", config_root=Path("/state")
)
IDENTITY = CallIdentity(
    team="payments",
    user="dev@acme.example",
    repo="acme/app",
    pipeline="feature-delivery",
    run_id="r-1",
    step="develop",
    agent="developer@1.2.0",
    policy_version="v1",
)
PERMISSIONS = {
    "permissions": {"allow": ["Bash(pytest:*)"], "ask": [], "deny": ["Bash(git push:*)"]}
}
CODING = ["read_files", "search", "edit_files", "run_shell"]


def step(agent, limit: str | None = "2", name: str = "develop") -> StepContext:
    ledger = BudgetLedger(Decimal("10") if limit else None)  # unlimited: no run limit either
    budget = ledger.run.open(f"{name}-1", "step", Decimal(limit) if limit else None)
    return StepContext("r-1", name, 1, agent, budget, None)


@pytest.fixture
def coder(resolved):
    return resolved.model_copy(
        update={"backend": "claude-code", "tools": CODING, "effort": "high", "temperature": None}
    )


def options(agent, **changes):
    kwargs = {
        "settings": SETTINGS,
        "identity": IDENTITY,
        "permissions": PERMISSIONS,
        "cwd": Path("/tree"),
        "edit_tree": True,
        **changes,
    }
    return lock_down(step(agent), **kwargs)


def test_every_lock_down_setting_is_present(coder):
    kw = options(coder)
    assert kw["setting_sources"] == []
    assert kw["extra_args"] == {"bare": None, "no-session-persistence": None}
    assert kw["permission_mode"] == "dontAsk"
    # search has no tool in --bare Claude Code (review I5); nothing is auto-approved (C1)
    assert kw["tools"] == ["Read", "Edit", "Bash"]
    assert "allowed_tools" not in kw
    assert kw["model"] == coder.model and kw["cwd"] == str(Path("/tree"))
    assert kw["effort"] == "high" and kw["max_turns"] == coder.limits.max_turns
    env = kw["env"]
    assert env["ANTHROPIC_BASE_URL"] == "https://tokenlean.acme.internal"
    assert env["ANTHROPIC_API_KEY"] == "tl-key"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == env["ANTHROPIC_SMALL_FAST_MODEL"] == kw["model"]
    switches = (
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_TELEMETRY",
        "DISABLE_ERROR_REPORTING",
        "DISABLE_AUTOUPDATER",
    )
    assert {env[name] for name in switches} == {"1"}
    assert "X-Agentic-Run-Id: r-1" in env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
    permissions = json.loads(kw["settings"])["permissions"]
    assert "Edit(./**)" in permissions["allow"] and "Bash(git push:*)" in permissions["deny"]


def test_tools_follow_the_agents_capabilities(coder):
    reader = coder.model_copy(update={"tools": ["read_files"]})
    kw = options(reader, edit_tree=False)
    assert kw["tools"] == ["Read"] and "allowed_tools" not in kw
    assert "Edit(./**)" not in json.loads(kw["settings"])["permissions"]["allow"]


def test_the_permissions_given_are_not_changed(coder):
    options(coder)
    assert PERMISSIONS["permissions"]["allow"] == ["Bash(pytest:*)"]


def test_forbidden_identity_headers_never_appear(coder):
    headers = options(coder)["env"]["ANTHROPIC_CUSTOM_HEADERS"].lower()
    assert all(bad not in headers for bad in ("x-step-name", "x-workflow-id", "x-session-id"))


def test_each_step_gets_its_own_config_dir(coder):
    develop = lock_down(
        step(coder),
        settings=SETTINGS,
        identity=IDENTITY,
        permissions=PERMISSIONS,
        cwd=Path("/tree"),
        edit_tree=True,
    )
    review = lock_down(
        step(coder, name="review"),
        settings=SETTINGS,
        identity=IDENTITY,
        permissions=PERMISSIONS,
        cwd=Path("/tree"),
        edit_tree=True,
    )
    first, second = develop["env"]["CLAUDE_CONFIG_DIR"], review["env"]["CLAUDE_CONFIG_DIR"]
    assert first != second
    assert Path(first).is_relative_to(Path("/state"))


def test_budget_slice_becomes_max_budget_usd(coder):
    assert options(coder)["max_budget_usd"] == 2.0
    unlimited = lock_down(
        step(coder, limit=None),
        settings=SETTINGS,
        identity=IDENTITY,
        permissions=PERMISSIONS,
        cwd=Path("/tree"),
        edit_tree=True,
    )
    assert "max_budget_usd" not in unlimited


def test_cli_path_is_passed_when_the_org_pins_an_install(coder):
    pinned = ClaudeCodeSettings(
        gateway_url=SETTINGS.gateway_url,
        api_key="k",
        config_root=Path("/state"),
        cli_path=Path("/opt/claude"),
    )
    assert options(coder, settings=pinned)["cli_path"] == str(Path("/opt/claude"))
    assert "cli_path" not in options(coder)


def test_policy_pass_env_is_kept_but_never_a_provider_switch(coder):  # DF-541
    environ = {"SSH_AUTH_SOCK": "/tmp/agent.sock", "ANTHROPIC_AUTH_TOKEN": "t", "GITHUB_TOKEN": "g"}
    passing = ClaudeCodeSettings(
        gateway_url="https://tokenlean.acme.internal",
        api_key="tl-key",
        config_root=Path("/state"),
        pass_env=("SSH_AUTH_SOCK", "ANTHROPIC_AUTH_TOKEN", "AGENTIC_TOKENLEAN_API_KEY"),
    )
    env = options(coder, settings=passing, environ=environ)["env"]
    assert "SSH_AUTH_SOCK" not in env  # not blanked: the user's own value reaches Claude Code
    assert env["ANTHROPIC_AUTH_TOKEN"] == env["GITHUB_TOKEN"] == ""
    assert options(coder, environ=environ)["env"]["SSH_AUTH_SOCK"] == ""  # without pass_env


def test_provider_switches_and_credentials_are_blanked(coder):  # review I1
    environ = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_SECRET_ACCESS_KEY": "s",
        "ANTHROPIC_AUTH_TOKEN": "t",
        "GITHUB_TOKEN": "g",
        "PATH": "/usr/bin",
        "CLAUDE_CODE_GIT_BASH_PATH": "C:/Git/bin/bash.exe",
    }
    env = options(coder, environ=environ)["env"]
    assert env["CLAUDE_CODE_USE_BEDROCK"] == env["AWS_SECRET_ACCESS_KEY"] == ""
    assert env["ANTHROPIC_AUTH_TOKEN"] == env["GITHUB_TOKEN"] == ""
    assert "PATH" not in env and "CLAUDE_CODE_GIT_BASH_PATH" not in env  # kept as the user has them
    assert env["ANTHROPIC_API_KEY"] == "tl-key"  # ours, never blanked


def test_internal_overrides_of_a_parent_session_are_blanked(coder):
    # A developer who runs agentic from inside a Claude Code session inherits its internal
    # `_CLAUDE_*` overrides; one makes the binary treat TokenLean as first party and reach
    # past it to the vendor's own API.
    environ = {"_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL": "1", "_claude_other": "x"}
    env = options(coder, environ=environ)["env"]
    assert env["_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"] == env["_claude_other"] == ""


def test_claude_code_internal_switches_never_pass(coder):
    # A parent Claude Code session sets this; inherited, it sent calls to the vendor's API.
    environ = {"_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL": "1"}
    passing = ClaudeCodeSettings(
        gateway_url="https://tokenlean.acme.internal",
        api_key="tl-key",
        config_root=Path("/state"),
        pass_env=("_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL",),
    )
    env = options(coder, settings=passing, environ=environ)["env"]
    assert env["_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"] == ""


def test_a_parent_claude_code_session_is_not_inherited(coder):
    environ = {
        "CLAUDE_CODE_SESSION_ID": "s-1",
        "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/session.sock",
        "CLAUDE_CODE_GIT_BASH_PATH": "C:/Git/bin/bash.exe",
    }
    env = options(coder, environ=environ)["env"]
    assert env["CLAUDE_CODE_SESSION_ID"] == env["CLAUDE_CODE_MESSAGING_SOCKET"] == ""
    assert "CLAUDE_CODE_GIT_BASH_PATH" not in env  # Windows needs it to find bash
    passing = ClaudeCodeSettings(
        gateway_url="https://tokenlean.acme.internal",
        api_key="tl-key",
        config_root=Path("/state"),
        pass_env=("CLAUDE_CODE_SESSION_ID",),
    )
    assert "CLAUDE_CODE_SESSION_ID" not in options(coder, settings=passing, environ=environ)["env"]


def test_variables_the_sdk_manages_are_left_to_it(coder):
    # The SDK sets CLAUDE_CODE_ENTRYPOINT=sdk-py under our env, drops CLAUDECODE, and
    # defaults CLAUDE_CODE_SDK_READS_SESSION_STATE only when it is absent (review I1).
    environ = {
        "CLAUDE_CODE_ENTRYPOINT": "claude-vscode",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
    }
    env = options(coder, environ=environ)["env"]
    assert not set(environ) & set(env)
