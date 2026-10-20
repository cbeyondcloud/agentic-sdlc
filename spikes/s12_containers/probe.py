"""S12 throwaway probe: which container engine is on this machine, and could
scripts/check.py drive it through a CONTAINER_ENGINE switch?

    uv run python spikes/s12_containers/probe.py          # detect only
    uv run python spikes/s12_containers/probe.py --run    # also run bubblewrap in the check image

Installs nothing. Reports, per engine CLI found on PATH:
  - its path, version and (on Windows) Authenticode signature status;
  - the server it talks to (Docker Desktop, Rancher Desktop, Podman machine, Engine ...);
  - whether `<engine> compose version` works;
With --run, it runs `bwrap --ro-bind / / --proc /proc --dev /dev true` inside the
already-built check image with the same --security-opt flags scripts/check.py uses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "scripts" / "linux-check.Dockerfile"
SANDBOX_OPTS = [
    "--security-opt",
    "seccomp=unconfined",
    "--security-opt",
    "systempaths=unconfined",
]
CANDIDATES = ("docker", "podman", "nerdctl")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)


def resolve_engine() -> str | None:
    """CONTAINER_ENGINE wins; otherwise the first of docker, podman on PATH."""
    wanted = os.environ.get("CONTAINER_ENGINE", "").strip()
    if wanted:
        return wanted if shutil.which(wanted) else None
    return next((c for c in ("docker", "podman") if shutil.which(c)), None)


def authenticode(path: str) -> str:
    if platform.system() != "Windows":
        return "n/a (not Windows)"
    ps = (
        f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; "
        "\"$($s.Status) | $($s.SignerCertificate.Subject -replace ',.*$','')\""
    )
    out = run(["powershell", "-NoProfile", "-Command", ps])
    return out.stdout.strip() or out.stderr.strip()


def server_info(engine: str) -> str:
    fmt = {
        "docker": "{{.OperatingSystem}} | {{.Architecture}} | {{.ServerVersion}}",
        "podman": "{{.Host.Distribution.Distribution}} | {{.Host.Arch}} | rootless={{.Host.Security.Rootless}}",
    }.get(engine)
    out = run([engine, "info", "--format", fmt] if fmt else [engine, "info"])
    if out.returncode != 0:
        return f"no server ({out.stderr.strip().splitlines()[-1] if out.stderr.strip() else 'error'})"
    return out.stdout.strip().splitlines()[0]


def licence_note(engine: str, info: str) -> str:
    if engine == "docker" and "Docker Desktop" in info:
        return "Docker Desktop: needs a paid seat in orgs >=250 staff or >=$10M revenue"
    return "free (Apache-2.0 engine)"


def check_image_tag() -> str:
    digest = hashlib.sha256(DOCKERFILE.read_bytes()).hexdigest()[:12]
    return f"agentic-check-linux:{digest}"


def bwrap_test(engine: str) -> str:
    tag = check_image_tag()
    if run([engine, "image", "inspect", tag]).returncode != 0:
        return f"skipped: {tag} not built for {engine} (run scripts/check.py --linux first)"
    cmd = [
        engine,
        "run",
        "--rm",
        *SANDBOX_OPTS,
        tag,
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--unshare-all",
        "true",
    ]
    out = run(cmd)
    if out.returncode == 0:
        return "bubblewrap OK"
    return f"bubblewrap FAILED rc={out.returncode}: {out.stderr.strip()[:200]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", action="store_true", help="also run bubblewrap in the check image")
    args = parser.parse_args()

    report: dict[str, object] = {
        "host": f"{platform.system()} {platform.release()} {platform.machine()}",
        "CONTAINER_ENGINE": os.environ.get("CONTAINER_ENGINE", "(unset)"),
        "resolved_engine": resolve_engine(),
        "engines": {},
    }
    for name in CANDIDATES:
        path = shutil.which(name)
        if path is None:
            report["engines"][name] = "not on PATH"
            continue
        info = server_info(name)
        compose = run([name, "compose", "version"])
        entry = {
            "path": path,
            "version": run([name, "--version"]).stdout.strip(),
            "signature": authenticode(path),
            "server": info,
            "licence": licence_note(name, info),
            "compose": compose.stdout.strip().splitlines()[0]
            if compose.returncode == 0
            else "unavailable",
        }
        if args.run:
            entry["bwrap_in_check_image"] = bwrap_test(name)
        report["engines"][name] = entry
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
