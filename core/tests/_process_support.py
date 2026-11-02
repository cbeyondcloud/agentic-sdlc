"""Helpers for the process-cleanup tests (M2-22): is a pid alive, wait until it is gone.
A unique module name, so it never collides with another folder's helpers."""

import os
import subprocess
import time
from pathlib import Path

#: Linux's process table; macOS has none, and `ps` is asked there.
PROC = Path("/proc")


def alive(pid: int) -> bool:
    """Whether `pid` runs. A zombie no one reaped (a container's init) counts as dead."""
    if os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    if PROC.is_dir():
        try:
            stat = (PROC / str(pid) / "stat").read_text()
        except OSError:
            return False
        return stat.rsplit(")", 1)[-1].split()[0] != "Z"
    return ps_state(pid) not in ("", "Z")


def ps_state(pid: int) -> str:
    """The first letter of `pid`'s state from `ps` (POSIX without /proc: macOS), or "" when
    there is no such process. `kill(pid, 0)` cannot tell: it succeeds for a zombie."""
    listing = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    if listing.returncode not in (0, 1) or listing.stderr.strip():  # 1: no such process
        raise RuntimeError(f"ps could not tell whether {pid} runs: {listing.stderr.strip()}")
    return listing.stdout.strip()[:1]


def gone(pid: int, within_s: float = 10.0) -> bool:
    """Whether `pid` stops running within `within_s` seconds."""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


# A parent that prints the pid of a child it started, then waits.
PARENT = """\
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
time.sleep(60)
"""
