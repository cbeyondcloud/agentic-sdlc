"""Stopping an agent CLI and everything it started (M2-57): what the time limit and a
stopped run rely on, on Windows and POSIX alike."""

import os
import subprocess
import sys
import time
from pathlib import Path

from agentic.backends.processes import kill_tree, session_leader

PARENT = """\
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
time.sleep(60)
"""


def alive(pid: int) -> bool:
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
    if stat.exists():  # a zombie no one reaped (a container's init) is dead too
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
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_kill_tree_stops_the_process_and_what_it_started(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(PARENT, encoding="utf-8")
    parent = subprocess.Popen(
        session_leader([sys.executable, str(script)]), stdout=subprocess.PIPE, text=True
    )
    try:
        grandchild = int(parent.stdout.readline())
        assert alive(grandchild)
        kill_tree(parent.pid)
        parent.wait(timeout=10)
        assert gone(grandchild)
    finally:
        parent.kill()
        parent.stdout.close()


def test_kill_tree_of_a_finished_process_is_harmless():
    done = subprocess.run([sys.executable, "-c", "pass"], check=True)
    kill_tree(999_999_999 if os.name != "nt" else 4_294_967_000)  # no such process
    assert done.returncode == 0


def test_the_session_leader_launcher_is_posix_only():
    argv = ["agent", "acp"]
    if os.name == "nt":
        assert session_leader(argv) == argv
    else:
        launched = session_leader(argv)
        assert launched[0] == sys.executable and launched[-2:] == argv
