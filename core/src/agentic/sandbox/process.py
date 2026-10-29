"""Running one command as a process tree that can always be stopped.

Every sandbox runs its command through `run_process`: the command gets its own
process group (POSIX session / Windows process group and job object), its output is
capped, and on a timeout or when the call is cancelled (time limit, budget, kill switch)
the WHOLE tree is killed - the shell, `srt`, bubblewrap and everything they started
(DF-27) - through the shared helper (`agentic.backends.processes`, DF-451). What a
finished command left running in the background (a dev server a later command tests)
lives until the step's process scope closes, never longer (M2-22).
"""

import asyncio
import logging
import os
import re
import subprocess
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agentic.backends.processes import KILL_WAIT_S, ProcessTree, spawn_group
from agentic.policy.model import FRAMEWORK_ENV_PREFIX, ResourceLimits
from agentic.sandbox.limits import POLL_S, Watchdog, job_limit, kernel_limit, launcher

MAX_OUTPUT = 50_000
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
# possessive (never given back), so a long value is scanned in linear time (DF-333) with
# no length cap that a long credential could slip past (review F4).
_URL_CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]*+://"
    r"(?:[^/\s@:]*+:[^/\s@]++|[^/\s@:]{20,}+)@"
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
    exit_code: int | None  # None when the command was stopped (timed out, or at a limit)
    stdout: str
    stderr: str
    timed_out: bool
    limited: str | None = None  # the resource limit that stopped it (M3-10), e.g. "memory (512 MB)"


_REPORTED: set[str] = set()  # withheld names already logged by this process


def secret_value(value: str) -> bool:
    """Whether an environment value carries a credential (a URL with one)."""
    return bool(_URL_CREDENTIAL.search(value))


def _withheld(name: str, value: str) -> bool:
    return bool(SECRET_NAME.search(name)) or secret_value(value)


def _framework(name: str) -> bool:
    return name.upper().startswith(FRAMEWORK_ENV_PREFIX)


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
        "withheld from agent commands on purpose (a secret name or a credential in the "
        "value): %s - if a tool needs one (an authenticated proxy, the SSH agent), ask your "
        "org admin to list it in policy sandbox.pass_env; `agentic doctor` lists them",
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
    environ: Mapping[str, str] | None = None,
    *,
    windows: bool | None = None,
    pass_through: Collection[str] = (),
) -> dict[str, str]:
    """The environment for an agent's command: `environ` (default: this process's)
    without secret-named variables or credential-carrying values (their names are
    logged once, never their values), with no relative entry in PATH (a folder the agent
    can write), and on Windows with the working folder never searched for a program.

    `pass_through` names variables org policy lets through anyway (`sandbox.pass_env`,
    DF-365: an authenticated proxy, an SSH agent); matched case-blind on Windows, where
    names are. The framework's own `AGENTIC_*` credentials are never passed."""
    source = os.environ if environ is None else environ
    windows = os.name == "nt" if windows is None else windows
    fold = str.upper if windows else str
    allowed = {fold(name) for name in pass_through if not _framework(name)}
    env = {
        k: v
        for k, v in source.items()
        if not _withheld(k, v) or (fold(k) in allowed and not _framework(k))
    }
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
    *,
    limits: ResourceLimits | None = None,
    launch: bool = True,
) -> ShellResult:
    """Run `command` (a string through the shell, a sequence exec'd) in `cwd`.

    `limits` (M3-10, `agentic.sandbox.limits`): on POSIX the command runs behind the
    rlimit launcher (unless `launch` is False: a sandbox that put the launcher inside
    itself) and a watchdog kills its tree past the memory or process limit; on Windows
    its Job Object carries the limits. `ShellResult.limited` names the limit hit."""
    limits = limits or ResourceLimits()
    common = {
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": dict(env) if env is not None else safe_env(),
        **spawn_group(),
    }
    argv = command
    if launch and os.name != "nt" and limits.given():
        shell = ["/bin/sh", "-c", command] if isinstance(command, str) else list(command)
        argv = launcher(limits, shell)
    if isinstance(argv, str):
        process = await asyncio.create_subprocess_shell(argv, **common)
    else:
        process = await asyncio.create_subprocess_exec(*argv, **common)
    tree = ProcessTree().adopt(process, limits=limits)
    out, err = _Capture(), _Capture()
    finished = asyncio.ensure_future(
        asyncio.gather(out.drain(process.stdout), err.drain(process.stderr), process.wait())
    )
    dog = Watchdog(limits, process.pid, include_root=launch)  # srt itself is not the command
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    hit: str | None = None
    try:
        while True:
            left = deadline - loop.time()
            wait = min(POLL_S, left) if dog.active else left
            done, _ = await asyncio.wait({finished}, timeout=max(wait, 0))
            if done or left <= 0:
                break
            if dog.active and (hit := await asyncio.to_thread(dog.check)):
                break
    except asyncio.CancelledError:  # the run was stopped: the command goes with it
        await _stop(tree, finished)
        raise
    if hit is not None:
        await _stop(tree, finished)
        return ShellResult(None, out.text(), err.text(), timed_out=False, limited=hit)
    if not done:
        await _stop(tree, finished)
        return ShellResult(exit_code=None, stdout=out.text(), stderr=err.text(), timed_out=True)
    hits = tree.limits_hit()  # Windows: what its job enforced (before the job is closed)
    if tree.settle():  # nothing left in the background: finished now, not at step end
        tree.close()
    stderr = err.text()
    code = process.returncode
    return ShellResult(
        exit_code=code,
        stdout=out.text(),
        stderr=stderr,
        timed_out=False,
        limited=kernel_limit(limits, code, stderr) or job_limit(limits, code, hits),
    )


async def _stop(tree: ProcessTree, finished: asyncio.Future) -> None:
    """Kill the command and everything it started, not just the shell, then wait
    (briefly) for it to exit and its pipes to close."""
    await asyncio.to_thread(tree.kill)  # on Windows a tree kill takes a moment
    try:
        await asyncio.wait_for(asyncio.shield(finished), KILL_WAIT_S)
    except TimeoutError:
        finished.cancel()
