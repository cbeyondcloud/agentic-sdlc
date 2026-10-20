"""Shared helpers for the S5 spike scripts (stdlib only)."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "out"
REPOS = OUT / "repos"
WORK = OUT / "work"
RAW = OUT / "raw"
REPO_NAMES = ["flask", "ky", "gson"]


def load_corpus(name: str) -> dict:
    return json.loads((OUT / f"corpus_{name}.json").read_text(encoding="utf-8"))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    ).stdout


def make_work_copy(tool: str, name: str, head: str) -> Path:
    """Fresh local clone of the pristine repo, checked out at the pinned commit (no tool artefacts)."""
    dest = WORK / tool / name
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(REPOS / name), str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "-q", head], check=True)
    return dest


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def rel(path: str, root: Path) -> str:
    """Normalise a tool-reported path to a repo-relative posix path."""
    p = path.replace("\\", "/")
    r = str(root).replace("\\", "/").rstrip("/") + "/"
    if p.lower().startswith(r.lower()):
        p = p[len(r) :]
    return p.lstrip("./") if p.startswith("./") else p


def dedupe(files: list[str], seed: str) -> list[str]:
    seen, out = {seed}, []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def save_raw(tool: str, name: str, doc: dict) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / f"{tool}_{name}.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")


class Timer:
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *a):
        self.s = time.perf_counter() - self.t0
