"""Stopping an agent CLI and everything it started (M2-57, M2-22, DF-451): the one
cross-OS test set for `agentic.backends.processes`, which every backend uses. Each case
spawns a grandchild that would outlive its parent and proves it does not survive."""

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic.backends.processes import (
    ProcessTree,
    current_scope,
    kill_tree,
    process_scope,
    run_contained,
    session_leader,
    spawn_group,
)

CORE_SRC = Path(__file__).resolve().parents[2] / "src" / "agentic"

PARENT = """\
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
time.sleep(60)
"""
# the parent leaves at once: its child is an orphan (POSIX re-parents it; Windows keeps
# no link at all, so `taskkill /T` cannot find it)
ORPHANER = """\
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
"""
# the child gets a process group (POSIX: a session) of its own, as agent CLIs do for
# each shell command
DETACHER = """\
import os, subprocess, sys, time
group = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    if os.name == "nt"
    else {"start_new_session": True}
)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **group)
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


def spawn(tmp_path: Path, source: str, name: str = "parent.py") -> subprocess.Popen:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, text=True, **spawn_group()
    )


def grandchild_of(parent: subprocess.Popen) -> int:
    pid = int(parent.stdout.readline())
    assert alive(pid)
    return pid


def cleanup(parent: subprocess.Popen, *pids: int) -> None:
    parent.kill()
    parent.wait(timeout=10)
    parent.stdout.close()
    for pid in pids:  # a failed test must not leave its grandchild behind
        if alive(pid):
            kill_tree(pid)


# ── the tree ──────────────────────────────────────────────────────────────────


def test_kill_stops_the_process_and_what_it_started(tmp_path):
    parent = spawn(tmp_path, PARENT)
    tree = ProcessTree().adopt(parent)
    grandchild = grandchild_of(parent)
    try:
        tree.kill()
        parent.wait(timeout=10)
        assert gone(grandchild)
    finally:
        tree.close()
        cleanup(parent, grandchild)


def test_an_orphaned_grandchild_dies_with_the_tree(tmp_path):  # Review Focus 1
    parent = spawn(tmp_path, ORPHANER)
    tree = ProcessTree().adopt(parent)
    grandchild = grandchild_of(parent)
    try:
        parent.wait(timeout=10)  # the parent is gone; its child lives on
        assert alive(grandchild)
        tree.close()
        assert gone(grandchild)
    finally:
        cleanup(parent, grandchild)


def test_a_grandchild_in_a_group_of_its_own_dies_with_the_tree(tmp_path):
    parent = spawn(tmp_path, DETACHER)
    tree = ProcessTree().adopt(parent)
    grandchild = grandchild_of(parent)
    try:
        tree.kill()
        assert gone(grandchild)
    finally:
        tree.close()
        cleanup(parent, grandchild)


def test_a_finished_root_is_not_signalled_by_pid():  # Review Focus 4
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:

        class Exited:  # a root that exited; its pid now belongs to someone else
            pid = bystander.pid
            returncode = 0

        tree = ProcessTree().adopt(Exited())
        tree.kill()
        tree.close()
        time.sleep(0.5)
        assert alive(bystander.pid)
    finally:
        bystander.kill()
        bystander.wait(timeout=10)


def test_kill_of_an_unknown_tree_is_harmless():
    ProcessTree().kill()  # nothing adopted
    ProcessTree().close()
    kill_tree(999_999_999 if os.name != "nt" else 4_294_967_000)  # no such process


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_the_child_is_contained_in_a_job_on_windows(tmp_path):
    import ctypes

    parent = spawn(tmp_path, PARENT)
    tree = ProcessTree().adopt(parent)
    grandchild = grandchild_of(parent)
    try:
        assert tree.contained
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, grandchild)
        inside = ctypes.c_int()
        kernel.IsProcessInJob(ctypes.c_void_p(handle), None, ctypes.byref(inside))
        kernel.CloseHandle(handle)
        assert inside.value  # started after adoption: the job holds it
    finally:
        tree.close()
        cleanup(parent, grandchild)


def test_the_session_leader_launcher_is_posix_only():
    argv = ["agent", "acp"]
    if os.name == "nt":
        assert session_leader(argv) == argv
    else:
        launched = session_leader(argv)
        assert launched[0] == sys.executable and launched[-2:] == argv


def test_kill_tree_by_pid_stops_a_session_leader_and_its_child(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(PARENT, encoding="utf-8")
    parent = subprocess.Popen(
        session_leader([sys.executable, str(script)]), stdout=subprocess.PIPE, text=True
    )
    grandchild = grandchild_of(parent)
    try:
        kill_tree(parent.pid)
        parent.wait(timeout=10)
        assert gone(grandchild)
    finally:
        cleanup(parent, grandchild)


# ── the scope ─────────────────────────────────────────────────────────────────


def test_closing_the_scope_closes_every_tree_in_it(tmp_path):
    with process_scope() as scope:
        assert current_scope() is scope
        first = spawn(tmp_path, PARENT, "first.py")
        ProcessTree().adopt(first)
        second = spawn(tmp_path, ORPHANER, "second.py")
        ProcessTree().adopt(second)
        pids = [grandchild_of(first), grandchild_of(second)]
    assert current_scope() is None
    try:
        assert all(gone(pid) for pid in pids)
        assert first.wait(timeout=10) is not None
    finally:
        cleanup(first, *pids)
        cleanup(second)


def test_a_closed_tree_leaves_the_scope(tmp_path):
    with process_scope() as scope:
        parent = spawn(tmp_path, PARENT)
        tree = ProcessTree().adopt(parent)
        grandchild = grandchild_of(parent)
        tree.close()
        assert tree not in scope.trees
    cleanup(parent, grandchild)


SLOW = """\
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(60)
"""


def test_closing_the_scope_kills_a_build_in_a_thread(tmp_path):  # Review Focus 3
    script, marker = tmp_path / "slow.py", tmp_path / "grandchild.pid"
    script.write_text(SLOW, encoding="utf-8")
    finished = threading.Event()

    def build() -> None:  # a synchronous tool, as pydantic-ai runs it: in a worker thread
        try:
            run_contained([sys.executable, str(script), str(marker)], timeout=120)
        finally:
            finished.set()

    async def step() -> None:
        with process_scope():
            work = asyncio.ensure_future(asyncio.to_thread(build))
            while not marker.exists() or not marker.read_text():
                await asyncio.sleep(0.05)
            try:  # the step's time limit
                await asyncio.wait_for(asyncio.shield(work), 0.2)
            except TimeoutError:
                pass

    started = time.monotonic()
    asyncio.run(step())
    grandchild = int(marker.read_text())
    assert gone(grandchild)
    assert finished.wait(10)  # the thread was not left waiting for 120 s
    assert time.monotonic() - started < 30


# ── running a helper ──────────────────────────────────────────────────────────


def test_run_contained_returns_the_output():
    done = run_contained(
        [sys.executable, "-c", "import sys; print(sys.stdin.read() * 2)"], input="ab"
    )
    assert (done.returncode, done.stdout.strip()) == (0, "abab")


def test_run_contained_kills_the_tree_at_its_timeout(tmp_path):
    script, marker = tmp_path / "slow.py", tmp_path / "grandchild.pid"
    script.write_text(SLOW, encoding="utf-8")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_contained([sys.executable, str(script), str(marker)], timeout=3)
    assert time.monotonic() - started < 20
    assert gone(int(marker.read_text()))


# ── one helper for every backend (DF-451) ─────────────────────────────────────


def test_only_the_helper_kills_process_trees():
    offenders = []
    for path in CORE_SRC.rglob("*.py"):
        if path.name == "processes.py" and path.parent.name == "backends":
            continue
        text = path.read_text(encoding="utf-8")
        for word in ("taskkill", "killpg", "_descendants(", "os.setsid"):
            if word in text:
                offenders.append(f"{path.relative_to(CORE_SRC).as_posix()}: {word}")
    assert offenders == []
