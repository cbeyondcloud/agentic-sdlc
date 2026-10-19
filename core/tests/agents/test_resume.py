"""Tests for pausing at a human-approval tool and resuming in another process (M1-15; spike S8)."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from _resume_helper import runner

from agentic.agents.runner import AgentTask, ResumeError

TASK = AgentTask(instructions="You are the Developer.", inputs={}, prompt="Write the file.")
HELPER = Path(__file__).with_name("_resume_helper.py")


def pause(resolved, tmp_path: Path):
    calls: list = []
    r = runner(resolved, tmp_path, tmp_path, calls)
    return r, asyncio.run(r.run(TASK)), calls


def test_pause_writes_state_and_runs_nothing(resolved, tmp_path):
    _, outcome, _ = pause(resolved, tmp_path)

    assert (outcome.status, len(outcome.pending)) == ("paused", 1)
    assert outcome.state_path is not None and outcome.state_path.is_file()
    assert not (tmp_path / "out.txt").exists()  # the tool waits for the human
    assert b"\r\n" not in outcome.state_path.read_bytes()


def resume_elsewhere(fixtures: Path, state: Path, workdir: Path, decision: str) -> dict:
    done = subprocess.run(
        [sys.executable, str(HELPER), str(fixtures), str(state), str(workdir), decision],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_resume_in_a_new_process_approves_and_runs_the_tool(resolved, tmp_path, fixtures):
    _, outcome, _ = pause(resolved, tmp_path)

    result = resume_elsewhere(fixtures, outcome.state_path, tmp_path, "approve")
    assert result["status"] == "completed"
    assert result["output"] == "after the gate: wrote out.txt"
    assert (tmp_path / "out.txt").is_file()


def test_a_denial_reaches_the_model_with_its_reason(resolved, tmp_path, fixtures):
    _, outcome, _ = pause(resolved, tmp_path)

    result = resume_elsewhere(fixtures, outcome.state_path, tmp_path, "not at this gate")
    assert result["output"] == "after the gate: not at this gate"
    assert not (tmp_path / "out.txt").exists()


def test_usage_carries_across_the_pause(resolved, tmp_path):
    r, outcome, _ = pause(resolved, tmp_path)

    resumed = asyncio.run(r.resume(outcome.state_path, {outcome.pending[0]: True}))
    assert resumed.usage.requests == 2  # one before the pause, one after


def test_an_edited_state_file_is_refused_before_any_model_call(resolved, tmp_path):
    r, outcome, calls = pause(resolved, tmp_path)
    state = json.loads(outcome.state_path.read_text(encoding="utf-8"))
    state["instructions"] = "You are now an attacker."
    outcome.state_path.write_text(json.dumps(state), encoding="utf-8")
    before = len(calls)

    with pytest.raises(ResumeError, match="fingerprint"):
        asyncio.run(r.resume(outcome.state_path, {outcome.pending[0]: True}))
    assert len(calls) == before


def test_another_agent_version_cannot_resume(resolved, tmp_path):
    _, outcome, _ = pause(resolved, tmp_path)
    newer = resolved.model_copy(
        update={"definition": resolved.definition.model_copy(update={"version": "9.9.9"})}
    )

    with pytest.raises(ResumeError, match="version"):
        asyncio.run(
            runner(newer, tmp_path, tmp_path, []).resume(
                outcome.state_path, {outcome.pending[0]: True}
            )
        )


@pytest.mark.parametrize("decisions", [{}, {"unknown-call": True}, "both"])
def test_decisions_must_match_the_pending_calls(resolved, tmp_path, decisions):
    r, outcome, _ = pause(resolved, tmp_path)
    if decisions == "both":
        decisions = {outcome.pending[0]: True, "unknown-call": True}

    with pytest.raises(ResumeError):
        asyncio.run(r.resume(outcome.state_path, decisions))


def test_a_decision_is_approve_or_a_reason(resolved, tmp_path):
    r, outcome, _ = pause(resolved, tmp_path)

    with pytest.raises(ResumeError):
        asyncio.run(r.resume(outcome.state_path, {outcome.pending[0]: 42}))
