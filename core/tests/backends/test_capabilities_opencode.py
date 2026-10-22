"""The opencode row of the capability matrix (M2-57; spike S7): what the ACP profile for
OpenCode honours, and what preflight says about the rest."""

import pytest

from agentic.backends import OPTIONAL
from agentic.backends.capabilities import CAPABILITIES, backend_issues

AVAILABLE = {"native", "opencode"}


def on_opencode(agent, **changes):
    update = {"backend": "opencode", "tools": ["read_files"], "ask_shell": [], **changes}
    return agent.model_copy(update=update)


def issues(agent, policy):
    return [(i.setting, i.action) for i in backend_issues(agent, policy, available=AVAILABLE)]


def test_opencode_maps_our_capabilities_to_its_permission_keys():
    assert CAPABILITIES["opencode"].tools == {
        "read_files": ("read", "list"),
        "search": ("glob", "grep"),
        "edit_files": ("edit",),
        "run_shell": ("bash",),
    }


def test_opencode_is_an_optional_backend_of_the_acp_extra():
    assert OPTIONAL["opencode"] == "acp"


@pytest.mark.parametrize("tool", ["spawn_agent", "codeintel", "activate_skill"])
def test_tools_opencode_cannot_offer_block(resolved, policy, tool):
    agent = on_opencode(resolved, tools=["read_files", tool], temperature=None, effort=None)
    assert ("tools", "block") in issues(agent, policy)


def test_temperature_is_honoured_and_effort_is_not(resolved, policy):
    agent = on_opencode(resolved, temperature=0.2, effort="high")
    assert issues(agent, policy) == [("effort", "warn"), ("secret_scan", "warn")]


def test_ask_listed_commands_are_refused_without_an_approval_pause(resolved, policy):
    agent = on_opencode(resolved, ask_shell=["npm publish"], temperature=None, effort=None)
    assert issues(agent, policy) == [("ask", "warn"), ("secret_scan", "warn")]
