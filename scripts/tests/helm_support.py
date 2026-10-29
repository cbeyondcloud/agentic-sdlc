"""Run helm and kubeconform for the chart tests (M3-02), only ever on Linux.

Both are native binaries, so they never run on a developer's laptop: in the Linux check
image (`scripts/check.py --linux`) they are installed, pinned by sha256
(scripts/linux-check.Dockerfile); on Windows and macOS they run in the pinned images below
through the container engine (docker or podman). The chart and any values file travel into
the container as a tar on stdin, so no bind mount or path conversion is involved. Without
either, the tests skip with the reason; in the check image a missing binary fails."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "agentic-platform"
#: The same releases as in scripts/linux-check.Dockerfile (a test keeps them equal).
HELM_VERSION = "4.3.1"
KUBECONFORM_VERSION = "0.8.0"
#: alpine/helm 4.3.1 (2026-10-15) and kubeconform v0.8.0 (2026-06-04), by tag and digest.
HELM_IMAGE = (
    "alpine/helm:4.3.1@sha256:e3f8d5c462d75470fbb1fce79ad5c20063657096d1f98c0fcea869e2600e2e5e"
)
KUBECONFORM_IMAGE = (
    "ghcr.io/yannh/kubeconform:v0.8.0"
    "@sha256:faffaf43f95aa6425306e1ab8d6fcad72acb9049158f38e574c085ea1ec0f64e"
)
#: The oldest and newest Kubernetes minor versions the chart is validated against.
KUBERNETES_VERSIONS = ("1.30.0", "1.36.0")
NO_TOOLS = (
    "helm and kubeconform run only in Linux containers: install docker or podman (or set "
    "CONTAINER_ENGINE), or run `uv run python scripts/check.py --linux`"
)
#: `docker run` / `podman run` exit with 125 when the container cannot start at all (no
#: daemon, an image that cannot be pulled offline).
ENGINE_FAILED = 125


def _check_module():
    """scripts/check.py, loaded by path: it owns the container engine choice."""
    name = "_agentic_check_script"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "check.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Tools:
    """Where helm and kubeconform run: on PATH (the check image) or through an engine."""

    engine: str | None

    def helm(self, args: list[str], files: dict[str, str] | None = None) -> Result:
        """`helm <args>` in a folder holding the chart (`agentic-platform/`) and `files`."""
        payload = _workspace_tar(files or {})
        if self.engine is None:
            with tempfile.TemporaryDirectory() as work:
                with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
                    tar.extractall(work, filter="data")
                done = subprocess.run(["helm", *args], cwd=work, capture_output=True, timeout=300)
        else:
            script = 'mkdir -p /w && tar -xf - -C /w && cd /w && exec helm "$@"'
            cmd = [self.engine, "run", "--rm", "-i", "--entrypoint", "sh", HELM_IMAGE]
            done = subprocess.run(
                [*cmd, "-c", script, "sh", *args], input=payload, capture_output=True, timeout=300
            )
        return _result(done, self.engine)

    def kubeconform(self, manifests: str, kubernetes_version: str) -> Result:
        """kubeconform on `manifests` (strict; the prometheus-operator CRDs skipped)."""
        args = [
            "-strict",
            "-summary",
            "-kubernetes-version",
            kubernetes_version,
            "-skip",
            "ServiceMonitor",
            "-",
        ]
        if self.engine is None:
            cmd = ["kubeconform", *args]
        else:
            cmd = [self.engine, "run", "--rm", "-i", KUBECONFORM_IMAGE, *args]
        done = subprocess.run(
            cmd, input=manifests.encode("utf-8"), capture_output=True, timeout=300
        )
        return _result(done, self.engine)


def _result(done: subprocess.CompletedProcess, engine: str | None) -> Result:
    stdout = done.stdout.decode("utf-8", "replace")
    stderr = done.stderr.decode("utf-8", "replace")
    if engine is not None and done.returncode == ENGINE_FAILED:
        pytest.skip(f"{engine} could not start the pinned tool image: {stderr.strip()[:300]}")
    return Result(done.returncode, stdout, stderr)


def _workspace_tar(files: dict[str, str]) -> bytes:
    """The chart folder as `agentic-platform/` plus `files` (name -> text), LF only."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in sorted(CHART.rglob("*")):
            if path.is_file():
                _add(
                    tar, f"agentic-platform/{path.relative_to(CHART).as_posix()}", path.read_bytes()
                )
        for name, text in files.items():
            _add(tar, name, text.encode("utf-8"))
    return buffer.getvalue()


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def tools() -> Tools:
    """The tools, or a skip (a failure in the check image) naming what is missing."""
    if shutil.which("helm") and shutil.which("kubeconform"):
        return Tools(engine=None)
    if os.environ.get("AGENTIC_CHECK_IMAGE") == "1":
        pytest.fail("the Linux check image must carry the pinned helm and kubeconform")
    try:
        engine = _check_module().container_engine()
    except SystemExit as error:
        pytest.skip(f"{error} - {NO_TOOLS}")
    if engine is None:
        pytest.skip(NO_TOOLS)
    return Tools(engine=engine)


def values_file(values: dict[str, Any]) -> str:
    """`values` as a values file (JSON is YAML)."""
    return json.dumps(values, indent=2)


def manifests(text: str) -> list[dict[str, Any]]:
    """The objects of a `helm template` output (empty documents dropped)."""
    return [doc for doc in YAML(typ="safe", pure=True).load_all(text) if doc]
