"""The kill switch at an agent CLI's turn boundaries (M2-19, M2-60; decision 89).

The engine asks the kill switch before every step, and its watchdog polls it while a step
runs. The native runner also asks before each model request (`StepContext.turn_guard`),
and a pause there saves the conversation to resume later. The agent-CLI runners
(claude-code, codex, the ACP backend) cannot save a conversation of the agent's, so:

  * a **kill** is acted on at the first turn boundary the runner sees - before each prompt
    or exec, at each model message or tool call the agent reports - by raising
    `RunKilled`: the runner's own cleanup and the step's process scope stop everything it
    started, and the engine closes the run `killed`;
  * a **pause** lets the step finish: the engine holds the run at the next step boundary
    (decision 89). The verdict is noted (`held`) and logged once.
"""

import logging

from agentic.agents.runner import TurnGuard
from agentic.killswitch import RunKilled, Verdict

_log = logging.getLogger("agentic.backends.turns")


class TurnCheck:
    """Asks a step's turn guard at a turn boundary; nothing when the step has none."""

    def __init__(self, guard: TurnGuard | None) -> None:
        self.guard = guard
        self.held: Verdict | None = None

    async def __call__(self) -> None:
        if self.guard is None:
            return
        verdict = await self.guard()
        if verdict.action == "kill":
            raise RunKilled(verdict)
        if verdict.action == "pause" and self.held is None:
            _log.info(
                "%s: this step finishes its work and the run holds before the next step",
                verdict.describe(),
            )
        if verdict.action == "pause":
            self.held = verdict


__all__ = ["TurnCheck", "TurnGuard"]
