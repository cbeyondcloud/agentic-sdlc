"""Resource limits for one agent command (M3-10): CPU time, memory, file size and process
count from policy (`sandbox.limits`).

  * Linux and macOS: hard rlimits through a launcher around the command (CPU time, file
    size, a process-count backstop), and a watchdog over the command's process tree
    (resident memory, process count) that kills it past a limit;
  * native Windows: Job Object limits (job CPU time, job memory, active processes); no
    file-size limit.

Live tests stay tiny: a memory hog stopped at 150 MB, a fork tree of at most 127 sleepers
stopped at 10, a CPU burner stopped after 1 s, a file stopped at 1 MB."""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path

import pytest

from agentic.policy.model import ResourceLimits
from agentic.sandbox import limits as limits_module
from agentic.sandbox.limits import LAUNCHER, Watchdog, launcher
from agentic.sandbox.process import run_process

LINUX = sys.platform.startswith("linux")
POSIX = sys.platform != "win32"
MB = 1024 * 1024


def run(argv, tmp_path: Path, limits: ResourceLimits, timeout: float = 60):
    return asyncio.run(run_process(argv, tmp_path, timeout, limits=limits))


# ── the launcher (every OS) ───────────────────────────────────────────────────


def test_no_limits_leave_the_command_alone():
    assert launcher(ResourceLimits(), ["bash", "-c", "true"], posix=True) == ["bash", "-c", "true"]


def test_windows_gets_no_launcher():
    limits = ResourceLimits(cpu_seconds=5, max_file_mb=1, processes=10)
    assert launcher(limits, ["cmd", "/c", "ver"], posix=False) == ["cmd", "/c", "ver"]


def test_memory_alone_needs_no_launcher():
    """Memory is the watchdog's: an address-space rlimit breaks Node and the JVM."""
    assert launcher(ResourceLimits(memory_mb=512), ["x"], posix=True) == ["x"]


def test_launcher_argv_on_posix(monkeypatch):
    monkeypatch.setattr(limits_module, "user_processes", lambda: 40)
    limits = ResourceLimits(cpu_seconds=60, max_file_mb=2, processes=10)
    argv = launcher(limits, ["bash", "-c", "make test"], posix=True)
    interpreter, *rest = argv
    assert Path(interpreter).name.startswith("python")
    assert rest == ["-I", "-S", "-c", LAUNCHER, "60", str(2 * MB), "50", "bash", "-c", "make test"]


def test_the_launcher_runs_isolated_from_the_tree():
    """`-I`: a `resource.py` an agent planted in the tree is never imported."""
    argv = launcher(ResourceLimits(cpu_seconds=1), ["x"], posix=True)
    assert "-I" in argv[1:3]


def test_root_gets_no_process_count_backstop(monkeypatch):
    monkeypatch.setattr(limits_module, "user_processes", lambda: None)
    argv = launcher(ResourceLimits(processes=10), ["x"], posix=True)
    assert argv[5:8] == ["-", "-", "-"]


# ── the watchdog (every OS: fake process table) ───────────────────────────────


def test_watchdog_counts_the_tree_below_the_command():
    dog = Watchdog(ResourceLimits(processes=3), 100)
    dog.descendants = lambda pid: [101, 102, 103]
    dog.resident = lambda pids: 0
    assert dog.check() is None
    dog.descendants = lambda pid: [101, 102, 103, 104]
    assert dog.check() == "processes (3)"


def test_watchdog_sums_resident_memory():
    dog = Watchdog(ResourceLimits(memory_mb=100), 100)
    dog.descendants = lambda pid: [101, 102]
    dog.resident = lambda pids: 60 * MB * len(pids)
    assert dog.check() == "memory (100 MB)"


def test_watchdog_counts_the_command_itself_when_it_was_started_directly():
    dog = Watchdog(ResourceLimits(memory_mb=100, processes=2), 100, include_root=True)
    dog.descendants = lambda pid: [101]
    dog.resident = lambda pids: 80 * MB * len(pids)
    assert dog.check() == "memory (100 MB)"  # 100 and 101 together
    dog.descendants = lambda pid: [101, 102]
    assert dog.check() == "processes (2)"


def test_efbig_from_a_program_that_ignores_sigxfsz_is_the_file_limit():
    from agentic.sandbox.limits import kernel_limit

    limits = ResourceLimits(max_file_mb=1)
    stderr = "OSError: [Errno 27] File too large\n"
    assert kernel_limit(limits, 1, stderr) == ("file size (1 MB)" if POSIX else None)
    assert kernel_limit(ResourceLimits(), 1, stderr) is None
    assert kernel_limit(limits, 0, stderr) is None


def test_watchdog_without_limits_watches_nothing():
    assert not Watchdog(ResourceLimits(cpu_seconds=5), 100).active
    assert Watchdog(ResourceLimits(memory_mb=5), 100).active == POSIX


# ── POSIX: real processes ─────────────────────────────────────────────────────


@pytest.mark.skipif(not POSIX, reason="rlimits are POSIX")
def test_command_cannot_raise_its_hard_limit(tmp_path: Path):
    script = "ulimit -t unlimited 2>/dev/null && echo raised || echo refused; ulimit -H -t"
    result = run(["sh", "-c", script], tmp_path, ResourceLimits(cpu_seconds=30))
    lines = result.stdout.split()
    assert lines[0] == "refused", result
    assert int(lines[1]) <= 31


@pytest.mark.skipif(not POSIX, reason="rlimits are POSIX")
def test_a_command_under_its_limits_runs_normally(tmp_path: Path):
    limits = ResourceLimits(cpu_seconds=30, memory_mb=512, max_file_mb=10, processes=50)
    result = run(["sh", "-c", "echo hello > out.txt && cat out.txt"], tmp_path, limits)
    assert result.exit_code == 0 and result.stdout.strip() == "hello"
    assert result.limited is None


@pytest.mark.skipif(not LINUX, reason="the live limits run in the Linux stage")
def test_linux_memory_hog_is_stopped(tmp_path: Path):
    hog = (
        "import time\nchunks = []\nfor _ in range(64):\n"
        "    chunks.append(b'x' * (16 << 20))\n    time.sleep(0.02)\nprint('survived')"
    )
    started = time.monotonic()
    result = run([sys.executable, "-c", hog], tmp_path, ResourceLimits(memory_mb=150))
    assert result.limited == "memory (150 MB)", result
    assert "survived" not in result.stdout
    assert not result.timed_out
    assert time.monotonic() - started < 20


@pytest.mark.skipif(not LINUX or shutil.which("bash") is None, reason="Linux stage, bash")
def test_linux_process_spawner_is_stopped(tmp_path: Path):
    """A fork tree kept tiny: 2^7 - 1 sleepers at most, stopped at 10."""
    bomb = "f() { if [ $1 -gt 0 ]; then f $(($1-1)) & f $(($1-1)) & fi; sleep 20; }; f 6; wait"
    started = time.monotonic()
    result = run(["bash", "-c", bomb], tmp_path, ResourceLimits(processes=10))
    assert result.limited == "processes (10)" or "fork" in result.stderr.lower(), result
    assert time.monotonic() - started < 15
    assert not result.timed_out


@pytest.mark.skipif(not LINUX, reason="the live limits run in the Linux stage")
def test_linux_cpu_burner_is_stopped(tmp_path: Path):
    started = time.monotonic()
    result = run(
        [sys.executable, "-c", "while True: pass"], tmp_path, ResourceLimits(cpu_seconds=1)
    )
    assert result.limited == "cpu time (1 s)", result
    assert time.monotonic() - started < 15


@pytest.mark.skipif(not LINUX, reason="the live limits run in the Linux stage")
def test_linux_big_file_is_refused(tmp_path: Path):
    writer = "f = open('big.bin', 'wb')\nfor _ in range(5): f.write(b'x' * (1 << 20)); f.flush()"
    result = run([sys.executable, "-c", writer], tmp_path, ResourceLimits(max_file_mb=1))
    assert result.limited == "file size (1 MB)", result
    assert (tmp_path / "big.bin").stat().st_size <= MB


@pytest.mark.skipif(not LINUX or shutil.which("srt") is None, reason="srt in the Linux stage")
def test_linux_limits_hold_inside_srt(tmp_path: Path):
    from agentic.sandbox import SandboxRules, SrtSandbox

    tree = tmp_path / "tree"
    tree.mkdir()
    box = SrtSandbox(
        SandboxRules(write=(tree,)),
        tmp_path / "settings",
        limits=ResourceLimits(memory_mb=150, processes=20, cpu_seconds=2),
    )
    hog = f"{sys.executable} -c \"b = [b'x' * (16 << 20) for _ in range(40)]; print('survived')\""
    result = asyncio.run(box.run(hog, tree, 60))
    assert "survived" not in result.stdout
    assert result.limited == "memory (150 MB)", result
    spawner = "for i in $(seq 1 40); do sleep 20 & done; wait"
    result = asyncio.run(box.run(spawner, tree, 60))
    assert result.limited == "processes (20)" or "fork" in result.stderr.lower(), result
    ulimit = asyncio.run(box.run("ulimit -H -t", tree, 60))
    assert int(ulimit.stdout.strip()) <= 3, ulimit


# ── Windows: Job Object limits ────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows")
def test_windows_job_memory_hog_fails(tmp_path: Path):
    hog = "b = b'x' * (400 << 20)\nprint('allocated')"
    result = run([sys.executable, "-c", hog], tmp_path, ResourceLimits(memory_mb=100))
    assert result.exit_code != 0, result
    assert "allocated" not in result.stdout
    assert "MemoryError" in result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows")
def test_windows_job_process_limit(tmp_path: Path):
    spawner = (
        "import subprocess, sys\nkids = []\nsleep = 'import time; time.sleep(5)'\n"
        "for _ in range(5):\n"
        "    try:\n"
        "        kids.append(subprocess.Popen([sys.executable, '-c', sleep]))\n"
        "    except OSError:\n"
        "        print('refused'); break\n"
        "for kid in kids: kid.kill()\n"
        "print('started', len(kids))"
    )
    result = run([sys.executable, "-c", spawner], tmp_path, ResourceLimits(processes=3))
    assert "refused" in result.stdout, result
    # the parent and 2 children; a console host Windows may add counts too
    assert "started 2" in result.stdout or "started 1" in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows")
def test_windows_job_cpu_time_limit(tmp_path: Path):
    started = time.monotonic()
    result = run(
        [sys.executable, "-c", "while True: pass"], tmp_path, ResourceLimits(cpu_seconds=1)
    )
    assert not result.timed_out, result
    assert result.exit_code != 0
    assert time.monotonic() - started < 20
