"""The tools an agent may use (M1-16).

Policy names CAPABILITIES (`read_files`, `edit_files`, ...); `build_tools` turns
the ones a resolved agent is allowed into Pydantic AI tools and builds nothing
else - a tool the agent may not use is never listed to the model, so it cannot
be called. `spawn_agent` is built by the child spawner (M1-17).
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
    guarded,
    refused,
)
from agentic.agents.tools.files import edit_tools, read_tools, search_tools
from agentic.agents.tools.shell import LocalProcessSandbox, shell_tools
from agentic.policy import ResolvedAgent


def _codeintel_tools(ctx: ToolContext) -> list[Tool]:
    if ctx.codeintel is None:
        raise ToolConfigError("codeintel is allowed but no code intelligence adapter was given")
    intel = ctx.codeintel

    @guarded
    def dependents(target: str) -> str:
        """Files and symbols that depend on a file or symbol."""
        return "\n".join(intel.dependents(target)) or "(none)"

    @guarded
    def impact(diff: str) -> str:
        """What a unified diff may affect, tests included."""
        return "\n".join(intel.impact(diff)) or "(none)"

    @guarded
    def tests_for(path: str) -> str:
        """The tests that cover a file."""
        return "\n".join(intel.tests_for(path)) or "(none)"

    return [Tool(dependents), Tool(impact), Tool(tests_for)]


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
    """The tools for the capabilities the resolved agent is allowed - and no others."""
    if unknown := sorted(set(agent.tools) - set(_BUILDERS)):
        raise ToolConfigError(f"no tools exist for the capabilities {unknown}")
    tools: list[Tool] = []
    for capability in agent.tools:
        tools.extend(_BUILDERS[capability](agent, ctx))
    return tools


__all__ = [
    "CodeIntel",
    "LocalProcessSandbox",
    "PathGuard",
    "Sandbox",
    "ShellResult",
    "SkillCatalog",
    "ToolConfigError",
    "ToolContext",
    "build_tools",
]
