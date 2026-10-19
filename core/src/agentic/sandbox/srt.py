"""The OS sandbox: Anthropic `srt` (sandbox-runtime, Apache-2.0) on every OS (D38).

macOS uses Seatbelt, Linux bubblewrap with a network namespace; Windows runs the
framework inside WSL2, where this is the Linux sandbox. `srt` is an external Node
tool, pinned at SRT_VERSION, called as a subprocess:

    srt --settings <file> -- bash -c "<command>"

The command goes as separate arguments: one string would be read as a program name
(spike S6). Stopping the call stops `srt` and everything under it (`run_process`).
"""

import json
import os
from collections.abc import Sequence
from pathlib import Path

from agentic.sandbox.process import ShellResult, run_process
from agentic.sandbox.rules import SandboxRules, srt_settings

SRT_VERSION = "0.0.78"
SETTINGS_FILE = "srt-settings.json"


class SrtSandbox:
    """Runs each command under `srt` with one fixed set of rules."""

    isolated = True

    def __init__(
        self, rules: SandboxRules, settings_dir: Path, *, srt: Sequence[str] = ("srt",)
    ) -> None:
        self.rules = rules
        self.settings = Path(settings_dir) / SETTINGS_FILE
        self.srt = tuple(srt)
        self._written = False

    def _write_settings(self) -> None:
        """Written once, atomically: a half-written file must never be read by srt."""
        text = json.dumps(srt_settings(self.rules), indent=2) + "\n"
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        temp = self.settings.with_name(f"{SETTINGS_FILE}.{os.getpid()}.tmp")
        temp.write_bytes(text.encode("utf-8"))
        os.replace(temp, self.settings)
        self._written = True

    async def run(self, command: str, cwd: Path, timeout_s: float) -> ShellResult:
        if not self._written:
            self._write_settings()
        argv = [*self.srt, "--settings", str(self.settings), "--", "bash", "-c", command]
        return await run_process(argv, cwd, timeout_s)
