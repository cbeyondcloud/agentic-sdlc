"""Local replacement for CI (GitHub Actions is not used, to save free-tier minutes).

Runs the same checks CI used to run: lock file, lint, format, tests, plus the
architecture rules, licence allow-list, known-vulnerability audit and (Linux only)
secret scan.

    uv run python scripts/check.py           # on this machine (Windows/macOS/Linux)
    uv run python scripts/check.py --linux   # inside a clean Linux container (Docker or Podman)
    uv run python scripts/check.py --all     # both
    uv run python scripts/check.py --offline # no network: skips pip-audit, says INCOMPLETE

Run `--all` before pushing; it is not wired into a git hook. Exit codes: 0 every check
passed, 1 a check failed, 3 INCOMPLETE (an offline run: the network checks were skipped,
so `check.py --offline && git push` does not push, DF-332).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PASSED, INCOMPLETE, FAILED = "passed", "incomplete", "failed"
EXIT_INCOMPLETE = 3

# The checks, in order. Each is a command run from the repo root.
CHECKS: list[tuple[str, list[str]]] = [
    ("lock file up to date", ["uv", "lock", "--check"]),
    ("lint", ["uv", "run", "ruff", "check", "."]),
    ("format", ["uv", "run", "ruff", "format", "--check", "."]),
    ("tests", ["uv", "run", "pytest"]),
    # Supply chain and architecture (P0-03)
    ("architecture rules", ["uv", "run", "lint-imports"]),
    ("licence allow-list", ["uv", "run", "python", "scripts/licence_check.py"]),
    (
        "known vulnerabilities",
        ["uv", "run", "pip-audit", "--skip-editable", "--progress-spinner", "off"],
    ),
]

# Checks that need the network (PyPI/OSV): `--offline` skips them and says so (DF-97).
NETWORK_CHECKS = ("known vulnerabilities",)

# Checks that run only in the Linux container, where their pinned native binary is
# installed (no unsigned binaries on developer laptops).
LINUX_ONLY_CHECKS: list[tuple[str, list[str]]] = [
    ("secret scan", ["gitleaks", "dir", ".", "--no-banner", "--redact"]),
]


def linux_steps() -> list[tuple[str, list[str]]]:
    """The Linux stage's steps. The secret scan goes first, on the tree exactly as
    streamed in: later steps write compiled files (pytest's __pycache__) whose folded
    constants can look like keys."""
    return LINUX_ONLY_CHECKS + CHECKS


# The Linux image: the pinned uv/Python image plus git and the sandbox prerequisites
# (`srt`, bubblewrap), built from this Dockerfile and tagged by its content hash.
LINUX_DOCKERFILE = REPO_ROOT / "scripts" / "linux-check.Dockerfile"
# bubblewrap creates namespaces and mounts /proc, which Docker's defaults forbid (spike S6)
SANDBOX_OPTS = ["--security-opt", "seccomp=unconfined", "--security-opt", "systempaths=unconfined"]


ENGINES = ("docker", "podman")


def container_engine(env=os.environ, which=shutil.which) -> str | None:
    """The container engine CLI: CONTAINER_ENGINE if set, else docker or podman on PATH.

    Engine-neutral so the org can standardise on any free runtime (spike S12); a named
    engine that is unknown or missing stops the run rather than silently falling back.
    """
    named = env.get("CONTAINER_ENGINE", "").strip()
    if named:
        if named not in ENGINES:
            raise SystemExit(f"CONTAINER_ENGINE={named}: use docker or podman")
        if which(named) is None:
            raise SystemExit(f"CONTAINER_ENGINE={named} but {named} is not on PATH")
        return named
    return next((name for name in ENGINES if which(name)), None)


def linux_image(engine: str) -> str | None:
    """The check image's tag, built once per Dockerfile version; None if the build fails."""
    digest = hashlib.sha256(LINUX_DOCKERFILE.read_bytes()).hexdigest()[:12]
    tag = f"agentic-check-linux:{digest}"
    found = subprocess.run([engine, "image", "inspect", tag], capture_output=True)
    if found.returncode == 0:
        return tag
    print(f"[linux] building {tag}", flush=True)
    build = [
        engine,
        "build",
        "-q",
        "-t",
        tag,
        "-f",
        str(LINUX_DOCKERFILE),
        str(LINUX_DOCKERFILE.parent),
    ]
    return tag if subprocess.run(build).returncode == 0 else None


def checks_for(offline: bool) -> list[tuple[str, list[str]]]:
    """The native checks: all of them, or offline only those that need no network."""
    return [c for c in CHECKS if c[0] not in NETWORK_CHECKS] if offline else CHECKS


def failure_hint(name: str) -> str:
    """What to try when a check fails for want of a network (DF-97)."""
    if name in NETWORK_CHECKS:
        return (
            "this check needs PyPI/OSV; with no network, `--offline` runs the others "
            "(an INCOMPLETE check - run again online before pushing)"
        )
    return ""


def _span_days(span: str) -> int | None:
    """Days in a uv relative span ('14 days', '2 weeks', 'P14D'); None if not relative."""
    text = span.strip().lower()
    if found := re.fullmatch(r"p(?:(\d+)w)?(?:(\d+)d)?", text):
        return int(found[1] or 0) * 7 + int(found[2] or 0)
    if found := re.fullmatch(r"(\d+)\s*(day|days|week|weeks)", text):
        return int(found[1]) * (7 if found[2].startswith("week") else 1)
    return None


def stale_exceptions(settings: dict, today: date) -> list[str]:
    """Each `exclude-newer-package` exception in `settings` (pyproject.toml, parsed) whose
    date the release-age window has passed: the exception no longer lets a fix in early,
    it only holds the package back (DF-330). One warning line per package."""
    uv = settings.get("tool", {}).get("uv", {})
    days = _span_days(str(uv.get("exclude-newer", "")))
    exceptions = uv.get("exclude-newer-package") or {}
    if days is None or not isinstance(exceptions, dict):
        return []
    found = []
    for name, when in sorted(exceptions.items()):
        try:
            cutoff = datetime.fromisoformat(str(when).replace("Z", "+00:00")).date()
        except ValueError:
            continue
        if cutoff + timedelta(days=days) <= today:
            found.append(
                f"{name}: its exclude-newer-package exception ({cutoff}) is older than the "
                f"{days}-day release-age window - remove it from pyproject.toml, or it holds "
                f"{name} back from later fixes"
            )
    return found


def _warn_stale_exceptions(label: str) -> None:
    try:
        settings = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return  # the lock check reports a broken pyproject.toml
    for line in stale_exceptions(settings, date.today()):
        print(f"[{label}] WARNING: {line}")


def run_native(offline: bool = False) -> str:
    """Run every check on this machine; stop at the first failure. Offline, the network
    checks are skipped, uv works from its cache, and the run says it is incomplete.
    Returns PASSED, INCOMPLETE or FAILED."""
    label = f"{platform.system()} (native)"
    _warn_stale_exceptions(label)
    env = dict(os.environ, UV_OFFLINE="1") if offline else None
    for name, cmd in checks_for(offline):
        print(f"[{label}] {name}: {' '.join(cmd)}", flush=True)
        if subprocess.run(cmd, cwd=REPO_ROOT, env=env).returncode != 0:
            print(f"[{label}] FAILED at: {name}")
            if hint := failure_hint(name):
                print(f"[{label}] {hint}")
            return FAILED
    if offline:
        skipped = ", ".join(NETWORK_CHECKS)
        print(f"[{label}] INCOMPLETE (offline): skipped {skipped} - run again online")
        return INCOMPLETE
    print(f"[{label}] all checks passed")
    return PASSED


def tracked_files_tar() -> bytes:
    """A tar archive of exactly the files git would track (tracked + new, minus ignored).

    So the container sees the same files as a fresh clone would, plus uncommitted
    work, but never git-ignored scratch such as .venv/ or .superpowers/.
    """
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout
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
    engine = container_engine()
    if engine is None:
        print("[linux] SKIPPED: no container engine (docker or podman) found")
        return True
    inner = " && ".join(" ".join(cmd) for _, cmd in linux_steps())
    image = linux_image(engine)
    if image is None:
        print("[linux] FAILED: the check image could not be built")
        return False
    # Zyroflo (sdlc-harness) is a private GitHub dependency. The throwaway container
    # gets the developer's existing `gh` token via an environment variable (never
    # written to the image or the repo) and git uses it for github.com only.
    github_auth = (
        'if [ -n "$GH_TOKEN" ]; then git config --global '
        'url."https://x-access-token:${GH_TOKEN}@github.com/".insteadOf "https://github.com/"; fi'
    )
    script = (
        f"{github_auth} && mkdir -p /work && cd /work && tar -xf - "
        f"&& uv sync --locked -q && {inner}"
    )
    token = _github_token()
    if not token:
        print(
            "[linux] FAILED: no GitHub token - the container must fetch the private Zyroflo "
            "dependency. Set GH_TOKEN, or log in with `gh auth login`."
        )
        return False
    env = dict(os.environ, GH_TOKEN=token)
    cmd = [
        engine,
        "run",
        "--rm",
        "-i",
        "-e",
        "GH_TOKEN",
        *SANDBOX_OPTS,
        image,
        "sh",
        "-c",
        script,
    ]
    print(f"[linux] running checks in {image}", flush=True)
    ok = subprocess.run(cmd, input=tracked_files_tar(), env=env).returncode == 0
    print("[linux] all checks passed" if ok else "[linux] FAILED")
    return ok


def _github_token() -> str:
    """The developer's GitHub token from GH_TOKEN, or from the gh CLI login."""
    if os.environ.get("GH_TOKEN"):
        return os.environ["GH_TOKEN"]
    if shutil.which("gh") is None:
        return ""
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--linux", action="store_true", help="run only in a Linux container")
    group.add_argument("--all", action="store_true", help="run natively and in Linux")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="no network: skip the checks that need it (an incomplete check)",
    )
    args = parser.parse_args()
    if args.offline and (args.linux or args.all):
        parser.error("--offline runs on this machine only (the Linux stage needs the network)")

    results = []
    if not args.linux:
        results.append(run_native(offline=args.offline))
    if args.linux or args.all:
        results.append(PASSED if run_linux() else FAILED)
    if FAILED in results:
        return 1
    return EXIT_INCOMPLETE if INCOMPLETE in results else 0


if __name__ == "__main__":
    sys.exit(main())
