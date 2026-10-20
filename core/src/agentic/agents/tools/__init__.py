"""The tools an agent may use (M1-16).

Policy names CAPABILITIES (`read_files`, `edit_files`, ...); `build_tools` turns
the ones a resolved agent is allowed into Pydantic AI tools and builds nothing
else - a tool the agent may not use is never listed to the model, so it cannot
be called. `spawn_agent` is built by the child spawner (M1-17).
"""

from collections.abc import Callable

from pydantic_ai import Tool

from agentic.agents.tools.context import (
    TOOL_ERRORS,
    CodeIntel,
    PathGuard,
    Sandbox,
    ShellResult,
    SkillCatalog,
    SkillLibrary,
    ToolConfigError,
    ToolContext,
    refused,
)
from agentic.agents.tools.files import _refusal, edit_tools, read_tools, search_tools
from agentic.agents.tools.shell import LocalProcessSandbox, shell_tools
from agentic.codeintel import codeintel_tools
from agentic.policy import ResolvedAgent
from agentic.skills.catalog import SkillsError


def _codeintel_tools(ctx: ToolContext) -> list[Tool]:
    if ctx.codeintel is None:
        raise ToolConfigError("codeintel is allowed but no code intelligence adapter was given")
    # only what the adapter supports (M1-34); reads obey the guard (SP9 review C1)
    return codeintel_tools(
        ctx.codeintel,
        ctx.tree,
        read_refusal=lambda target: _refusal(ctx, target, reading=True),
    )


def _skill_tools(agent: ResolvedAgent, ctx: ToolContext) -> list[Tool]:
    if ctx.skills is None:
        raise ToolConfigError("activate_skill is allowed but no skill catalog was given")
    catalog, allowed = ctx.skills, set(agent.skills)

    def activate_skill(name: str, file: str | None = None) -> str:
        """Load the full instructions of one of your skills, or with `file`, one of the
        files bundled with it (a path inside the skill, as its instructions list them)."""
        if name not in allowed:
            return refused(f"{name!r} is not one of your skills: {sorted(allowed)}")
        try:
            if file is None:
                return catalog.get(name)
            if not isinstance(catalog, SkillLibrary):
                return refused("this skill catalog has no bundled files")
            return catalog.resource(name, file)
        except (SkillsError, *TOOL_ERRORS) as error:  # tampered, missing, a bad path
            return refused(f"{type(error).__name__}: {error}")

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
    "SkillLibrary",
    "ToolConfigError",
    "ToolContext",
    "build_tools",
]
