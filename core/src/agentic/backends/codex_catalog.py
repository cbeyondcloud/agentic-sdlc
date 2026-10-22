"""The model catalog the codex backend gives Codex for each step (M2-40, DF-115).

Codex knows only its vendor's models. Any other model (every model TokenLean routes to
another provider) gets Codex's fallback metadata: no `apply_patch`, so edits go through
the shell, plus a warning on every run (spike S3 Q10). `model_catalog_json` replaces
Codex's catalog with ours. Each step gets a one-model catalog for its resolved model.
So every model, whatever its vendor, is described the same way, and the catalog also
shapes the step's tool surface:

  * `apply_patch` (freeform) only when the agent may edit files;
  * the shell tool only when the agent has a capability Codex serves through it
    (read, search, run, edit), so an agent with no such capability gets no shell;
  * reasoning levels, so `effort` reaches the model as `reasoning.effort`;
  * no hosted search, verbosity or reasoning summaries, which a gateway upstream may
    not support.

Codex requires an instructions template per catalog model. It is the framework's own,
`codex_instructions.md`, which names no vendor. The agent's instructions travel
separately, as Codex's developer instructions.

The fields follow codex-rs `ModelInfo` at the pinned tag. A field Codex renames fails
fast under `--strict-config` and the pinned-version contract tests.
"""

from collections.abc import Collection, Mapping
from fnmatch import fnmatchcase
from importlib.resources import files

from agentic.backends.capabilities import CAPABILITIES

BASE_INSTRUCTIONS = (
    files("agentic.backends").joinpath("codex_instructions.md").read_text(encoding="utf-8")
)
#: Reasoning levels offered when the agent sets an effort (its own level is added).
EFFORT_LEVELS = ("low", "medium", "high")
#: Fields an org override may not change: identity, and the tool surface set by policy.
_FIXED = frozenset({"slug", "apply_patch_tool_type", "shell_type", "model_messages"})


def catalog_entry(
    model: str,
    *,
    tools: Collection[str],
    effort: str | None,
    overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> dict:
    """The catalog entry for `model` as this agent may use it. `overrides` maps model
    names or globs to fields merged over the defaults (an exact name wins over a glob);
    they may tune context windows or priorities, never the tool surface."""
    mapped = {name for cap in tools for name in CAPABILITIES["codex"].tools.get(cap, ())}
    levels = list(EFFORT_LEVELS) if effort else []
    if effort and effort not in levels:
        levels.append(effort)
    entry: dict = {
        "slug": model,
        "display_name": model,
        "description": "An organisation model served through the gateway",
        "supported_reasoning_levels": [{"effort": e, "description": e} for e in levels],
        "shell_type": "shell_command" if "exec_command" in mapped else "disabled",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 1,
        "availability_nux": None,
        "upgrade": None,
        "model_messages": {"instructions_template": BASE_INSTRUCTIONS},
        "include_skills_usage_instructions": False,
        "include_plugin_usage_instructions": False,
        "include_apps_usage_instructions": False,
        "supports_reasoning_summary_parameter": False,
        "default_reasoning_summary": "none",
        "support_verbosity": False,
        "default_verbosity": None,
        "apply_patch_tool_type": "freeform" if "apply_patch" in mapped else None,
        "web_search_tool_type": "text",
        "truncation_policy": {"mode": "bytes", "limit": 10_000},
        "context_window": 128_000,
        "experimental_supported_tools": [],
        "input_modalities": ["text"],
        "supports_search_tool": False,
    }
    if effort:
        entry["default_reasoning_level"] = effort
    for pattern, fields in sorted((overrides or {}).items(), key=lambda item: item[0] == model):
        if pattern == model or fnmatchcase(model, pattern):
            entry.update({k: v for k, v in fields.items() if k not in _FIXED})
    return entry


def model_catalog(
    model: str,
    *,
    tools: Collection[str],
    effort: str | None,
    overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> dict:
    """The `model_catalog_json` document: Codex's `ModelsResponse` with one model."""
    return {"models": [catalog_entry(model, tools=tools, effort=effort, overrides=overrides)]}
