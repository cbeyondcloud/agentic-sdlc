"""Kill-switch propagation, end to end (M2-54; spec §8.6; SP15 2a rulings R1, R2, R4).

The real control plane (Postgres, forced RLS, the audit chain) serves the real CLI, which
runs a pipeline in this process against a scripted model. The run's only step is in a slow
turn - blocked in a `run_shell` call whose command started a child and a grandchild - when
an org admin flips a switch through the API. Measured from the flip (taken before the
request is sent): the CLI learning of it (a state poll served), the run being stopped (the
CLI's command returned) and the step's processes being gone. Every run's timing is printed
in the terminal summary and carried in every assertion message.

Bounds: a kill stops the run within 30 s and within one turn (no model call after the flip
but the one in progress), with no process left. The flip lands just after the step's first
state fetch, the worst phase for a step blocked in one turn: the watchdog refetches only
when the answer is a TTL (10 s) old, so these runs measure about TTL + cleanup. By design
the worst case of any phase is TTL + the watchdog poll (5 s) = 15 s plus cleanup: a fetch
made off the watchdog's beat (at a step boundary) can wait one more poll.

A pause holds at the end of the turn in progress once the CLI has learned of it; the slow
turn here ends when the control plane has served the CLI a state poll after the flip, so
both bounds are asserted without a fixed sleep (ruling R2: a turn shorter than the
propagation window can be followed by more turns before the pause lands). Unconfirmable
switch state - the control plane unreachable past the stale window - pauses the same way
(the port's clock is advanced past the window instead of waiting a minute, ruling R4)."""

from __future__ import annotations

import time

import httpx2
import pytest
from _killswitch_e2e import (
    KILL_BOUND_S,
    PIPELINE,
    PIPELINE_BYTES,
    RUNS,
    SIGNAL_ENV,
    TEAM,
    TIMINGS,
    MidStep,
    ScriptedModel,
    Timing,
    gone_after,
    release,
    report,
    slow_turn_script,
    wait_for,
)

from agentic.orgapi import API_PREFIX
from agentic_cli import killswitch as killswitch_port
from agentic_cli.exit_codes import ExitCode

SCOPES = {
    "tenant": {"scope_kind": "tenant"},
    "team": {"scope_kind": "team", "scope_value": TEAM},
    "pipeline": {"scope_kind": "pipeline", "scope_value": PIPELINE},
}


def _registered(plane, run_id: str) -> int:
    """The control plane's answer for the run (200: it registered after the org checks)."""
    headers = {"Authorization": f"Bearer {plane.admin_token}"}
    with httpx2.Client(base_url=plane.base, headers=headers, timeout=30) as http:
        return http.get(f"{API_PREFIX}/runs/{run_id}").status_code


def _flip(plane, step: MidStep, body: dict) -> None:
    with plane.admin() as admin:
        made = admin.set_switch(body)
    step.notes["flipped_at"] = time.monotonic()
    step.notes["switch"] = str(made.id)


def _clear(plane, step: MidStep) -> None:
    if switch := step.notes.get("switch"):
        with plane.admin() as admin:
            admin.clear_switch(str(switch), reason="e2e: next run")


def _aware(plane, step: MidStep) -> float | None:
    flipped = step.notes.get("flipped_at")
    seen = plane.log.first_after(flipped) if flipped is not None else None
    return None if seen is None or step.acted_at is None else seen - step.acted_at


@pytest.mark.parametrize("scope", list(SCOPES))
def test_a_kill_switch_stops_a_running_pipeline_5_of_5(
    scope, plane, workspace, cli, tmp_path, monkeypatch
):
    cached = workspace.home / "policy" / "acme" / "repo" / "pipelines" / f"{PIPELINE}.yaml"
    assert cached.read_bytes() == PIPELINE_BYTES  # CRLF and UTF-8 as committed (D33)
    timings: list[Timing] = []
    for n in range(1, RUNS + 1):
        where = tmp_path / f"{scope}-{n}"
        where.mkdir()
        monkeypatch.setenv(SIGNAL_ENV, str(where))
        workspace.model.current = model = ScriptedModel(slow_turn_script())
        reason = f"e2e: {scope} kill, run {n}"
        body = {**SCOPES[scope], "mode": "kill", "reason": reason}
        step = MidStep(where, lambda s, body=body: _flip(plane, s, body))
        step.start()
        try:
            result = cli("--json", "run", PIPELINE)
            stopped_at = time.monotonic()
        finally:
            step.finish()
            _clear(plane, step)
        assert step.error is None and step.acted_at is not None, (
            f"run {n}: {step.error}\n{result.err}\n{result.out}"
        )
        flip_at = step.acted_at
        timing = Timing(
            scenario=scope,
            run=n,
            to_aware_s=_aware(plane, step),
            to_stopped_s=stopped_at - flip_at,
            to_gone_s=gone_after(step.pids, flip_at),
            calls_after=model.after(flip_at),
        )
        TIMINGS.append(timing)
        timings.append(timing)
        so_far = report(timings)
        assert result.code == ExitCode.STOPPED, f"{result.err}\n{so_far}"
        data = result.json()
        assert data["status"] == "killed", so_far
        assert data["ended_by"] == plane.admin_email and reason in data["end_reason"]
        assert _registered(plane, data["run_id"]) == 200  # the org checks passed (D33)
        assert timing.to_stopped_s <= KILL_BOUND_S, f"run {n} too slow\n{so_far}"
        assert timing.to_gone_s is not None, f"run {n} left processes running\n{so_far}"
        assert timing.to_gone_s <= KILL_BOUND_S, f"run {n} processes too slow\n{so_far}"
        assert timing.calls_after <= 1, f"run {n} made model calls after the kill\n{so_far}"
    worst = max(t.to_stopped_s for t in timings)
    assert worst <= KILL_BOUND_S, report(timings)


def _one_slow_run(plane, workspace, cli, where, act) -> tuple[MidStep, ScriptedModel, object]:
    """Run the pipeline while `act` runs once the run is in its slow turn; the step, the
    model and the CLI's result, with the time the command returned in `step.notes`."""
    workspace.model.current = model = ScriptedModel(slow_turn_script())
    step = MidStep(where, act)
    step.start()
    try:
        result = cli("--json", "run", PIPELINE)
        step.notes["stopped_at"] = time.monotonic()
    finally:
        step.finish()
    assert step.error is None and step.acted_at is not None, (
        f"{step.error}\n{result.err}\n{result.out}"
    )
    return step, model, result


def _timing(scenario: str, step: MidStep, model: ScriptedModel, aware_at) -> Timing:
    flip_at = step.acted_at
    timing = Timing(
        scenario=scenario,
        run=1,
        to_aware_s=None if aware_at is None else aware_at - flip_at,
        to_stopped_s=step.notes["stopped_at"] - flip_at,
        to_gone_s=gone_after(step.pids, flip_at),
        calls_after=model.after(flip_at),
    )
    TIMINGS.append(timing)
    return timing


def test_a_pause_switch_holds_within_one_turn_and_resume_repeats_no_model_call(
    plane, workspace, cli, tmp_path, monkeypatch
):
    """M2-10 end to end: the turn in progress finishes, no model call follows, the run
    holds (`hold`); once the switch is cleared `agentic resume` sends exactly one more
    request - the one carrying the slow command's result - and completes."""
    where = tmp_path / "pause"
    where.mkdir()
    monkeypatch.setenv(SIGNAL_ENV, str(where))
    reason = "e2e: maintenance window"

    def pause(step: MidStep) -> None:
        body = {"scope_kind": "team", "scope_value": TEAM, "mode": "pause", "reason": reason}
        _flip(plane, step, body)
        flipped = step.notes["flipped_at"]
        step.notes["aware_at"] = wait_for(lambda: plane.log.first_after(flipped), KILL_BOUND_S)
        release(step.where)  # the slow turn ends once the CLI knows

    step, model, result = _one_slow_run(plane, workspace, cli, where, pause)
    try:
        timing = _timing("pause", step, model, step.notes["aware_at"])
        assert result.code == ExitCode.PAUSED, f"{result.err}\n{timing.line()}"
        pending = result.json()["pending"]
        assert pending["id"] == "hold" and reason in pending["reason"]
        assert timing.calls_after == 0 and len(model.calls) == 1, timing.line()
        assert timing.to_aware_s is not None and timing.to_aware_s <= KILL_BOUND_S, timing.line()
        assert timing.to_stopped_s <= KILL_BOUND_S, timing.line()
        assert timing.to_gone_s is not None, timing.line()
    finally:
        _clear(plane, step)
    resumed = cli("--json", "resume", result.json()["run_id"])
    assert resumed.code == ExitCode.OK, resumed.err
    assert resumed.json()["status"] == "completed"
    assert len(model.calls) == 2  # the held request, sent once; turn 1 was not repeated
    messages = model.bodies[1]["messages"]
    calls = [m for m in messages if m["role"] == "assistant" and m.get("tool_calls")]
    results = [m for m in messages if m["role"] == "tool"]
    assert len(calls) == 1 and calls[0]["tool_calls"][0]["id"] == "call-1"
    assert len(results) == 1 and results[0]["tool_call_id"] == "call-1"
    assert "released" in results[0]["content"]


class _Skew:
    """A monotonic clock that can be moved forward (the kill-switch port's only)."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset


def test_unconfirmable_switch_state_pauses_a_running_pipeline(
    plane, workspace, cli, tmp_path, monkeypatch
):
    """Master Review Focus 3 end to end: the control plane stops answering (connections
    refused) and the port's last confirmed answer passes the stale window: the run holds
    (`kill-switch-unconfirmed`) at the end of the turn in progress, never continues."""
    skew = _Skew()

    class SkewedPort(killswitch_port.ControlPlaneKillSwitch):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, clock=skew, **kwargs)

    monkeypatch.setattr(killswitch_port, "ControlPlaneKillSwitch", SkewedPort)
    where = tmp_path / "stale"
    where.mkdir()
    monkeypatch.setenv(SIGNAL_ENV, str(where))

    def outage(step: MidStep) -> None:
        plane.server.stop()
        skew.offset += killswitch_port.STALE_AFTER_S + 1
        step.notes["flipped_at"] = time.monotonic()
        release(step.where)

    step, model, result = _one_slow_run(plane, workspace, cli, where, outage)
    timing = _timing("stale", step, model, None)
    assert result.code == ExitCode.PAUSED, f"{result.err}\n{timing.line()}"
    pending = result.json()["pending"]
    assert pending["id"] == "kill-switch-unconfirmed", pending
    assert "cannot reach the control plane" in pending["reason"]
    assert timing.calls_after == 0 and len(model.calls) == 1, timing.line()
    assert timing.to_stopped_s <= KILL_BOUND_S, timing.line()
    assert timing.to_gone_s is not None, timing.line()
