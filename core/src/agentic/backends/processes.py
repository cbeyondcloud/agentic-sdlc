"""Stopping a process and every process it started - the one helper every backend uses
(M2-57, M2-22, DF-451; the approach of the claude-code backend, review I4).

An agent CLI starts shell commands, language servers and helpers of its own, and a shell
command may start a server in the background; killing only the CLI would leave them
running after a time limit, a stopped run or a Ctrl-C. So:

  * a child gets a process group of its own (`spawn_group`; for a CLI an SDK starts,
    `session_leader`), and is adopted into a `ProcessTree` as soon as it starts;
  * on Windows, adopting puts the child in a **Job Object** that kills every process in
    it when its handle closes (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`). Whatever the child
    starts afterwards is in the job - an orphan whose parent has exited included, which
    `taskkill /T` cannot find - and the job dies with this process however it ends: a
    Ctrl-C, a closed console, the CLI killed from Task Manager;
  * `ProcessTree.kill` kills the tree: on Windows `taskkill /T` (what started before the
    adoption) and the job; on POSIX every descendant (found by parent id before anything
    dies, because orphans are re-parented), the child's process group and, on Linux, its
    session (an orphan that left the group but not the session);
  * each tree registers in the innermost `process_scope`. The engine opens one per agent
    step, so whatever a step started dies when the step ends, whichever backend ran it.
    The scope is a context variable: it reaches `asyncio` tasks and the worker threads
    synchronous tools run in (a code-intelligence build, `run_contained`).

A process that has exited is never signalled by its pid, which the OS may have given to
someone else; only its group, session or job is - and on POSIX not even those once
`settle` found nothing left in them, or once its pid runs another process (a pid is not
reused while a group or session of that id has members, so the group or session is then
the newcomer's). Left open: a newcomer that leads a group, starts a child and exits, all
before the step ends, leaves a group of that id with no process of that pid. What POSIX
cannot reach: a grandchild
that starts a session of its own AND outlives its parent is re-parented to init and
leaves no trace; on Linux the sandbox's PID namespace (`srt`, bubblewrap) ends it.
"""

import contextvars
import ctypes
import logging
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple, Protocol

KILL_WAIT_S = 5.0
_SESSION = "import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])"
_log = logging.getLogger("agentic.backends.processes")


class Started(Protocol):
    """A started child: `subprocess.Popen`, an `asyncio` or an `anyio` process."""

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...


def spawn_group() -> dict[str, object]:
    """The keyword arguments (`subprocess.Popen`, `asyncio.create_subprocess_*`) that
    start a child in a process group of its own: a new session on POSIX, a new process
    group on Windows (where Ctrl-C then reaches the CLI alone, which stops the child)."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def session_leader(argv: Sequence[str]) -> list[str]:
    """`argv`, started as the leader of a new session on POSIX (so its process group is
    its own and can be killed whole); unchanged on Windows. For a CLI that an SDK starts
    with arguments we cannot add to."""
    if os.name == "nt":
        return list(argv)
    return [sys.executable, "-c", _SESSION, *argv]


# ── one tree ──────────────────────────────────────────────────────────────────


class ProcessTree:
    """One child and every process it starts: contained from adoption, killed whole.
    Thread-safe; `kill` and `close` may be called any number of times."""

    def __init__(self) -> None:
        self.pid: int | None = None
        self._process: Started | None = None
        self._job: _Job | None = None
        self._scope: ProcessScope | None = None
        self._settled = False  # exited with nothing left: never signal its group again
        self._lock = threading.RLock()

    def adopt(self, process: Started) -> "ProcessTree":
        """Contain `process` (just started) and register it in the current scope."""
        with self._lock:
            self._process, self.pid = process, process.pid
            if os.name == "nt" and self._job is None and not self._exited():
                self._job = _Job.containing(process.pid)
        scope = _SCOPE.get()
        if scope is not None:
            scope.add(self)
        return self

    @property
    def contained(self) -> bool:
        """Whether the OS holds the tree together (Windows: the child is in a job)."""
        return self._job is not None

    def settle(self) -> bool:
        """After the child has exited: whether it left nothing running in its group
        (and, on Linux, its session). Then the tree is finished on POSIX: its pid, free
        for the OS to reuse, is never signalled again, not even as a group. A tree that
        left something running stays open for its scope to close. Windows needs no such
        care (the job holds the tree, not a pid)."""
        with self._lock:
            pid = self.pid
            if pid is None or not self._exited():
                return False
            if os.name == "nt" or self._settled:
                return self._settled
            if _reused(pid):
                self._settled = True
                return True
            try:
                os.killpg(pid, 0)
                return False  # its group still has members, which hold the id
            except ProcessLookupError:
                pass
            except (PermissionError, OSError):
                return False
            if Path("/proc").is_dir() and any(row.sid == pid for row in _table()):
                return False
            self._settled = True
            return True

    def kill(self) -> None:
        """Kill the child and everything below it. Harmless when they are gone."""
        with self._lock:
            pid = self.pid
            if pid is None or self._settled:
                return
            if not self._exited():  # its pid is still its own
                kill_tree(pid)
            elif os.name != "nt":
                if _reused(pid):  # nothing of it is left: the group and session are another's
                    self._settled = True
                    return
                _kill_group(pid)  # what it left behind in its group and session
            if os.name != "nt":
                _kill_session(pid)
            if self._job is not None:
                self._job.terminate()

    def kill_below(self) -> None:
        """Kill what the live child started in process groups of their own (an agent
        CLI's shell commands), before the CLI is shut down gracefully - while they can
        still be found by parent id (POSIX re-parents them when it exits). What stays in
        the child's own group, the CLI itself included, is left to `close`. On Windows
        the job keeps them all."""
        with self._lock:
            pid = self.pid
            if pid is None or os.name == "nt" or self._exited():
                return
            below = set(descendants(pid))
            for row in _table():
                if row.pid in below and row.pgid != pid:
                    _signal(row.pid)

    def close(self) -> None:
        """The child's work is over: nothing it started may outlive it."""
        with self._lock:
            self.kill()
            if self._job is not None:
                self._job.close()
                self._job = None
            scope, self._scope = self._scope, None
        if scope is not None:
            scope.discard(self)

    def __enter__(self) -> "ProcessTree":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _exited(self) -> bool:
        process = self._process
        if process is None:
            return True
        poll = getattr(process, "poll", None)  # Popen updates returncode only when asked
        code = poll() if callable(poll) else process.returncode
        return code is not None


def kill_tree(pid: int) -> None:
    """Kill the live process `pid` and every process below it, for a caller that holds
    only a pid (prefer `ProcessTree`). A process already gone is no error."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    for target in [*descendants(pid), pid]:
        _signal(target)
    _kill_group(pid)  # a session leader's group is its own


def descendants(pid: int) -> list[int]:
    """Every process below `pid` (POSIX): from /proc on Linux, else from `ps`."""
    children: dict[int, list[int]] = {}
    for row in _table():
        children.setdefault(row.ppid, []).append(row.pid)
    found: list[int] = []
    queue = [pid]
    while queue:
        for child in children.get(queue.pop(), []):
            if child not in found:
                found.append(child)
                queue.append(child)
    return found


class _Row(NamedTuple):
    pid: int
    ppid: int
    pgid: int
    sid: int | None  # None where `ps` names no session (macOS)


def _table() -> list[_Row]:
    """Every process (POSIX): from /proc on Linux, else from `ps`."""
    rows: list[_Row] = []
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat_line = (entry / "stat").read_text()
            except OSError:
                continue
            # after the name: state, ppid, pgrp, session, ...
            fields = stat_line.rsplit(")", 1)[-1].split()
            if len(fields) > 3:
                rows.append(_Row(int(entry.name), int(fields[1]), int(fields[2]), int(fields[3])))
        return rows
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True, check=False
    ).stdout
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            rows.append(_Row(int(parts[0]), int(parts[1]), int(parts[2]), None))
    return rows


def _signal(pid: int) -> None:
    if pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _reused(pid: int) -> bool:
    """Whether a process runs as `pid`, which belonged to a child that has exited (and
    was reaped): then the OS gave the pid to someone else. POSIX systems do not reuse a
    pid while a process group or session of that id has members, so the child left
    nothing in them, and a group or session of that id now is the newcomer's."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # it exists, owned by someone else
    return True


def _kill_group(pgid: int) -> None:
    """Kill the process group `pgid` (led by that pid, or left over from its leader)."""
    if pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill_session(sid: int) -> None:
    """Kill what is left of the session `sid` (Linux; macOS's `ps` names no session, so
    there the process group is the reach)."""
    if sid == os.getsid(0) or not Path("/proc").is_dir():
        return
    for row in _table():
        if row.sid == sid:
            _signal(row.pid)


# ── the scope ─────────────────────────────────────────────────────────────────


class ProcessScope:
    """The trees started while it is current; `close` ends every one still open."""

    def __init__(self) -> None:
        self._trees: set[ProcessTree] = set()
        self._closed = False
        self._lock = threading.Lock()

    @property
    def trees(self) -> frozenset[ProcessTree]:
        with self._lock:
            return frozenset(self._trees)

    def add(self, tree: ProcessTree) -> None:
        """Register `tree`; once the scope is closed (its step is over - a tool thread
        that was still waiting starts a process late), the tree is closed at once."""
        with self._lock:
            closed = self._closed
            if not closed:
                self._trees.add(tree)
                tree._scope = self
        if closed:
            tree.close()

    def discard(self, tree: ProcessTree) -> None:
        with self._lock:
            self._trees.discard(tree)

    def close(self) -> None:
        with self._lock:
            trees, self._trees, self._closed = list(self._trees), set(), True
        for tree in trees:
            try:
                tree.close()
            except Exception:  # noqa: BLE001 - one failure must not spare the others
                _log.warning("a process tree could not be closed", exc_info=True)


_SCOPE: contextvars.ContextVar[ProcessScope | None] = contextvars.ContextVar(
    "agentic_process_scope", default=None
)


def current_scope() -> ProcessScope | None:
    return _SCOPE.get()


@contextmanager
def process_scope() -> Iterator[ProcessScope]:
    """A scope for the trees started inside it (and in tasks and tool threads it
    starts); every one still open is closed when it ends, however it ends."""
    scope = ProcessScope()
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)
        scope.close()


# ── running a helper ──────────────────────────────────────────────────────────


def run_contained(
    argv: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
    input: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """`subprocess.run(argv, capture_output=True, text=True)` for a helper whose tree
    must not outlive it: in a group of its own, adopted (so the current scope can kill
    it from another thread), the whole tree killed at `timeout` (then TimeoutExpired)."""
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        **spawn_group(),
    )
    with process, ProcessTree().adopt(process) as tree:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            tree.kill()
            try:  # its pipes close once everything holding them is dead
                process.communicate(timeout=KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
            raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


# ── Windows job objects (kernel32 through ctypes: no native dependency) ───────

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_KERNEL32 = None


def _kernel32():
    """kernel32 with typed prototypes (handles are pointer-sized on 64-bit Windows)."""
    global _KERNEL32
    if _KERNEL32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        handle, dword, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
        prototypes = {
            "CreateJobObjectW": ([ctypes.c_void_p, ctypes.c_wchar_p], handle),
            "SetInformationJobObject": ([handle, ctypes.c_int, ctypes.c_void_p, dword], boolean),
            "OpenProcess": ([dword, boolean, dword], handle),
            "AssignProcessToJobObject": ([handle, handle], boolean),
            "TerminateJobObject": ([handle, ctypes.c_uint], boolean),
            "CloseHandle": ([handle], boolean),
        }
        for name, (argtypes, restype) in prototypes.items():
            function = getattr(k, name)
            function.argtypes, function.restype = argtypes, restype
        _KERNEL32 = k
    return _KERNEL32


def _last_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


class _Job:
    """A Windows Job Object that kills its processes when its last handle closes. The
    handle is not inheritable, so only this process holds it."""

    def __init__(self, handle: int) -> None:
        self.handle: int | None = handle

    @classmethod
    def containing(cls, pid: int) -> "_Job | None":
        """A new job holding `pid`, or None (logged) when Windows refuses: the tree is
        then killed by `taskkill /T` alone."""
        k = _kernel32()
        job = k.CreateJobObjectW(None, None)
        if not job:
            _log.warning("no job object for pid %s: %s", pid, _last_error())
            return None
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process = None
        try:
            size = ctypes.sizeof(limits)
            info = _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION
            if not k.SetInformationJobObject(job, info, ctypes.byref(limits), size):
                raise _last_error()
            process = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not process or not k.AssignProcessToJobObject(job, process):
                raise _last_error()
        except OSError as error:
            k.CloseHandle(job)
            _log.warning(
                "pid %s could not be put in a job object (%s): what it starts may outlive "
                "a stop if its parent exits first",
                pid,
                error,
            )
            return None
        finally:
            if process:
                k.CloseHandle(process)
        return cls(job)

    def terminate(self) -> None:
        if self.handle is not None:
            _kernel32().TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle is not None:
            _kernel32().CloseHandle(self.handle)  # KILL_ON_JOB_CLOSE: anything left dies
            self.handle = None
