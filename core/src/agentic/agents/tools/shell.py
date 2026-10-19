"""Running shell commands for an agent: guard first, then a sandbox.

The guard (`command`) refuses destructive and denied commands before anything
runs. The command then runs through a `Sandbox`; the real, OS-isolated ones are
M1-32 (Linux/macOS) and M1-33 (Windows). `LocalProcessSandbox` runs a plain
subprocess with NO isolation - for tests and development only, which is why
`build_tools` refuses it unless the caller explicitly allows it.

`run_shell` is async: when the run stops (time limit, budget, kill switch) the
call is cancelled and the sandbox kills the command and everything it started.
"""

import asyncio
import os
import re
import signal
import subprocess
from pathlib import Path

from pydantic_ai import Tool

from agentic.agents.tools.context import ShellResult, ToolContext, guarded, refused

MAX_OUTPUT = 50_000
KILL_WAIT_S = 5.0
# Environment variables a command never sees: API keys, tokens and passwords.
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.IGNORECASE)


class _Capture:
    """One output stream, kept up to MAX_OUTPUT bytes; the rest is read and counted,
    so a chatty command cannot fill memory or block on a full pipe."""

    def __init__(self) -> None:
        self.kept = bytearray()
        self.dropped = 0

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(65_536):
            room = MAX_OUTPUT - len(self.kept)
            self.kept += chunk[:room]
            self.dropped += max(0, len(chunk) - room)

    def text(self) -> str:
        text = self.kept.decode("utf-8", errors="replace")
        return text + (f"\n[... {self.dropped} more bytes]" if self.dropped else "")


class LocalProcessSandbox:
    """A subprocess in the tree's folder - no OS isolation (`isolated = False`)."""

    isolated = False

    async def run(self, command: str, cwd: Path, timeout_s: float) -> ShellResult:
        group = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={k: v for k, v in os.environ.items() if not SECRET_NAME.search(k)},
            **group,
        )
        out, err = _Capture(), _Capture()
        finished = asyncio.ensure_future(
            asyncio.gather(out.drain(process.stdout), err.drain(process.stderr), process.wait())
        )
        try:
            done, _ = await asyncio.wait({finished}, timeout=timeout_s)
        except asyncio.CancelledError:  # the run was stopped: the command goes with it
            await _stop(process, finished)
            raise
        if not done:
            await _stop(process, finished)
            return ShellResult(exit_code=None, stdout=out.text(), stderr=err.text(), timed_out=True)
        return ShellResult(
            exit_code=process.returncode, stdout=out.text(), stderr=err.text(), timed_out=False
        )


async def _stop(process: asyncio.subprocess.Process, finished: asyncio.Future) -> None:
    """Kill the command and everything it started, not just the shell, then wait
    (briefly) for it to exit and its pipes to close."""
    if process.returncode is None:
        if os.name == "nt":
            kill = ["taskkill", "/T", "/F", "/PID", str(process.pid)]
            killer = await asyncio.create_subprocess_exec(
                *kill, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            await killer.wait()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # already gone
    try:
        await asyncio.wait_for(asyncio.shield(finished), KILL_WAIT_S)
    except TimeoutError:
        finished.cancel()


def shell_tools(ctx: ToolContext) -> list[Tool]:
    @guarded
    async def run_shell(command: str) -> str:
        """Run a shell command in the working tree; returns the exit code and output."""
        blocks = list(ctx.guard.command(command))
        if blocks:
            return refused("; ".join(b.why for b in blocks))
        result = await ctx.sandbox.run(command, ctx.tree, ctx.shell_timeout_s)
        status = "timed out" if result.timed_out else f"exit {result.exit_code}"
        return f"{status}\nstdout:\n{_cap(result.stdout)}\nstderr:\n{_cap(result.stderr)}"

    return [Tool(run_shell)]


def _cap(text: str) -> str:
    """A second cap, for sandboxes that return output uncapped."""
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n[... {len(text) - MAX_OUTPUT} more characters]"
