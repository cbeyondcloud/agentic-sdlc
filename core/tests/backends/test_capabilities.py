"""Tests for the backend capability matrix and the unhonoured-settings policy (M1-27, DF-25):
which resolved settings a backend honours, and what org policy does when it cannot."""

import shutil
from pathlib import Path

import pytest

from agentic.backends.capabilities import CAPABILITIES, backend_issues
from agentic.policy import load_policy

BOTH = {"native", "claude-code"}


def on_claude_code(agent, **changes):
    return agent.model_copy(
        update={"backend": "claude-code", "tools": ["read_files"], "ask_shell": [], **changes}
    )


def issues(agent, policy, available=BOTH, skip=("secret_scan",)):
    # claude-code always reports its unscanned egress (DF-149); tests of other settings
    # skip it, and the secret_scan tests below look at it alone
    found = backend_issues(agent, policy, available=available)
    return [(i.setting, i.action) for i in found if i.setting not in skip]


def test_temperature_on_claude_code_is_blocked_when_policy_says_block(resolved, policy):
    policy.unhonoured["temperature"] = "block"
    agent = on_claude_code(resolved, temperature=0.2, effort=None)
    assert issues(agent, policy) == [("temperature", "block")]


def test_temperature_on_claude_code_warns_by_default(resolved, policy):
    agent = on_claude_code(resolved, temperature=0.2, effort=None)
    assert issues(agent, policy) == [("temperature", "warn")]


def test_ignore_reports_nothing(resolved, policy):
    policy.unhonoured["temperature"] = "ignore"
    agent = on_claude_code(resolved, temperature=0.2, effort=None)
    assert issues(agent, policy) == []


def test_effort_is_honoured_on_both_backends(resolved, policy):
    native = resolved.model_copy(update={"effort": "high", "temperature": None})
    assert issues(native, policy) == []
    assert issues(on_claude_code(resolved, effort="high", temperature=None), policy) == []


@pytest.mark.parametrize("model", ["openai/gpt-5-mini", "openai/o3", "gpt-5"])
def test_temperature_on_a_reasoning_model_is_unhonoured_on_native(resolved, policy, model):
    agent = resolved.model_copy(update={"model": model, "temperature": 0.2, "effort": None})
    assert issues(agent, policy) == [("temperature", "warn")]


@pytest.mark.parametrize("model", ["anthropic/claude-sonnet", "google/gemini-flash"])
def test_temperature_is_honoured_on_other_native_models(resolved, policy, model):
    agent = resolved.model_copy(update={"model": model, "temperature": 0.2, "effort": None})
    assert issues(agent, policy) == []


def test_a_tool_the_backend_lacks_blocks(resolved, policy):
    tools = ["read_files", "spawn_agent", "codeintel"]
    agent = on_claude_code(resolved, tools=tools, temperature=None)
    found = backend_issues(agent, policy, available=BOTH)
    assert [(i.setting, i.action) for i in found] == [("tools", "block"), ("secret_scan", "warn")]
    assert "spawn_agent" in found[0].detail and "codeintel" in found[0].detail


def test_an_unavailable_backend_blocks(resolved, policy):
    agent = on_claude_code(resolved, temperature=None)
    assert issues(agent, policy, available={"native"}) == [("backend", "block")]


def test_ask_rules_warn_on_a_backend_without_approvals(resolved, policy):
    agent = on_claude_code(resolved, temperature=None, ask_shell=["npm publish"])
    found = backend_issues(agent, policy, available=BOTH)
    assert [(i.setting, i.action) for i in found] == [("ask", "warn"), ("secret_scan", "warn")]
    assert "refused" in found[0].detail


def test_every_backend_maps_tool_capabilities():
    assert set(CAPABILITIES) >= {"native", "claude-code"}
    assert CAPABILITIES["claude-code"].tools["run_shell"] == ("Bash",)
    assert "spawn_agent" not in CAPABILITIES["claude-code"].tools
    assert CAPABILITIES["native"].children and not CAPABILITIES["claude-code"].children


def test_unhonoured_policy_loads_from_org_yaml(fixtures, tmp_path: Path):
    root = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", root)
    org = root / "org.yaml"
    org.write_text(org.read_text(encoding="utf-8") + "unhonoured: {effort: ignore}\n", "utf-8")
    policy = load_policy(root)
    assert policy.unhonoured == {
        "temperature": "warn",
        "effort": "ignore",
        "secret_scan": "warn",
        "shell_default_deny": "warn",
    }


def test_claude_code_egress_is_not_secret_scanned_and_warns_by_default(resolved, policy):
    # DF-149 / decision 38: claude-code calls the model itself, past agentic.transport
    agent = on_claude_code(resolved, temperature=None, effort=None)
    found = backend_issues(agent, policy, available=BOTH)
    assert [(i.setting, i.action) for i in found] == [("secret_scan", "warn")]
    assert "claude-code" in found[0].detail


def test_an_org_can_block_backends_whose_egress_is_not_scanned(resolved, policy):
    policy.unhonoured["secret_scan"] = "block"
    agent = on_claude_code(resolved, temperature=None, effort=None)
    assert issues(agent, policy, skip=()) == [("secret_scan", "block")]


def test_native_egress_is_scanned(resolved, policy):
    policy.unhonoured["secret_scan"] = "block"
    native = resolved.model_copy(update={"effort": None, "temperature": None})
    assert issues(native, policy, skip=()) == []


def test_claude_code_maps_only_the_tools_it_really_offers():  # review I5 (--bare)
    assert CAPABILITIES["claude-code"].tools == {
        "read_files": ("Read",),
        "edit_files": ("Edit",),
        "run_shell": ("Bash",),
    }


# ── codex (M2-40, spike S3, DF-119) ───────────────────────────────────────────

ALL = {"native", "claude-code", "codex"}


def on_codex(agent, **changes):
    update = {"backend": "codex", "tools": ["read_files"], "ask_shell": [], "temperature": None}
    return agent.model_copy(update={**update, "effort": None, **changes})


def test_codex_row_matches_spike_s3():
    codex = CAPABILITIES["codex"]
    assert codex.tools == {
        "read_files": ("exec_command",),
        "search": ("exec_command",),
        "run_shell": ("exec_command",),
        "edit_files": ("apply_patch", "exec_command"),
    }
    assert (codex.temperature, codex.effort) == (False, True)
    assert not (codex.approvals or codex.children or codex.stuck_check or codex.secret_scan)
    assert codex.shell_default_deny is False
    assert all(c.shell_default_deny for name, c in CAPABILITIES.items() if name != "codex")


def test_codex_shell_default_deny_warns_by_default(resolved, policy):
    found = backend_issues(on_codex(resolved), policy, available=ALL)
    shell = [i for i in found if i.setting == "shell_default_deny"]
    assert [(i.setting, i.action) for i in shell] == [("shell_default_deny", "warn")]
    assert "codex" in shell[0].detail and "unlisted" in shell[0].detail


def test_an_org_can_block_backends_without_shell_default_deny(resolved, policy):
    policy.unhonoured["shell_default_deny"] = "block"
    found = issues(on_codex(resolved, tools=["run_shell"]), policy, available=ALL)
    assert found == [("shell_default_deny", "block")]


def test_ignore_hides_the_shell_gap(resolved, policy):
    policy.unhonoured["shell_default_deny"] = "ignore"
    assert issues(on_codex(resolved, tools=["edit_files"]), policy, available=ALL) == []


def test_an_agent_without_shell_tools_has_no_shell_issue(resolved, policy):
    assert issues(on_codex(resolved, tools=[]), policy, available=ALL) == []


def test_codex_temperature_is_unhonoured_and_effort_honoured(resolved, policy):
    agent = on_codex(resolved, temperature=0.3, effort="high")
    found = issues(agent, policy, available=ALL, skip=("secret_scan", "shell_default_deny"))
    assert found == [("temperature", "warn")]


def test_codex_spawn_agent_blocks(resolved, policy):
    found = issues(on_codex(resolved, tools=["spawn_agent"]), policy, available=ALL)
    assert found == [("tools", "block")]
