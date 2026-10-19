"""Running shell commands for an agent: guard first, then a sandbox.

The guard (`command`) refuses destructive and denied commands before anything
runs. The command then runs through a `Sandbox`; the real, OS-isolated ones are
M1-32 (Linux/macOS) and M1-33 (Windows). `LocalProcessSandbox` runs a plain
subprocess with NO isolation - for tests and development only, which is why
`build_tools` refuses it unless the caller explicitly allows it.
"""

import os
import signal
import subprocess
from pathlib import Path

from pydantic_ai import Tool

from agentic.agents.tools.context import ShellResult, ToolContext, refused

MAX_OUTPUT = 50_000


class LocalProcessSandbox:
    """A subprocess in the tree's folder - no OS isolation (`isolated = False`)."""

    isolated = False

    def run(self, command: str, cwd: Path, timeout_s: float) -> ShellResult:
        group = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **group,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            stdout, stderr = process.communicate()
            return ShellResult(exit_code=None, stdout=stdout, stderr=stderr, timed_out=True)
        return ShellResult(
            exit_code=process.returncode, stdout=stdout, stderr=stderr, timed_out=False
        )


def _kill_tree(process: subprocess.Popen) -> None:
    """Stop the command and everything it started, not just the shell."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
    else:
        os.killpg(process.pid, signal.SIGKILL)


def shell_tools(ctx: ToolContext) -> list[Tool]:
    def run_shell(command: str) -> str:
        """Run a shell command in the working tree; returns the exit code and output."""
        blocks = list(ctx.guard.command(command))
        if blocks:
            return refused("; ".join(b.why for b in blocks))
        result = ctx.sandbox.run(command, ctx.tree, ctx.shell_timeout_s)
        status = "timed out" if result.timed_out else f"exit {result.exit_code}"
        return f"{status}\nstdout:\n{_cap(result.stdout)}\nstderr:\n{_cap(result.stderr)}"

    return [Tool(run_shell)]


def _cap(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n[... {len(text) - MAX_OUTPUT} more characters]"
