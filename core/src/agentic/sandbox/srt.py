"""The OS sandbox: Anthropic `srt` (sandbox-runtime, Apache-2.0) on every OS (D38).

macOS uses Seatbelt, Linux bubblewrap with a network namespace; Windows runs the
framework inside WSL2, where this is the Linux sandbox. `srt` is an external Node
tool, pinned at SRT_VERSION, called as a subprocess:

    srt --settings <file> -- bash -c "<command>"

The command goes as separate arguments: one string would be read as a program name
(spike S6). Stopping the call stops `srt` and everything under it (`run_process`).

Each sandbox has its own temp folder beside its settings, writable inside the
sandbox and named in TMPDIR/TMP/TEMP: srt's default temp folder may not exist, and
temp files must never land in the run's tree, where a checkpoint would commit them.
"""

import json
import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from agentic.policy.model import ResourceLimits
from agentic.sandbox.limits import launcher
from agentic.sandbox.process import ShellResult, run_process, safe_env
from agentic.sandbox.rules import SandboxRules, srt_settings

SRT_VERSION = "0.0.78"
SETTINGS_FILE = "srt-settings.json"


class SrtSandbox:
    """Runs each command under `srt` with one fixed set of rules."""

    isolated = True

    def __init__(
        self,
        rules: SandboxRules,
        settings_dir: Path,
        *,
        srt: Sequence[str] = ("srt",),
        pass_env: Sequence[str] = (),  # policy's `sandbox.pass_env` (DF-365)
        limits: ResourceLimits | None = None,  # policy's `sandbox.limits` (M3-10)
    ) -> None:
        self.rules = rules
        self.settings = Path(settings_dir) / SETTINGS_FILE
        self.tmp = Path(settings_dir) / "tmp"
        self.srt = tuple(srt)
        self.pass_env = tuple(pass_env)
        self.limits = limits or ResourceLimits()
        self._written = False

    def _write_settings(self) -> None:
        """Written once, atomically: a half-written file must never be read by srt.
        The file itself is denied writing: were it under a writable path, one command
        could rewrite the rules of the next (DF-71)."""
        rules = replace(
            self.rules,
            write=(*self.rules.write, self.tmp),
            deny_write=(*self.rules.deny_write, self.settings),
        )
        text = json.dumps(srt_settings(rules), indent=2) + "\n"
        self.tmp.mkdir(parents=True, exist_ok=True)
        temp = self.settings.with_name(f"{SETTINGS_FILE}.{os.getpid()}.tmp")
        temp.write_bytes(text.encode("utf-8"))
        os.replace(temp, self.settings)
        self._written = True

    def command_prefix(self, *, proxy_everything: bool = False) -> list[str]:
        """What runs a program under this sandbox: append the program and its arguments.

        `proxy_everything` is for a whole agent CLI process (M1-29, DF-65): srt exempts
        loopback and private address ranges from its proxy (NO_PROXY), but inside the
        sandbox's own network those are unreachable - a gateway at 127.0.0.1 or a
        private IP could not be called. Cleared, every request goes through srt's
        proxy and its allowlist. Agent shell commands keep srt's default, so a test
        suite still reaches a server it started inside the sandbox."""
        if not self._written:
            self._write_settings()
        names = [f"{name}={self.tmp}" for name in ("TMPDIR", "TMP", "TEMP")]
        if proxy_everything:
            names += ["NO_PROXY=", "no_proxy="]
        return [*self.srt, "--settings", str(self.settings), "--", "env", *names]

    async def run(self, command: str, cwd: Path, timeout_s: float) -> ShellResult:
        """The command under srt, behind the rlimit launcher inside the sandbox (srt's own
        Node process is never limited), its tree watched for memory and processes."""
        inner = launcher(self.limits, ["bash", "-c", command])
        return await run_process(
            [*self.command_prefix(), *inner],
            cwd,
            timeout_s,
            env=safe_env(pass_through=self.pass_env),
            limits=self.limits,
            launch=False,
        )
