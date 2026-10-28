"""Resource limits for one agent command (M3-10): what policy's `sandbox.limits` allows a
command - CPU time, memory, the size of a file it writes, and how many processes it runs.

How each is enforced (the ruling; the CLI operations runbook says the same):

| limit       | Linux and macOS                                | native Windows (best effort)  |
|-------------|------------------------------------------------|-------------------------------|
| CPU time    | hard `RLIMIT_CPU`, per process (the launcher)  | Job: user time, all processes |
| memory      | the watchdog: the tree's resident memory       | Job: committed job memory     |
| file size   | hard `RLIMIT_FSIZE` (the launcher)             | not enforced                  |
| processes   | the watchdog's count of the tree, and a hard   | Job: active processes         |
|             | `RLIMIT_NPROC` backstop for non-root users     |                               |

  * **The launcher** (`launcher`) is `python -I -S -c <setrlimit, then exec>` in front of
    the command - inside `srt`, so srt's own Node process is never limited. Hard limits
    (soft = hard, CPU's hard one second later for SIGXCPU to land): the command cannot
    raise them. `-I` keeps a module an agent planted in its tree from being imported.
    `RLIMIT_NPROC` counts every task of the user - each thread on Linux, each process on
    macOS - so it is set to what the user runs now plus wide fork-bomb-only headroom
    (`nproc_backstop`): a backstop against a fork bomb taking the laptop down, never the
    limit itself. Root, as in a CI container, is exempt from it; the watchdog's count of
    the tree is the limit everywhere.
  * **No `RLIMIT_AS`**: Node (V8) and the JVM reserve far more address space than they
    use, so an address-space limit would break `npm test` long before memory ran out.
    cgroups v2 (`memory.max`, `pids.max`) would be exact, but are not delegated inside
    srt's bubblewrap, a Docker CI container or macOS.
  * **The watchdog** (`Watchdog`) reads the command's process tree every `POLL_S`
    (`/proc` on Linux, `ps` on macOS) and the caller kills the tree past a limit. The
    tree is what runs below the started process (srt, or the command's shell): a burst
    faster than one poll can overshoot briefly. Under srt the count includes the
    sandbox's two or three helper processes.
  * **Windows**: the limits are set on the Job Object every command's tree already runs
    in (M2-22). A process started in the moment before the job holds the child escapes
    them. There is no file-size limit. The job's completion port names each limit it
    enforced (`job_limit`), so the agent is told when a command failed at one.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Collection, Iterable, Sequence
from pathlib import Path

from agentic.backends.processes import descendants
from agentic.policy.model import ResourceLimits

MB = 1024 * 1024
#: How often the watchdog reads the tree: /proc is cheap; elsewhere each read runs `ps`.
POLL_S = 0.1 if Path("/proc").is_dir() else 0.5
#: The least room `RLIMIT_NPROC` leaves above what the user already runs: it only stops a
#: fork bomb, and Linux charges every thread of the user (an IDE or browser holds hundreds).
NPROC_HEADROOM = 4096
#: setrlimit each given limit (soft, hard), never above the hard limit already in place,
#: then become the command. argv: cpu seconds, file bytes, processes ("-": none), command.
LAUNCHER = (
    "import os, resource, sys\n"
    "names = ('RLIMIT_CPU', 'RLIMIT_FSIZE', 'RLIMIT_NPROC')\n"
    "for name, given in zip(names, sys.argv[1:4]):\n"
    "    if given == '-' or not hasattr(resource, name):\n"
    "        continue\n"
    "    which = getattr(resource, name)\n"
    "    soft, hard = int(given), int(given) + (1 if name == 'RLIMIT_CPU' else 0)\n"
    "    _, now = resource.getrlimit(which)\n"
    "    if now != resource.RLIM_INFINITY:\n"
    "        soft, hard = min(soft, now), min(hard, now)\n"
    "    resource.setrlimit(which, (soft, hard))\n"
    "os.execvp(sys.argv[4], sys.argv[4:])\n"
)
#: How a command stopped by a kernel limit ends: killed by the signal, or a shell's
#: 128 + signal for its last command.
_SIGNALS = {24: "cpu", 25: "file size"}  # SIGXCPU, SIGXFSZ (Linux and macOS)
_EFBIG = "File too large"  # strerror(EFBIG) on Linux and macOS


def launcher(
    limits: ResourceLimits, argv: Sequence[str], *, posix: bool | None = None
) -> list[str]:
    """`argv` behind the rlimit launcher (POSIX, when a limit it sets is given)."""
    posix = os.name != "nt" if posix is None else posix
    if not posix or not (limits.cpu_seconds or limits.max_file_mb or limits.processes):
        return list(argv)
    backstop = nproc_backstop(limits.processes) if limits.processes else None
    nproc = "-" if backstop is None else str(backstop)
    return [
        interpreter(),
        "-I",
        "-S",
        "-c",
        LAUNCHER,
        str(limits.cpu_seconds) if limits.cpu_seconds else "-",
        str(limits.max_file_mb * MB) if limits.max_file_mb else "-",
        nproc,
        *argv,
    ]


def interpreter() -> str:
    """This Python's own binary: inside a sandbox the virtual environment around it may
    not be readable, its interpreter is (`-S`: no site-packages needed)."""
    return os.path.realpath(sys.executable)


def nproc_backstop(processes: int) -> int | None:
    """The `RLIMIT_NPROC` for a command allowed `processes`: what the user runs now plus
    fork-bomb-only headroom (`NPROC_HEADROOM`, or ten times the limit if larger), so the
    user's other programs can start threads meanwhile. The watchdog's count of the tree is
    the real limit. None for root (exempt) or when it cannot be counted."""
    if not hasattr(os, "getuid") or os.getuid() == 0:
        return None
    running = user_tasks(os.getuid())
    if running is None:
        return None
    return running + max(10 * processes, NPROC_HEADROOM)


def user_tasks(uid: int, proc: Path = Path("/proc")) -> int | None:
    """What `uid` runs now as the kernel charges it to `RLIMIT_NPROC`: every task (thread)
    on Linux, every process on macOS (`ps`); None when it cannot be counted."""
    if proc.is_dir():
        count = 0
        for entry in proc.iterdir():
            try:
                if not entry.name.isdigit() or entry.stat().st_uid != uid:
                    continue
            except OSError:
                continue
            try:
                count += max(1, len(os.listdir(entry / "task")))
            except OSError:
                count += 1  # it ended meanwhile, or hides its tasks: one at least
        return count
    try:
        listing = subprocess.run(
            ["ps", "-U", str(uid), "-o", "pid="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return sum(1 for line in listing.splitlines() if line.strip())


def kernel_limit(limits: ResourceLimits, returncode: int | None, stderr: str = "") -> str | None:
    """The limit a finished command was stopped at by the kernel (POSIX), or None: killed
    by SIGXCPU or SIGXFSZ, or - a program that ignores SIGXFSZ, as Python does - failing
    with EFBIG ("File too large") under a file-size limit."""
    if returncode is None or returncode == 0 or os.name == "nt":
        return None
    signal = -returncode if returncode < 0 else returncode - 128 if returncode > 128 else 0
    kind = _SIGNALS.get(signal)
    if kind is None and _EFBIG in stderr:
        kind = "file size"
    if kind == "cpu" and limits.cpu_seconds:
        return f"cpu time ({limits.cpu_seconds} s)"
    if kind == "file size" and limits.max_file_mb:
        return f"file size ({limits.max_file_mb} MB)"
    return None


def job_limit(limits: ResourceLimits, returncode: int | None, hits: Collection[str]) -> str | None:
    """The Job Object limit (native Windows) a failed command ran into, or None. `hits`:
    what the job enforced (`ProcessTree.limits_hit`). A command that coped with a refused
    allocation or process start and still exited 0 keeps its exit code."""
    if returncode is None or returncode == 0:
        return None
    if "cpu" in hits and limits.cpu_seconds:
        return f"cpu time ({limits.cpu_seconds} s)"
    if "memory" in hits and limits.memory_mb:
        return f"memory ({limits.memory_mb} MB)"
    if "processes" in hits and limits.processes:
        return f"processes ({limits.processes})"
    return None


class Watchdog:
    """Polls the process tree of `pid` against the memory and process limits (POSIX; on
    Windows the Job Object holds them). `include_root`: `pid` is the command itself (it
    is not when a sandbox runner such as srt started the command below it)."""

    def __init__(self, limits: ResourceLimits, pid: int, *, include_root: bool = False) -> None:
        self.limits, self.pid, self.include_root = limits, pid, include_root
        self.descendants: Callable[[int], list[int]] = descendants
        self.resident: Callable[[Iterable[int]], int] = resident_bytes

    @property
    def active(self) -> bool:
        return os.name != "nt" and bool(self.limits.memory_mb or self.limits.processes)

    def check(self) -> str | None:
        """The limit the tree is past, or None."""
        below = self.descendants(self.pid)
        if self.include_root:
            below = [self.pid, *below]
        if self.limits.processes and len(below) > self.limits.processes:
            return f"processes ({self.limits.processes})"
        if self.limits.memory_mb and self.resident(below) > self.limits.memory_mb * MB:
            return f"memory ({self.limits.memory_mb} MB)"
        return None


def resident_bytes(pids: Iterable[int]) -> int:
    """The resident memory of `pids` together: `/proc/<pid>/statm` on Linux, `ps` else."""
    pids = list(pids)
    if not pids:
        return 0
    if Path("/proc").is_dir():
        page = os.sysconf("SC_PAGE_SIZE")
        total = 0
        for pid in pids:
            try:
                fields = Path(f"/proc/{pid}/statm").read_text().split()
                total += int(fields[1]) * page
            except (OSError, IndexError, ValueError):
                continue
        return total
    try:
        listing = subprocess.run(
            ["ps", "-o", "rss=", "-p", ",".join(map(str, pids))],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return sum(int(n) * 1024 for n in re.findall(r"\d+", listing))
