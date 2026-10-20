"""Local replacement for CI (GitHub Actions is not used, to save free-tier minutes).

Runs the same checks CI used to run: lock file, lint, format, tests, plus the
architecture rules, licence allow-list, known-vulnerability audit and (Linux only)
secret scan.

    uv run python scripts/check.py           # on this machine (Windows/macOS/Linux)
    uv run python scripts/check.py --linux   # inside a clean Linux container (Docker or Podman)
    uv run python scripts/check.py --all     # both

Run `--all` before pushing; it is not wired into a git hook.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
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
    # Supply chain and architecture (P0-03)
    ("architecture rules", ["uv", "run", "lint-imports"]),
    ("licence allow-list", ["uv", "run", "python", "scripts/licence_check.py"]),
    (
        "known vulnerabilities",
        ["uv", "run", "pip-audit", "--skip-editable", "--progress-spinner", "off"],
    ),
]

# Checks that run only in the Linux container, where their pinned native binary is
# installed (no unsigned binaries on developer laptops).
LINUX_ONLY_CHECKS: list[tuple[str, list[str]]] = [
    ("secret scan", ["gitleaks", "dir", ".", "--no-banner", "--redact"]),
]

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
    inner = " && ".join(" ".join(cmd) for _, cmd in CHECKS + LINUX_ONLY_CHECKS)
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
    args = parser.parse_args()

    results = []
    if not args.linux:
        results.append(run_native())
    if args.linux or args.all:
        results.append(run_linux())
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
