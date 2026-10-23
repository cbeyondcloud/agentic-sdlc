"""The engine's kill-switch port in org mode (M2-19, spec §8.6; rulings R1, R2).

`ControlPlaneKillSwitch` asks the control plane for the switches that may apply to a run
(`GET /v1/killswitch/state` for its team, repo and pipeline), caches the answer for
`CACHE_TTL_S` and decides per step with `agentic.killswitch.decide`. When the control plane
cannot be reached it keeps the last good answer while that is younger than `STALE_AFTER_S`,
and after that answers `unconfirmed` - a pause, never "run" (master Review Focus 3). A
failed fetch is retried once per TTL, not on every check, so an outage does not make every
turn wait on the network. Thread-safe: the engine asks from worker threads.

With these values a kill reaches a running step within TTL + `POLL_S` (the engine's
watchdog interval) = 15 s, inside the 30 s target (M2-54); a pause or unconfirmed state
takes effect at the next turn."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from agentic.killswitch import RunScope, Switch, Verdict, decide, unconfirmed
from agentic_cli.control_plane import ControlPlaneClient, ControlPlaneError

#: How long one answer is used before the control plane is asked again (seconds).
CACHE_TTL_S = 10.0
#: How old the last confirmed answer may be before runs hold (seconds).
STALE_AFTER_S = 60.0
#: How often the engine's watchdog asks while a step runs (seconds).
POLL_S = 5.0


@dataclass
class _Entry:
    switches: list[Switch] | None = None
    confirmed_at: float | None = None  # monotonic time of the last good answer
    attempted_at: float | None = None
    problem: str = ""


class ControlPlaneKillSwitch:
    def __init__(
        self,
        client: ControlPlaneClient,
        *,
        user: str | None,
        team: str | None,
        repo: str | None,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = CACHE_TTL_S,
        stale_after_s: float = STALE_AFTER_S,
    ) -> None:
        self.client, self.user, self.team, self.repo = client, user, team, repo
        self.clock, self.ttl_s, self.stale_after_s = clock, ttl_s, stale_after_s
        self._entries: dict[tuple, _Entry] = {}
        self._lock = threading.Lock()

    def check(self, scope: RunScope) -> Verdict:
        # the user is the signed-in one (the control plane matched user switches to it)
        scope = replace(
            scope,
            user=(self.user or scope.user or "").lower() or None,
            team=scope.team or self.team,
            repo=scope.repo or self.repo,
        )
        key = (scope.team, scope.repo, scope.pipeline)
        with self._lock:
            entry = self._entries.setdefault(key, _Entry())
            now = self.clock()
            due = entry.confirmed_at is None or now - entry.confirmed_at >= self.ttl_s
            retry = entry.attempted_at is None or now - entry.attempted_at >= self.ttl_s
            if due and retry:
                self._fetch(entry, scope, now)
            if entry.confirmed_at is None or now - entry.confirmed_at > self.stale_after_s:
                age = (
                    f"for {int(now - entry.confirmed_at)} s"
                    if entry.confirmed_at is not None
                    else "since the run started"
                )
                return unconfirmed(f"cannot reach the control plane {age} ({entry.problem})")
            return decide(entry.switches or [], scope)

    def _fetch(self, entry: _Entry, scope: RunScope, now: float) -> None:
        entry.attempted_at = now
        try:
            state = self.client.kill_state(
                team=scope.team, repo=scope.repo, pipeline=scope.pipeline
            )
        except ControlPlaneError as error:
            entry.problem = str(error)
            return
        entry.switches = [
            Switch(
                id=str(s.id),
                scope_kind=s.scope_kind,
                scope_value=s.scope_value,
                mode=s.mode,
                reason=s.reason,
                by=s.created_by,
                team=s.team,
            )
            for s in state.switches
            if s.active
        ]
        entry.confirmed_at, entry.problem = now, ""


class Unconfirmable:
    """The port when switch state can never be confirmed (org mode with a control plane, but
    not signed in): every check holds the run, with the reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def check(self, scope: RunScope) -> Verdict:
        return unconfirmed(self.reason)
