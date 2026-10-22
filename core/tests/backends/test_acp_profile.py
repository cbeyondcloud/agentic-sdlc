"""The generic parts of an ACP profile (M2-57; spike S7): our shell policy in neutral form,
and the per-step environment every ACP agent is launched with. Pure data - no agent runs."""

import os
from pathlib import Path

import pytest

from agentic.backends.acp.profile import (
    AcpProfile,
    Launch,
    ShellRules,
    home_env,
    launch_env,
    spelled_out,
    write_files,
)

HOME = Path("/state/acp/r-1/develop-1/developer")


def profile(**changes) -> AcpProfile:
    fields = {
        "name": "fake",
        "command": ("fake-agent", "acp"),
        "version": "1.0.0",
        "configure": lambda setup: Launch(),
        "quiet": {"FAKE_TELEMETRY": "0"},
        "inherit": ("FAKE_CA_BUNDLE", "FAKE_PROXY"),
        **changes,
    }
    return AcpProfile(**fields)


def test_rules_come_from_the_resolved_agent(resolved):
    agent = resolved.model_copy(
        update={
            "shell": ["pytest", "ruff"],
            "ask_shell": ["npm publish"],
            "deny_shell": ["git push"],
            "deny_paths": ["infra/**"],
        }
    )
    assert ShellRules.from_agent(agent) == ShellRules(
        allow=("pytest", "ruff"), ask=("npm publish",), deny=("git push",), deny_paths=("infra/**",)
    )
    assert ShellRules.from_agent(agent).deny_anywhere == ()  # the guard's floor: from commons


def test_alternatives_in_a_rule_are_spelled_out():
    assert spelled_out("git rebase -x|--exec") == ["git rebase -x", "git rebase --exec"]
    assert spelled_out("pwsh -e|-ec") == ["pwsh -e", "pwsh -ec"]
    assert spelled_out("pytest") == ["pytest"]


def test_every_home_variable_points_inside_the_step_home():
    env = home_env(HOME)
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        assert Path(env[name]).is_relative_to(HOME), name
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        assert Path(env[name]).is_relative_to(HOME), name
    assert env["HOME"] == str(HOME)


def test_only_named_variables_are_inherited_and_never_a_credential():
    user = {
        "FAKE_CA_BUNDLE": "/etc/ssl/corp.pem",
        "FAKE_PROXY": "http://user:pa55@proxy.acme.example:8080",  # carries a credential
        "OPENAI_API_KEY": "sk-" + "x" * 20,  # never named: never inherited
    }
    env = launch_env(profile(), Launch(), HOME, environ=user)
    assert env["FAKE_CA_BUNDLE"] == "/etc/ssl/corp.pem"
    assert "FAKE_PROXY" not in env and "OPENAI_API_KEY" not in env


def test_quiet_switches_and_the_launch_win_over_inherited_values():
    user = {"FAKE_CA_BUNDLE": "/user/ca.pem"}
    launch = Launch(env={"FAKE_CA_BUNDLE": "/profile/ca.pem", "FAKE_ROUTE": "x"})
    env = launch_env(profile(inherit=("FAKE_CA_BUNDLE", "FAKE_TELEMETRY")), launch, HOME, user)
    assert env["FAKE_CA_BUNDLE"] == "/profile/ca.pem"
    assert env["FAKE_TELEMETRY"] == "0"
    assert env["FAKE_ROUTE"] == "x"
    assert env["HOME"] == str(HOME)


def test_profile_files_are_written_under_the_home(tmp_path):
    write_files(tmp_path, {"cfg/agent.json": "{}", "notes.txt": "x"})
    assert (tmp_path / "cfg" / "agent.json").read_text(encoding="utf-8") == "{}"
    assert (tmp_path / "notes.txt").exists()
    if os.name != "nt":  # a key file: the user only
        assert (tmp_path / "notes.txt").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("name", ["../escape.txt", "/abs/escape.txt", "a/../../escape.txt"])
def test_a_profile_file_outside_the_home_is_refused(tmp_path, name):
    with pytest.raises(ValueError, match="outside"):
        write_files(tmp_path / "home", {name: "x"})
    assert not (tmp_path / "escape.txt").exists()
