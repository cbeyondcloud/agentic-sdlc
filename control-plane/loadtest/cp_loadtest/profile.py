"""What one simulated run does, and when (M3-22).

The template is a real run ledger: the 40 events of the M1-68 acceptance run of
`feature-delivery` (14 step executions, 4 gate waits - one rejection - and 2 loop-backs of
the build loop). The counts come from it; the durations do not, because the acceptance run
uses a scripted model. `Timing` sets them: a step lasts `step_s` and a person answers a
gate after `gate_s`, each with +/- `jitter` so runs do not move in lockstep.

At a gate the CLI process ends (`agentic run` exits waiting), so a paused run sends no
heartbeat and polls no kill switch until a person decides (`agentic approve` resumes)."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "feature-delivery.events.jsonl"
#: Seconds between the engine's events that follow each other with no work in between.
SHORT_GAP_S = {"step_started": 0.2, "gate_waiting": 0.05, "loop_back": 0.1, "run_closed": 0.05}


@dataclass(frozen=True)
class Timing:
    """Durations of the simulated work, in seconds. `heartbeat_s` is the policy's
    `run_health.heartbeat_seconds` (default 60) and `kill_poll_s` the CLI's kill-switch
    cache (`agentic_cli.killswitch.CACHE_TTL_S`, 10)."""

    step_s: float = 60.0
    gate_s: float = 30.0
    heartbeat_s: float = 60.0
    kill_poll_s: float = 10.0
    jitter: float = 0.5


@dataclass(frozen=True)
class Action:
    """Wait `delay_s`, then: produce template event `index` (`event`), stop as at a gate
    (`gate`: the CLI exits) or start again (`resume`: a person decided)."""

    delay_s: float
    kind: Literal["event", "gate", "resume"]
    index: int = -1


def load_template(path: Path = FIXTURE) -> list[dict]:
    """The recorded run's events, in order."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def plan_run(template: list[dict], timing: Timing, rng: random.Random) -> list[Action]:
    """The run's actions in order; the sum of the delays is its length."""

    def jittered(seconds: float) -> float:
        if not timing.jitter:
            return seconds
        return seconds * rng.uniform(1 - timing.jitter, 1 + timing.jitter)

    actions: list[Action] = []
    for index, entry in enumerate(template):
        event = entry.get("event")
        if event == "step_finished":
            actions.append(Action(jittered(timing.step_s), "event", index))
        elif event == "gate_decided":
            actions.append(Action(jittered(timing.gate_s), "resume"))
            actions.append(Action(0.0, "event", index))
        else:
            actions.append(Action(SHORT_GAP_S.get(event, 0.0), "event", index))
        if event == "gate_waiting":
            actions.append(Action(0.0, "gate"))
    return actions
