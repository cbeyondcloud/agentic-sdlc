"""Running one command as a process tree that can always be stopped.

Every sandbox runs its command through `run_process`: the command gets its own
process group (POSIX session / Windows process group), its output is capped, and
on a timeout or when the call is cancelled (time limit, budget, kill switch) the
WHOLE tree is killed - the shell, `srt`, bubblewrap and everything they started
(DF-27).
"""

import asyncio
import logging
import os
import re
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT = 50_000
KILL_WAIT_S = 5.0
# Environment variables a command never sees: API keys, tokens, passwords, agent sockets
# (DF-66). The short words count only as a whole part of the name: `GITHUB_PAT` and
# `SSH_AUTH_SOCK` are secrets, `PATH` and `GIT_AUTHOR_NAME` are not.
SECRET_NAME = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|PRIVATE|COOKIE"
    r"|(?:^|_)(?:AUTH|PAT|DSN|PASS|CREDS)(?:_|$)",
    re.IGNORECASE,
)
# A value carrying a credential, whatever its name: a URL with `user:password@`, or with
# a long user part (`https://<token>@host`); `ssh://git@host` is a user name, not a secret.
# A scheme starts only where no scheme character precedes it, and the userinfo parts are
# bounded, so a long value is scanned in linear time (DF-333).
_URL_CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]*://"
    r"(?:[^/\s@:]{0,256}:[^/\s@]{1,4096}|[^/\s@:]{20,4096})@"
)
# Windows: `cmd.exe` and CreateProcess search the working folder before PATH unless this
# is set, so an agent's `pytest.cmd` in the tree would run in place of the real one (DF-68).
_NO_CWD_SEARCH = "NoDefaultCurrentDirectoryInExePath"
_ABSOLUTE_ENTRY = re.compile(r"^(?:[/\\]|[A-Za-z]:[/\\]|%)")
_POSIX_DEFAULT_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
# Withheld variables whose loss breaks a tool's network or sign-in: named as a warning.
_NOTICEABLE = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "SSH_AUTH_SOCK"})
_log = logging.getLogger("agentic.sandbox")


@dataclass(frozen=True)
class ShellResult:
    exit_code: int | None  # None when the command was stopped (timed out)
    stdout: str
    stderr: str
    timed_out: bool


_REPORTED: set[str] = set()  # withheld names already logged by this process


def secret_value(value: str) -> bool:
    """Whether an environment value carries a credential (a URL with one)."""
    return bool(_URL_CREDENTIAL.search(value))


def _withheld(name: str, value: str) -> bool:
    return bool(SECRET_NAME.search(name)) or secret_value(value)


def withheld_names(environ: Mapping[str, str] | None = None) -> list[str]:
    """The variables `safe_env` leaves out of `environ` (default: this process's), by
    name only - never a value - for `doctor` and the run log (DF-334)."""
    source = os.environ if environ is None else environ
    return sorted(name for name, value in source.items() if _withheld(name, value))


def _report(names: list[str]) -> None:
    """Log the withheld names this process has not logged yet: a developer whose proxy
    or SSH agent stops working inside a run can see why (values are never logged)."""
    new = [name for name in names if name not in _REPORTED]
    if not new:
        return
    _REPORTED.update(new)
    noticeable = any(name.upper() in _NOTICEABLE for name in new)
    _log.log(
        logging.WARNING if noticeable else logging.INFO,
        "withheld from agent commands (a secret name or a credential in the value): %s",
        ", ".join(new),
    )


def _path_entries(value: str) -> list[str]:
    """The absolute entries of a PATH value; a quoted entry is judged without its quotes
    (Windows allows `"C:\\Program Files\\Tool"`, DF-336) and kept as written."""
    return [e for e in value.split(os.pathsep) if _ABSOLUTE_ENTRY.match(e.strip('"'))]


def _default_path(source: Mapping[str, str], windows: bool) -> str:
    """A PATH of system folders, for a PATH left with no absolute entry: an empty PATH
    means the current folder - the agent's own tree - to some shells (DF-336)."""
    if windows:
        root = next((v for k, v in source.items() if k.upper() == "SYSTEMROOT"), "C:\\Windows")
        return os.pathsep.join([f"{root}\\System32", root, f"{root}\\System32\\Wbem"])
    return os.pathsep.join(_POSIX_DEFAULT_PATH)


def safe_env(
    environ: Mapping[str, str] | None = None, *, windows: bool | None = None
) -> dict[str, str]:
    """The environment for an agent's command: `environ` (default: this process's)
    without secret-named variables or credential-carrying values (their names are
    logged once, never their values), with no relative entry in PATH (a folder the agent
    can write), and on Windows with the working folder never searched for a program."""
    source = os.environ if environ is None else environ
    windows = os.name == "nt" if windows is None else windows
    env = {k: v for k, v in source.items() if not _withheld(k, v)}
    _report(sorted(set(source) - set(env)))
    for name in [k for k in env if k.upper() == "PATH"]:
        entries = _path_entries(env[name])
        env[name] = os.pathsep.join(entries) if entries else _default_path(source, windows)
    if windows:
        for name in [k for k in env if k.upper() == _NO_CWD_SEARCH.upper()]:
            del env[name]
        env[_NO_CWD_SEARCH] = "1"
    return env


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


async def run_process(
    command: str | Sequence[str],
    cwd: Path,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
) -> ShellResult:
    """Run `command` (a string through the shell, a sequence exec'd) in `cwd`."""
    group = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    common = {
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": dict(env) if env is not None else safe_env(),
        **group,
    }
    if isinstance(command, str):
        process = await asyncio.create_subprocess_shell(command, **common)
    else:
        process = await asyncio.create_subprocess_exec(*command, **common)
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
