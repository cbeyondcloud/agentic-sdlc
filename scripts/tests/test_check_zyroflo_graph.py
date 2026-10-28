"""scripts/check.py demands the Zyroflo domain-graph tests run, natively and in the Linux
container, so a missing installer is a failure there rather than a skip (DF-463)."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check  # noqa: E402

FLAG = "AGENTIC_REQUIRE_ZYROFLO_GRAPH"


def test_native_checks_require_the_graph_online_and_offline(monkeypatch):
    seen = []

    def run(cmd, **kwargs):
        seen.append(kwargs.get("env"))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(check.subprocess, "run", run)
    monkeypatch.setattr(check, "checks_for", lambda offline: [("tests", ["pytest"])])
    monkeypatch.setattr(check, "_warn_stale_exceptions", lambda label: None)
    check.run_native(offline=False)
    check.run_native(offline=True)
    assert [env[FLAG] for env in seen] == ["1", "1"]
    assert seen[1]["UV_OFFLINE"] == "1"


def test_linux_checks_require_the_graph(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append((list(cmd), kwargs.get("env")))
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(check, "container_engine", lambda: "podman")
    monkeypatch.setattr(check, "linux_image", lambda engine: "agentic-check-linux:test")
    monkeypatch.setattr(check, "_github_token", lambda: "gh-test-value")
    monkeypatch.setattr(check, "tracked_files_tar", lambda: b"")
    assert check.run_linux(run=run) is True
    checks_cmd, checks_env = next((c, e) for c, e in calls if check.CHECK_CONTAINER_MARK in c)
    assert ["-e", FLAG] == checks_cmd[checks_cmd.index(FLAG) - 1 :][:2]
    assert checks_env[FLAG] == "1"
