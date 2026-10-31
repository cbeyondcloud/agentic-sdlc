"""The umbrella Helm chart as a release file (DF-801, owner decision 2026-10-30).

`package_chart` writes `agentic-platform-<chart version>.tgz` into the release folder, so
`release.py sign` signs it with the release's other files (an org Ed25519 key, SSH
signature format: decision 105) and `ssh-keygen -Y verify` checks it. The archive has the
layout `helm package` gives (one top folder named after the chart) and holds only the
chart's committed files minus `.helmignore` matches. Entries are sorted, owned by 0:0 with
mode 0644 and dated SOURCE_DATE_EPOCH, and the gzip header carries the same date, so two
builds of one commit are byte-identical. `helm install` and `helm push` (an org OCI
registry) read it as it is; no helm binary is needed to build it.
"""

from __future__ import annotations

import fnmatch
import gzip
import io
import re
import tarfile
from pathlib import Path, PurePosixPath

from agentic_release.build import tracked_files

CHART_DIR = "deploy/helm/agentic-platform"
_FIELD = re.compile(r"(?m)^(name|version): *\"?([^\"\s]+)\"? *$")


class ChartError(RuntimeError):
    """The chart cannot be packaged for this release."""


def _fields(text: str) -> dict[str, str]:
    return {key: value for key, value in _FIELD.findall(text)}


def _ignored(rel: str, patterns: list[str]) -> bool:
    parts = PurePosixPath(rel).parts
    for pattern in patterns:
        bare = pattern.rstrip("/")
        if fnmatch.fnmatch(rel, bare) or any(fnmatch.fnmatch(p, bare) for p in parts):
            return True
    return False


def package_chart(
    repo: Path, out: Path, *, epoch: int, expected: str | None = None, folder: str = CHART_DIR
) -> Path:
    """Package the chart at `repo/folder` into `out`; `expected` is the chart version the
    release requires (ChartError when Chart.yaml says otherwise)."""
    root = repo / folder
    fields = _fields((root / "Chart.yaml").read_text(encoding="utf-8"))
    name, chart_version = fields.get("name"), fields.get("version")
    if not name or not chart_version:
        raise ChartError(f"{folder}/Chart.yaml has no name or version")
    if expected is not None and chart_version != expected:
        raise ChartError(
            f"{folder}/Chart.yaml has version {chart_version}, the release needs {expected}: "
            "run `release.py version set`"
        )
    ignore_file = root / ".helmignore"
    patterns = []
    if ignore_file.is_file():
        for line in ignore_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    prefix = folder.rstrip("/") + "/"
    files = sorted(
        rel
        for rel in (path[len(prefix) :] for path in tracked_files(repo, folder))
        if not _ignored(rel, patterns)
    )
    if "Chart.yaml" not in files:
        raise ChartError(f"{folder}/Chart.yaml is not committed")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in files:
            data = (root / rel).read_bytes()
            info = tarfile.TarInfo(f"{name}/{rel}")
            info.size, info.mtime, info.mode = len(data), epoch, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}-{chart_version}.tgz"
    with path.open("wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=epoch) as gz:
            gz.write(raw.getvalue())
    return path
