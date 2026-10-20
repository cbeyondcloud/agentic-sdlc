"""The container engine for the Linux stage of scripts/check.py (spike S12): Docker or
Podman, never tied to Docker Desktop."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check import container_engine  # noqa: E402


def _which(*present):
    return lambda name: f"/bin/{name}" if name in present else None


def test_the_named_engine_wins():
    assert container_engine({"CONTAINER_ENGINE": "podman"}, _which("docker", "podman")) == "podman"


def test_docker_is_found_first_then_podman():
    assert container_engine({}, _which("docker", "podman")) == "docker"
    assert container_engine({}, _which("podman")) == "podman"


def test_no_engine_is_none():
    assert container_engine({}, _which()) is None


def test_a_named_engine_that_is_missing_is_an_error():
    with pytest.raises(SystemExit, match="CONTAINER_ENGINE=podman"):
        container_engine({"CONTAINER_ENGINE": "podman"}, _which("docker"))


def test_an_unknown_engine_name_is_an_error():
    with pytest.raises(SystemExit, match="docker or podman"):
        container_engine({"CONTAINER_ENGINE": "lxc"}, _which("lxc"))


def test_the_secret_scan_runs_before_anything_writes_into_the_tree():
    # Tests leave compiled files whose folded constants look like keys (P0-03 follow-up).
    from check import linux_steps

    names = [name for name, _ in linux_steps()]
    assert names.index("secret scan") < names.index("tests")
    assert names.index("secret scan") < names.index("lock file up to date")
