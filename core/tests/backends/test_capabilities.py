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


def issues(agent, policy, available=BOTH):
    return [(i.setting, i.action) for i in backend_issues(agent, policy, available=available)]


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
    assert [(i.setting, i.action) for i in found] == [("tools", "block")]
    assert "spawn_agent" in found[0].detail and "codeintel" in found[0].detail


def test_an_unavailable_backend_blocks(resolved, policy):
    agent = on_claude_code(resolved, temperature=None)
    assert issues(agent, policy, available={"native"}) == [("backend", "block")]


def test_ask_rules_warn_on_a_backend_without_approvals(resolved, policy):
    agent = on_claude_code(resolved, temperature=None, ask_shell=["npm publish"])
    found = backend_issues(agent, policy, available=BOTH)
    assert [(i.setting, i.action) for i in found] == [("ask", "warn")]
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
    assert policy.unhonoured == {"temperature": "warn", "effort": "ignore"}
