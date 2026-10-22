"""The Linux stage of scripts/check.py runs the control-plane database tests against a
Postgres sidecar on a private container network, and always removes both."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check  # noqa: E402


class _Runner:
    """Records every command; the check container fails, everything else succeeds."""

    def __init__(self, fail_checks: bool) -> None:
        self.calls: list[tuple[list[str], dict | None]] = []
        self.fail_checks = fail_checks

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs.get("env")))
        failing = self.fail_checks and check.CHECK_CONTAINER_MARK in cmd
        return subprocess.CompletedProcess(cmd, 1 if failing else 0, stdout=b"", stderr=b"")


def _run(monkeypatch, fail_checks: bool) -> tuple[bool, _Runner]:
    runner = _Runner(fail_checks)
    monkeypatch.setattr(check, "container_engine", lambda: "podman")
    monkeypatch.setattr(check, "linux_image", lambda engine: "agentic-check-linux:test")
    monkeypatch.setattr(check, "_github_token", lambda: "gh-test-value")
    monkeypatch.setattr(check, "tracked_files_tar", lambda: b"")
    return check.run_linux(run=runner), runner


def test_linux_stage_starts_postgres_sidecar_on_a_private_network(monkeypatch):
    ok, runner = _run(monkeypatch, fail_checks=True)
    assert ok is False
    commands = [cmd for cmd, _ in runner.calls]
    network_create = next(c for c in commands if c[1:3] == ["network", "create"])
    network = network_create[3]
    postgres = next(c for c in commands if check.POSTGRES_IMAGE in c)
    assert postgres[:2] == ["podman", "run"]
    assert ["--network", network] == postgres[postgres.index("--network") :][:2]
    assert "--network-alias" in postgres or "--name" in postgres
    checks_cmd, checks_env = next(
        (c, e) for c, e in runner.calls if check.CHECK_CONTAINER_MARK in c
    )
    assert ["--network", network] == checks_cmd[checks_cmd.index("--network") :][:2]
    # both variables reach the container by name; the DSN (with its password) never
    # appears on a command line
    assert "AGENTIC_CP_TEST_DSN" in checks_cmd
    assert "AGENTIC_CP_REQUIRE_PG" in checks_cmd
    assert checks_env["AGENTIC_CP_REQUIRE_PG"] == "1"
    assert checks_env["AGENTIC_CP_TEST_DSN"].startswith("postgresql://")
    assert all(checks_env["AGENTIC_CP_TEST_DSN"] not in " ".join(c) for c in commands)
    # cleanup after the failed checks: the sidecar, then the network
    assert commands.index(checks_cmd) < len(commands) - 2
    removal = commands[commands.index(checks_cmd) + 1 :]
    assert any(c[1:3] == ["rm", "-f"] for c in removal)
    assert removal[-1][1:3] == ["network", "rm"] and removal[-1][3] == network


def test_linux_stage_cleans_up_after_passing_checks_too(monkeypatch):
    ok, runner = _run(monkeypatch, fail_checks=False)
    assert ok is True
    assert runner.calls[-1][0][1:3] == ["network", "rm"]


def test_the_postgres_image_is_pinned_by_digest():
    name, _, digest = check.POSTGRES_IMAGE.partition("@sha256:")
    assert name.startswith("postgres:17.")
    assert len(digest) == 64
