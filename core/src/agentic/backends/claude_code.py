"""The claude-code backend: Claude Code driven through the Claude Agent SDK, locked down
(M1-29, spike S2). Optional: needs the `claude-code` extra of agentic-core.

What S2 proved, applied to every run:
  * every model call goes to TokenLean (`ANTHROPIC_BASE_URL`) with the user's proxy
    key and our identity headers (`ANTHROPIC_CUSTOM_HEADERS`), on the resolved model -
    the small/fast model is pinned to it too;
  * nothing else leaves the machine: Claude Code's telemetry, error reporting,
    auto-update and other non-essential traffic are switched off;
  * no user or project settings, hooks, plugins or CLAUDE.md load (`--bare` and an
    explicit empty `setting_sources`), and each step has its own config folder;
  * the model is offered only the tools mapped from the agent's capabilities
    (`tools=`; `allowed_tools` only auto-approves), and the guard's permission rules
    apply in don't-ask mode - anything not allowed is refused, never asked about.

This module imports `claude_agent_sdk` only when a run starts, so agentic-core works
without the extra.
"""

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agentic.backends.capabilities import CAPABILITIES
from agentic.transport import CallIdentity

if TYPE_CHECKING:
    from agentic.engine.engine import StepContext

#: Switches that stop every request Claude Code makes besides model calls (S2 Q3).
QUIET = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
}


@dataclass(frozen=True)
class ClaudeCodeSettings:
    gateway_url: str  # TokenLean's root
    api_key: str  # the user's TokenLean key
    config_root: Path  # per-step CLAUDE_CONFIG_DIR folders live here
    cli_path: Path | None = None  # None: the SDK's bundled (Anthropic-signed) binary


def lock_down(
    step: "StepContext",
    *,
    settings: ClaudeCodeSettings,
    identity: CallIdentity,
    permissions: dict,
    cwd: Path,
    edit_tree: bool,
) -> dict:
    """The `ClaudeAgentOptions` keyword arguments for one step: every lock-down setting."""
    agent = step.agent
    mapped = CAPABILITIES["claude-code"].tools
    tools = list(dict.fromkeys(name for cap in agent.tools for name in mapped.get(cap, ())))
    rules = copy.deepcopy(permissions)
    if edit_tree:  # a redirect or file edit inside the tree is a write (S2 Q6)
        allow = rules.setdefault("permissions", {}).setdefault("allow", [])
        allow.append("Edit(./**)")
    config_dir = (
        settings.config_root / step.run_id / f"{step.step}-{step.iteration}" / agent.definition.name
    )
    env = {
        "ANTHROPIC_BASE_URL": settings.gateway_url,
        "ANTHROPIC_API_KEY": settings.api_key,
        "ANTHROPIC_CUSTOM_HEADERS": "\n".join(f"{k}: {v}" for k, v in identity.headers().items()),
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": agent.model,
        "ANTHROPIC_SMALL_FAST_MODEL": agent.model,
        "CLAUDE_CONFIG_DIR": str(config_dir),
        **QUIET,
    }
    options: dict = {
        "cwd": str(cwd),
        "model": agent.model,
        "env": env,
        "setting_sources": [],
        "extra_args": {"bare": None, "no-session-persistence": None},
        "tools": tools,
        "allowed_tools": list(tools),
        "permission_mode": "dontAsk",
        "settings": json.dumps(rules),
    }
    if agent.limits.max_turns is not None:
        options["max_turns"] = agent.limits.max_turns
    if agent.effort is not None:
        options["effort"] = agent.effort
    available = step.budget.available()
    if available is not None:
        options["max_budget_usd"] = float(available)
    if settings.cli_path is not None:
        options["cli_path"] = str(settings.cli_path)
    return options
