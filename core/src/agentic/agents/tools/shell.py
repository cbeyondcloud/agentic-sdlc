"""Running shell commands for an agent: guard first, then a sandbox.

The guard (`command`) refuses destructive, denied and unlisted commands before
anything runs; a command on the ask list (`asks`) waits for a person's approval
(the run pauses, M1-31). The command then runs through a `Sandbox`: the real,
OS-isolated one is `agentic.sandbox.SrtSandbox` (D38). `LocalProcessSandbox`
runs a plain subprocess with NO isolation - for tests, development and a regime
that may run unsandboxed - which is why `build_tools` refuses it unless the
caller explicitly allows it.

`run_shell` is async: when the run stops (time limit, budget, kill switch) the
call is cancelled and the sandbox kills the command and everything it started.
"""

from pathlib import Path

from pydantic_ai import ApprovalRequired, RunContext, Tool

from agentic.agents.tools.context import ShellResult, ToolContext, guarded, refused
from agentic.sandbox.process import MAX_OUTPUT, run_process


class LocalProcessSandbox:
    """A subprocess in the tree's folder - no OS isolation (`isolated = False`)."""

    isolated = False

    async def run(self, command: str, cwd: Path, timeout_s: float) -> ShellResult:
        return await run_process(command, cwd, timeout_s)


def shell_tools(ctx: ToolContext) -> list[Tool]:
    @guarded
    async def run_shell(run: RunContext, command: str) -> str:
        """Run a shell command in the working tree; returns the exit code and output."""
        blocks = list(ctx.guard.command(command))
        if blocks:
            return refused("; ".join(b.why for b in blocks))
        asks = list(ctx.guard.asks(command))
        if asks and not run.tool_call_approved:  # a person approves this command first
            raise ApprovalRequired(metadata={"ask_rules": asks})
        result = await ctx.sandbox.run(command, ctx.tree, ctx.shell_timeout_s)
        status = "timed out" if result.timed_out else f"exit {result.exit_code}"
        return f"{status}\nstdout:\n{_cap(result.stdout)}\nstderr:\n{_cap(result.stderr)}"

    return [Tool(run_shell, takes_ctx=True)]


def _cap(text: str) -> str:
    """A second cap, for sandboxes that return output uncapped."""
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n[... {len(text) - MAX_OUTPUT} more characters]"
