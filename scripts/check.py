"""Local replacement for CI (GitHub Actions is not used, to save free-tier minutes).

Runs the same checks CI used to run: lock file, lint, format, tests.

    uv run python scripts/check.py           # on this machine (Windows/macOS/Linux)
    uv run python scripts/check.py --linux   # inside a clean Linux container (needs Docker)
    uv run python scripts/check.py --all     # both

Run `--all` before pushing; it is not wired into a git hook.
"""

from __future__ import annotations

import argparse
import io
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The checks, in order. Each is a command run from the repo root.
CHECKS: list[tuple[str, list[str]]] = [
    ("lock file up to date", ["uv", "lock", "--check"]),
    ("lint", ["uv", "run", "ruff", "check", "."]),
    ("format", ["uv", "run", "ruff", "format", "--check", "."]),
    ("tests", ["uv", "run", "pytest"]),
]

# Pinned image: same uv and Python versions as local development.
LINUX_IMAGE = "ghcr.io/astral-sh/uv:0.12.1-python3.12-trixie-slim"


def run_native() -> bool:
    """Run every check on this machine; stop at the first failure."""
    label = f"{platform.system()} (native)"
    for name, cmd in CHECKS:
        print(f"[{label}] {name}: {' '.join(cmd)}", flush=True)
        if subprocess.run(cmd, cwd=REPO_ROOT).returncode != 0:
            print(f"[{label}] FAILED at: {name}")
            return False
    print(f"[{label}] all checks passed")
    return True


def tracked_files_tar() -> bytes:
    """A tar archive of exactly the files git would track (tracked + new, minus ignored).

    So the container sees the same files as a fresh clone would, plus uncommitted
    work, but never git-ignored scratch such as .venv/ or .superpowers/.
    """
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO_ROOT, capture_output=True, check=True,
    ).stdout  # fmt: skip
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in filter(None, listing.decode().split("\0")):
            if (REPO_ROOT / name).is_file():  # skips files deleted but not yet committed
                archive.add(REPO_ROOT / name, arcname=name)
    return buffer.getvalue()


def run_linux() -> bool:
    """Run every check inside a clean Linux container.

    The files are streamed into the container (never mounted), so the Windows
    virtualenv and scratch folders on the host are never seen or touched.
    """
    if shutil.which("docker") is None:
        print("[linux] SKIPPED: docker not found")
        return True
    inner = " && ".join(" ".join(cmd) for _, cmd in CHECKS)
    script = f"mkdir -p /work && cd /work && tar -xf - && uv sync --locked -q && {inner}"
    cmd = ["docker", "run", "--rm", "-i", LINUX_IMAGE, "sh", "-c", script]
    print(f"[linux] running checks in {LINUX_IMAGE}", flush=True)
    ok = subprocess.run(cmd, input=tracked_files_tar()).returncode == 0
    print("[linux] all checks passed" if ok else "[linux] FAILED")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--linux", action="store_true", help="run only in a Linux container")
    group.add_argument("--all", action="store_true", help="run natively and in Linux")
    args = parser.parse_args()

    results = []
    if not args.linux:
        results.append(run_native())
    if args.linux or args.all:
        results.append(run_linux())
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
