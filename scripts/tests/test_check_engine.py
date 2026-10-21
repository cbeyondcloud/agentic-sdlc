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


def test_offline_leaves_out_only_the_checks_that_need_the_network():  # DF-97
    from check import CHECKS, checks_for

    assert checks_for(offline=False) == CHECKS
    names = [name for name, _ in checks_for(offline=True)]
    assert "known vulnerabilities" not in names
    assert names == [name for name, _ in CHECKS if name != "known vulnerabilities"]


def test_a_failed_vulnerability_check_points_at_offline():
    from check import failure_hint

    assert "--offline" in failure_hint("known vulnerabilities")
    assert failure_hint("tests") == ""


def test_an_offline_run_says_it_is_incomplete(monkeypatch, capsys):
    import check

    ran = []

    def fake_run(cmd, cwd=None, env=None):
        ran.append((cmd, (env or {}).get("UV_OFFLINE")))
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(check.subprocess, "run", fake_run)
    assert check.run_native(offline=True) is True
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out and "known vulnerabilities" in out
    assert all(flag == "1" for _, flag in ran)
    assert not any("pip-audit" in cmd for cmd, _ in ran)


def test_the_secret_scan_runs_before_anything_writes_into_the_tree():
    # Tests leave compiled files whose folded constants look like keys (P0-03 follow-up).
    from check import linux_steps

    names = [name for name, _ in linux_steps()]
    assert names.index("secret scan") < names.index("tests")
    assert names.index("secret scan") < names.index("lock file up to date")
