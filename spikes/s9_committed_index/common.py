"""Shared helpers for the S9 spike (needs psutil; everything else is stdlib)."""

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "out"
WORK = OUT / "work"
S5_REPOS = HERE.parent / "s5_code_graph" / "out" / "repos"
CRG_EXE = OUT / "venv" / "Scripts" / "code-review-graph.exe"
PY = OUT / "venv" / "Scripts" / "python.exe"
CRG_VERSION = "2.3.9"
DB_REL = Path(".code-review-graph") / "graph.db"


def git(repo: Path, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
        env=e,
    )


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_size(path: Path, skip: tuple[str, ...] = ()) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def rmtree(p: Path) -> None:
    def onerr(func, path, _exc):
        try:
            os.chmod(path, 0o700)
            func(path)
        except OSError:
            pass

    if p.exists():
        shutil.rmtree(p, onerror=onerr)


def work_clone(name: str, tag: str = "") -> Path:
    """Fresh local clone of the S5 pristine repo (full history) under out/work."""
    dest = WORK / f"{name}{tag}"
    rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(S5_REPOS / name), str(dest)], check=True)
    return dest


def pick_commits(repo: Path, n: int = 20, months: float = 6.0) -> list[dict]:
    """n commits spaced evenly in time over the last `months` of first-parent history."""
    head_date = datetime.fromisoformat(git(repo, "log", "-1", "--format=%cI", "HEAD").stdout.strip())
    start = head_date - timedelta(days=30.44 * months)
    # Every first-parent commit in the window, oldest first.  Time-spacing 20 points on a repo that
    # lands ~20 commits in 6 months (flask) just yields duplicates, so space by commit index instead:
    # each stand-in "night" then differs from the previous one by at least one commit.
    shas = git(repo, "rev-list", "--first-parent", f"--since={start.isoformat()}", "HEAD").stdout.split()[::-1]
    if len(shas) <= n:
        pick = shas
    else:
        pick = [shas[round(i * (len(shas) - 1) / (n - 1))] for i in range(n)]
    out = []
    for i, sha in enumerate(dict.fromkeys(pick)):
        out.append({"i": i, "sha": sha, "date": git(repo, "log", "-1", "--format=%cI", sha).stdout.strip(),
                    "window_commits": len(shas)})
    return out


class PeakMem:
    """Poll the RSS of a process tree; usable as a context manager around a Popen."""

    def __init__(self, proc: subprocess.Popen, interval: float = 0.05):
        import psutil

        self.psutil = psutil
        self.proc = proc
        self.peak = 0
        self.interval = interval
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        ps = self.psutil
        try:
            root = ps.Process(self.proc.pid)
        except ps.Error:
            return
        while not self._stop.is_set():
            total = 0
            try:
                procs = [root, *root.children(recursive=True)]
            except ps.Error:
                procs = []
            for p in procs:
                try:
                    mi = p.memory_info()
                    total += getattr(mi, "private", None) or mi.rss  # private bytes ~ commit; rss fallback
                except ps.Error:
                    pass
            self.peak = max(self.peak, total)
            time.sleep(self.interval)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join()


def run_crg(args: list[str], repo: Path, env: dict | None = None) -> dict:
    quiet = args[0] in ("build", "update")
    """Run the code-review-graph CLI; return wall time, rc, peak private memory (MB)."""
    e = dict(os.environ)
    if env:
        e.update(env)
    t0 = time.perf_counter()
    p = subprocess.Popen(
        [str(CRG_EXE), *args, "--repo", str(repo), *(["-q"] if quiet else [])],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=e,
    )
    with PeakMem(p) as pm:
        so, se = p.communicate()
    return {
        "s": round(time.perf_counter() - t0, 2),
        "rc": p.returncode,
        "peak_mb": round(pm.peak / 1e6, 1),
        "stdout_tail": so[-300:],
        "stderr_tail": se[-300:],
    }


def build(repo: Path) -> dict:
    rmtree(repo / ".code-review-graph")
    return run_crg(["build"], repo)


def checkout(repo: Path, sha: str) -> None:
    git(repo, "checkout", "-q", "-f", sha)
    git(repo, "clean", "-q", "-fdx", "-e", ".code-review-graph")


def save_json(name: str, obj) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, indent=1), encoding="utf-8")
