"""The engine's kill-switch port in org mode (M2-19, spec §8.6; rulings R1, R2).

`ControlPlaneKillSwitch` asks the control plane for the switches that may apply to a run
(`GET /v1/killswitch/state` for its team, repo and pipeline), caches the answer for
`CACHE_TTL_S` and decides per step with `agentic.killswitch.decide`. When the control plane
cannot be reached it keeps the last good answer while that is younger than `STALE_AFTER_S`,
and after that answers `unconfirmed` - a pause, never "run" (master Review Focus 3). A
failed fetch is retried once per TTL, not on every check, so an outage does not make every
turn wait on the network. Thread-safe: the engine asks from worker threads.

With these values a kill reaches a running step within TTL + `POLL_S` (the engine's
watchdog interval) = 15 s, inside the 30 s target (M2-54, measured by the E2E in
`control-plane/tests/e2e`); a pause or unconfirmed state takes effect at the next turn.

The server's `ttl_seconds` (`AGENTIC_CP_KILL_TTL_SECONDS`) can shorten the cache, never
lengthen it past `CACHE_TTL_S` (DF-516); `checked_at` is informational - answers are aged
by this process's monotonic clock, never a laptop's wall clock. Each poll sends the last
answer's `ETag`; a 304 confirms the switches already held (DF-506)."""

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
#: The shortest cache a server's `ttl_seconds` can ask for (seconds).
MIN_TTL_S = 1.0


@dataclass
class _Entry:
    switches: list[Switch] | None = None
    confirmed_at: float | None = None  # monotonic time of the last good answer
    attempted_at: float | None = None
    problem: str = ""
    etag: str | None = None  # the last full answer's tag (DF-506)
    ttl_s: float | None = None  # the cache time the last answer allows (DF-516)


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
            ttl = entry.ttl_s if entry.ttl_s is not None else self.ttl_s
            due = entry.confirmed_at is None or now - entry.confirmed_at >= ttl
            retry = entry.attempted_at is None or now - entry.attempted_at >= ttl
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
        # a tag is sent only with an answer to confirm: a 304 then keeps that answer
        held = entry.etag if entry.switches is not None else None
        try:
            state, etag = self.client.kill_state_reply(
                team=scope.team, repo=scope.repo, pipeline=scope.pipeline, etag=held
            )
        except ControlPlaneError as error:
            entry.problem = str(error)
            return
        if state is None:  # 304: unchanged since the answer held
            entry.confirmed_at, entry.problem = now, ""
            return
        entry.etag = etag
        entry.ttl_s = min(self.ttl_s, max(MIN_TTL_S, float(state.ttl_seconds)))
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
