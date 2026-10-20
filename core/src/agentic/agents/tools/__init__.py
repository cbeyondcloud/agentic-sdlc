"""The tools an agent may use (M1-16).

Policy names CAPABILITIES (`read_files`, `edit_files`, ...); `build_tools` turns
the ones a resolved agent is allowed into Pydantic AI tools and builds nothing
else - a tool the agent may not use is never listed to the model, so it cannot
be called. `spawn_agent` is built by the child spawner (M1-17).

Every tool built here goes through `protect_tools` (M1-41, M1-42): what it
returns reaches the model with host paths scrubbed, secrets masked or withheld,
and labelled as untrusted data.
"""

from collections.abc import Callable

from pydantic_ai import Tool

from agentic.agents.tools.context import (
    CodeIntel,
    PathGuard,
    Sandbox,
    ShellResult,
    SkillCatalog,
    ToolConfigError,
    ToolContext,
    refused,
)
from agentic.agents.tools.files import edit_tools, read_tools, search_tools
from agentic.agents.tools.protect import TRUSTED_TOOLS, protect_tools
from agentic.agents.tools.shell import LocalProcessSandbox, shell_tools
from agentic.codeintel import codeintel_tools
from agentic.policy import ResolvedAgent


def _codeintel_tools(ctx: ToolContext) -> list[Tool]:
    if ctx.codeintel is None:
        raise ToolConfigError("codeintel is allowed but no code intelligence adapter was given")
    return codeintel_tools(ctx.codeintel, ctx.tree)  # only what the adapter supports (M1-34)


def _skill_tools(agent: ResolvedAgent, ctx: ToolContext) -> list[Tool]:
    if ctx.skills is None:
        raise ToolConfigError("activate_skill is allowed but no skill catalog was given")
    catalog, allowed = ctx.skills, set(agent.skills)

    def activate_skill(name: str) -> str:
        """Load the full instructions of one of your skills."""
        if name not in allowed:
            return refused(f"{name!r} is not one of your skills: {sorted(allowed)}")
        return catalog.get(name)

    return [Tool(activate_skill)]


def _shell(ctx: ToolContext) -> list[Tool]:
    if ctx.sandbox is None:
        raise ToolConfigError("run_shell is allowed but no sandbox was given")
    if not ctx.sandbox.isolated and not ctx.allow_unisolated:
        raise ToolConfigError(
            "run_shell needs an isolated sandbox (M1-32/33); a local process "
            "sandbox is for tests and development and must be allowed explicitly"
        )
    return shell_tools(ctx)


_BUILDERS: dict[str, Callable[[ResolvedAgent, ToolContext], list[Tool]]] = {
    "read_files": lambda agent, ctx: read_tools(ctx),
    "search": lambda agent, ctx: search_tools(ctx),
    "edit_files": lambda agent, ctx: edit_tools(ctx),
    "run_shell": lambda agent, ctx: _shell(ctx),
    "codeintel": lambda agent, ctx: _codeintel_tools(ctx),
    "activate_skill": _skill_tools,
    "spawn_agent": lambda agent, ctx: [],  # built by the child spawner (M1-17)
}


def build_tools(agent: ResolvedAgent, ctx: ToolContext) -> list[Tool]:
    """The tools for the capabilities the resolved agent is allowed - and no others -
    each one's output scrubbed, secret-scanned and labelled as data (`protect_tools`)."""
    if unknown := sorted(set(agent.tools) - set(_BUILDERS)):
        raise ToolConfigError(f"no tools exist for the capabilities {unknown}")
    tools: list[Tool] = []
    for capability in agent.tools:
        tools.extend(_BUILDERS[capability](agent, ctx))
    return protect_tools(tools, scanner=ctx.secret_scanner, tree=ctx.tree)


__all__ = [
    "CodeIntel",
    "LocalProcessSandbox",
    "PathGuard",
    "Sandbox",
    "ShellResult",
    "SkillCatalog",
    "TRUSTED_TOOLS",
    "ToolConfigError",
    "ToolContext",
    "build_tools",
    "protect_tools",
]
