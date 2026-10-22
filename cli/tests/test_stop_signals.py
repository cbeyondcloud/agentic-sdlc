"""No orphan survives a Ctrl-C or a kill of the CLI (M2-22). A small `agentic`-like
program runs a step through the real `main` and `drive`: inside a process scope it starts
a contained process with a child of its own, then waits. The test stops the program the
way a person or the OS would and proves that neither process survives."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROGRAM = '''\
import asyncio, subprocess, sys
from pathlib import Path

import typer

from agentic.backends.processes import ProcessTree, process_scope, spawn_group
from agentic_cli.driving import drive
from agentic_cli.main import main

PARENT = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(child.pid, flush=True)
time.sleep(120)
"""
app = typer.Typer()


@app.callback()
def options() -> None:
    pass


@app.command()
def run(run_dir: Path) -> None:
    async def step():
        with process_scope():  # as the engine runs every agent step
            parent = subprocess.Popen(
                [sys.executable, "-c", PARENT], stdout=subprocess.PIPE, text=True,
                **spawn_group(),
            )
            ProcessTree().adopt(parent)
            print(parent.pid, parent.stdout.readline().strip(), flush=True)
            await asyncio.sleep(120)

    drive(lambda text: text, run_dir, step)


sys.exit(main(["run", sys.argv[1]], app=app))
'''


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


def all_gone(pids: list[int], within_s: float = 15.0) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if not any(alive(pid) for pid in pids):
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def cli(request, tmp_path):
    """Start the program; yields (process, [pid of its agent process, of that one's child]).
    Parametrized indirectly with a signal name, the program starts with it ignored, as
    `nohup` starts its command."""
    ignored = getattr(request, "param", None)
    script = tmp_path / "agentic_like.py"
    script.write_text(PROGRAM, encoding="utf-8")
    group = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}  # its own foreground group, as in a terminal
    )
    if ignored:
        group["preexec_fn"] = lambda: signal.signal(getattr(signal, ignored), signal.SIG_IGN)
    process = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path / "run")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **group,
    )
    pids = [int(pid) for pid in process.stdout.readline().split()]
    assert len(pids) == 2 and all(alive(pid) for pid in pids)
    yield process, pids
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=30)
    for pid in pids:  # a failed test must not leave them behind
        if alive(pid):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)]
                if os.name == "nt"
                else ["kill", "-9", str(pid)],
                capture_output=True,
                check=False,
            )


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_ctrl_c_of_the_cli_leaves_no_orphan(cli):
    process, pids = cli
    os.killpg(process.pid, signal.SIGINT)  # what the terminal sends its foreground group
    assert process.wait(timeout=30) == 130
    assert all_gone(pids)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
@pytest.mark.parametrize("name", ["SIGTERM", "SIGHUP"])
def test_a_terminated_cli_leaves_no_orphan(cli, name):
    process, pids = cli
    os.kill(process.pid, getattr(signal, name))  # `kill <pid>`, a closed terminal
    assert process.wait(timeout=30) == 130
    assert all_gone(pids)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
@pytest.mark.parametrize("cli", ["SIGHUP"], indirect=True)
def test_a_cli_started_under_nohup_survives_a_hang_up(cli):  # F2
    process, pids = cli
    os.kill(process.pid, signal.SIGHUP)  # the SSH session closed
    time.sleep(1.0)
    assert process.poll() is None and all(alive(pid) for pid in pids)
    os.kill(process.pid, signal.SIGTERM)  # still stoppable the usual way
    assert process.wait(timeout=30) == 130
    assert all_gone(pids)


def test_a_signal_the_parent_ignored_stays_ignored():  # F2: `nohup`, a wrapper's SIG_IGN
    from agentic_cli.main import stop_signals_as_interrupts

    number = signal.SIGTERM
    before = signal.signal(number, signal.SIG_IGN)
    try:
        with stop_signals_as_interrupts():
            assert signal.getsignal(number) == signal.SIG_IGN
        assert signal.getsignal(number) == signal.SIG_IGN
    finally:
        signal.signal(number, before)


@pytest.mark.skipif(os.name != "nt", reason="Windows console events")
def test_ctrl_break_of_the_cli_leaves_no_orphan(cli):
    process, pids = cli
    os.kill(process.pid, signal.CTRL_BREAK_EVENT)
    assert process.wait(timeout=30) == 130
    assert all_gone(pids)


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_a_killed_cli_leaves_no_orphan_on_windows(cli):  # Task Manager, a closed console
    process, pids = cli
    process.kill()  # TerminateProcess: no Python code runs; the job dies with its handle
    process.wait(timeout=30)
    assert all_gone(pids)
