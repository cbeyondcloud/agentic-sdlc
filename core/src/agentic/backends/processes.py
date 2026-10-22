"""Stopping an agent CLI and every process it started (M2-57; the approach of the
claude-code backend, review I4).

An agent CLI starts shell commands, language servers and helpers of its own; killing
only the CLI would leave them running after a time limit or a stopped run. On Windows
`taskkill /T` ends the whole tree. On POSIX the CLI is started as a session leader
(`session_leader`), and `kill_tree` kills every descendant - found by parent id before
anything dies, because orphans are re-parented - and then the CLI's process group.
"""

import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_SESSION = "import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])"


def session_leader(argv: Sequence[str]) -> list[str]:
    """`argv`, started as the leader of a new session on POSIX (so its process group is
    its own and can be killed whole); unchanged on Windows."""
    if os.name == "nt":
        return list(argv)
    return [sys.executable, "-c", _SESSION, *argv]


def kill_tree(pid: int) -> None:
    """Kill `pid` and every process below it. A process already gone is no error."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    for target in [*descendants(pid), pid]:
        try:
            os.kill(target, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:  # a session leader's group is its own
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def descendants(pid: int) -> list[int]:
    """Every process below `pid` (POSIX): from /proc on Linux, else from `ps`."""
    children: dict[int, list[int]] = {}
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat_line = (entry / "stat").read_text()
            except OSError:
                continue
            fields = stat_line.rsplit(")", 1)[-1].split()
            if len(fields) > 1:
                children.setdefault(int(fields[1]), []).append(int(entry.name))
    else:
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, check=False
        ).stdout
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: list[int] = []
    queue = [pid]
    while queue:
        for child in children.get(queue.pop(), []):
            if child not in found:
                found.append(child)
                queue.append(child)
    return found
