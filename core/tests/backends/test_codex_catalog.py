"""Tests for the codex model catalog (M2-40, DF-115): every model a step runs on, whatever
its vendor, is described to Codex, so it gets `apply_patch` and effort metadata instead of
Codex's fallback (no apply_patch, edits through the shell, a warning on every run). The
catalog also shapes the step's tool surface from its capabilities."""

import json

from agentic.backends.codex_catalog import BASE_INSTRUCTIONS, catalog_entry, model_catalog

#: Fields codex-rs `ModelInfo` (rust-v0.160.1) requires: no serde default.
REQUIRED = {
    "slug",
    "display_name",
    "supported_reasoning_levels",
    "shell_type",
    "visibility",
    "supported_in_api",
    "priority",
    "support_verbosity",
    "truncation_policy",
    "experimental_supported_tools",
}


def entry(tools=("read_files", "edit_files", "run_shell"), effort=None, **kw):
    return catalog_entry("acme/claude-sonnet", tools=tools, effort=effort, **kw)


def test_an_editing_agent_gets_freeform_apply_patch():
    assert entry()["apply_patch_tool_type"] == "freeform"


def test_a_read_only_agent_gets_no_apply_patch():
    assert entry(tools=("read_files", "search"))["apply_patch_tool_type"] is None


def test_an_agent_without_shell_capabilities_gets_no_shell():
    assert entry(tools=())["shell_type"] == "disabled"
    assert entry(tools=("search",))["shell_type"] == "shell_command"


def test_effort_levels_follow_the_agent():
    with_effort = entry(effort="xhigh")
    assert with_effort["default_reasoning_level"] == "xhigh"
    levels = [level["effort"] for level in with_effort["supported_reasoning_levels"]]
    assert levels == ["low", "medium", "high", "xhigh"]
    without = entry(effort=None)
    assert without["supported_reasoning_levels"] == []
    assert "default_reasoning_level" not in without


def test_required_codex_fields_present_and_json_serialisable():
    found = entry()
    assert REQUIRED <= set(found)
    assert found["slug"] == "acme/claude-sonnet"
    assert found["model_messages"]["instructions_template"] == BASE_INSTRUCTIONS
    assert found["supports_search_tool"] is False  # no hosted tools behind the gateway
    json.dumps(found)


def test_overrides_merge_over_defaults_by_exact_name_or_glob():
    overrides = {
        "acme/*": {"context_window": 1_000_000, "priority": 5},
        "acme/claude-sonnet": {"priority": 7},
    }
    found = entry(overrides=overrides)
    assert (found["context_window"], found["priority"]) == (1_000_000, 7)
    assert entry(overrides={"other/*": {"priority": 9}})["priority"] == 1


def test_an_override_cannot_rename_the_model_or_re_add_tools():
    found = entry(tools=("read_files",), overrides={"*": {"slug": "x", "apply_patch_tool_type": 1}})
    assert found["slug"] == "acme/claude-sonnet"
    assert found["apply_patch_tool_type"] is None


def test_model_catalog_wraps_one_entry():
    catalog = model_catalog("m", tools=("edit_files",), effort="low")
    assert [model["slug"] for model in catalog["models"]] == ["m"]


def test_base_instructions_name_no_vendor():
    lowered = BASE_INSTRUCTIONS.lower()
    assert "apply_patch" in lowered and "*** begin patch" in lowered
    for vendor in ("openai", "anthropic", "claude", "gemini", "gpt"):
        assert vendor not in lowered
