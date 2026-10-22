"""Helpers for the process-cleanup tests (M2-22): is a pid alive, wait until it is gone.
A unique module name, so it never collides with another folder's helpers."""

import os
import time
from pathlib import Path


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
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            return stat.read_text().rsplit(")", 1)[-1].split()[0] != "Z"
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
