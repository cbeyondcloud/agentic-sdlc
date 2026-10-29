"""`loadtest.py down`: the stack's state file goes only with the stack (M3-22 review F1).

Compose is faked here: these tests check what `down` does with compose's answer, not
compose itself."""

import json
import subprocess

import pytest
from cp_loadtest import cli, stack
from cp_loadtest.stack import SECRETS, Stack


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(stack, "STATE", tmp_path)
    monkeypatch.setattr(stack, "container_engine", lambda: "docker")
    return tmp_path / "stack.json"


def _compose(monkeypatch, returncode: int) -> list[dict]:
    """Fake `subprocess.run` for compose; returns the list of calls it saw."""
    calls = []

    def run(command, **kwargs):
        calls.append({"command": command, "env": kwargs.get("env", {})})
        return subprocess.CompletedProcess(command, returncode, "", "engine stopped")

    monkeypatch.setattr(stack.subprocess, "run", run)
    return calls


def _write(state) -> dict:
    found = {name: f"value-{name.lower()}" for name in SECRETS}
    state.write_text(json.dumps(found), encoding="utf-8")
    return found


def test_a_failed_down_keeps_the_state_file_and_fails(state, monkeypatch, capsys):
    _write(state)
    _compose(monkeypatch, returncode=1)
    with pytest.raises(SystemExit) as failed:
        cli._down(None)
    assert "compose down failed" in str(failed.value)
    assert state.exists(), "the passwords of a stack still running must not be thrown away"
    assert "removed" not in capsys.readouterr().out


def test_a_successful_down_removes_the_state_file(state, monkeypatch):
    _write(state)
    calls = _compose(monkeypatch, returncode=0)
    assert cli._down(None) == 0
    assert not state.exists()
    assert calls[0]["command"][-3:] == ["down", "-v", "--remove-orphans"]


def test_down_runs_without_a_state_file_so_leftovers_can_be_removed(state, monkeypatch):
    calls = _compose(monkeypatch, returncode=0)
    assert cli._down(None) == 0
    env = calls[0]["env"]
    assert all(env.get(name) for name in SECRETS), "compose.yml requires every secret"
    assert not state.exists()


def test_load_without_a_state_file_still_refuses_for_other_commands(state):
    with pytest.raises(SystemExit, match="up"):
        Stack.load()
