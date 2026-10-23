"""Scoped stop switches (spec §8.6; M2-06, M2-19, M2-10): one model for the control plane,
which stores them, and the CLI, which asks before every step and every agent turn.

A switch names one dimension of a run - the whole tenant (the spec's "global"), a team, a
user, a repo, a pipeline, an agent (by name, or name@version), a model or a backend - and
optionally a `team` it is limited to (a team lead's switches always are). Its `mode` is
`kill` (stop now, the run is marked killed) or `pause` (finish the current turn,
checkpoint, hold until the switch is cleared). `decide` turns the switches that match a
run's scope into one `Verdict`: kill beats pause beats run.

The engine reaches the switches through the `KillSwitch` port. Standalone mode uses
`NoKillSwitch`; the CLI's org implementation caches the control plane's answer and gives
an `unconfirmed` verdict - a pause, never a kill and never "run" - when it cannot confirm
the state for longer than its expiry (master Review Focus 3).

Model and backend values are opaque strings: no provider is named here."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from agentic.policy.repos import normalise_remote

ScopeKind = Literal["tenant", "team", "user", "repo", "pipeline", "agent", "model", "backend"]
SCOPE_KINDS: tuple[ScopeKind, ...] = (
    "tenant",
    "team",
    "user",
    "repo",
    "pipeline",
    "agent",
    "model",
    "backend",
)
#: The kinds that depend on the step being run, not on the run as a whole.
STEP_KINDS: frozenset[str] = frozenset({"agent", "model", "backend"})
Mode = Literal["kill", "pause"]
MODES: tuple[Mode, ...] = ("kill", "pause")
Action = Literal["run", "pause", "kill"]

#: The pause a run takes for a pause switch, and for switch state it cannot confirm.
HOLD_PAUSE = "hold"
UNCONFIRMED_PAUSE = "kill-switch-unconfirmed"
#: How a person names the tenant-wide switch on the command line.
GLOBAL = "global"

_TEAM = re.compile(r"[a-z][a-z0-9-]{0,62}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_AGENT = re.compile(r"[a-z][a-z0-9-]{0,63}(?:@[0-9A-Za-z][0-9A-Za-z.+-]{0,63})?")
_OPAQUE = re.compile(r"\S{1,200}")


@dataclass(frozen=True)
class RunScope:
    """What a run is, for matching: None where it is not known (a step's agent, model and
    backend are known only for that step)."""

    team: str | None = None
    user: str | None = None
    repo: str | None = None
    pipeline: str | None = None
    agent: str | None = None  # name@version
    model: str | None = None
    backend: str | None = None

    def with_step(self, *, agent: str | None, model: str | None, backend: str | None) -> RunScope:
        return replace(self, agent=agent, model=model, backend=backend)


@dataclass(frozen=True)
class Switch:
    id: str
    scope_kind: ScopeKind
    scope_value: str
    mode: Mode
    reason: str
    by: str
    team: str | None = None  # limited to the runs of this team


def _dimension(switch: Switch, scope: RunScope) -> bool:
    kind, value = switch.scope_kind, switch.scope_value
    if kind == "tenant":
        return True
    if kind == "agent":
        if not scope.agent:
            return False
        return value == scope.agent or ("@" not in value and scope.agent.split("@")[0] == value)
    found = getattr(scope, kind, None)
    return found is not None and found == value


def _in_team(switch: Switch, scope: RunScope) -> bool:
    return switch.team is None or switch.team == scope.team


def matches(switch: Switch, scope: RunScope) -> bool:
    """True when `switch` applies to a run (or step) with `scope`."""
    return _in_team(switch, scope) and _dimension(switch, scope)


def run_level(switch: Switch, scope: RunScope) -> bool:
    """True when `switch` may apply to some step of a run with `scope`: a step kind (agent,
    model, backend) is taken as possible, every other kind must match. The control plane
    sends these to a run's CLI, which decides per step with `matches`."""
    if not _in_team(switch, scope):
        return False
    return switch.scope_kind in STEP_KINDS or _dimension(switch, scope)


@dataclass(frozen=True)
class Verdict:
    """What a run must do now. `stale`: the switch state could not be confirmed."""

    action: Action
    reason: str = ""
    by: str = ""
    switch: str = ""
    stale: bool = False

    @property
    def pause_id(self) -> str:
        return UNCONFIRMED_PAUSE if self.stale else HOLD_PAUSE

    def describe(self) -> str:
        if self.stale:
            return f"the kill-switch state cannot be confirmed: {self.reason}"
        if self.action == "run":
            return "no switch applies"
        what = "kill switch" if self.action == "kill" else "pause switch"
        by = f" set by {self.by}" if self.by else ""
        return f"{what}{by}: {self.reason}"


RUN = Verdict("run")


def unconfirmed(reason: str) -> Verdict:
    """The verdict for switch state older than its expiry: hold, never continue."""
    return Verdict("pause", reason=reason, stale=True)


def decide(switches: Iterable[Switch], scope: RunScope) -> Verdict:
    """The verdict of the switches matching `scope`: the first kill, else the first pause
    (in the order given), else run."""
    found = [s for s in switches if matches(s, scope)]
    for mode in ("kill", "pause"):
        for s in found:
            if s.mode == mode:
                return Verdict(mode, reason=s.reason, by=s.by, switch=s.id)
    return RUN


class KillSwitch(Protocol):
    """The engine's port: asked before every step and every agent turn. It must be cheap
    (cached) and safe to call from a worker thread."""

    def check(self, scope: RunScope) -> Verdict: ...


class NoKillSwitch:
    """Standalone mode: there is no control plane, so nothing can stop a run."""

    def check(self, scope: RunScope) -> Verdict:
        return RUN


class RunKilled(Exception):
    """A kill switch matched while a step was running."""

    def __init__(self, verdict: Verdict) -> None:
        super().__init__(verdict.describe())
        self.verdict = verdict


def scope_problem(kind: str, value: str) -> str | None:
    """Why `value` cannot name a `kind` scope, or None. Values are expected normalised
    (`parse_scope` does that for the command line)."""
    if kind not in SCOPE_KINDS:
        return f"unknown scope kind {kind!r} (one of: {', '.join(SCOPE_KINDS)})"
    if kind == "tenant":
        return None if value == "" else "the tenant-wide switch takes no value"
    if not value:
        return f"a {kind} switch names the {kind} it stops"
    checks = {
        "team": (_TEAM, "a team slug (lower-case letters, digits and '-')"),
        "user": (_EMAIL, "an email address"),
        "pipeline": (_NAME, "a pipeline name"),
        "agent": (_AGENT, "an agent name, or name@version"),
        "model": (_OPAQUE, "a model id without spaces"),
        "backend": (_OPAQUE, "a backend name without spaces"),
        "repo": (_OPAQUE, "a repo as host/owner/name"),
    }
    pattern, what = checks[kind]
    if not pattern.fullmatch(value):
        return f"{value!r} is not {what}"
    if kind == "repo" and value.count("/") < 2:
        return f"{value!r} is not a repo as host/owner/name"
    if kind == "user" and value != value.lower():
        return f"{value!r}: an email is written in lower case"
    return None


def parse_scope(text: str) -> tuple[ScopeKind, str]:
    """`global`, or `<kind>:<value>`, normalised (an email lower-cased, a repo remote as
    `host/owner/name`); ValueError naming the problem."""
    text = text.strip()
    if text == GLOBAL:
        return "tenant", ""
    kind, colon, value = text.partition(":")
    kind, value = kind.strip().lower(), value.strip()
    if not colon or kind not in SCOPE_KINDS or kind == "tenant":
        kinds = ", ".join(k for k in SCOPE_KINDS if k != "tenant")
        raise ValueError(f"a scope is `{GLOBAL}` or <kind>:<value> with kind one of: {kinds}")
    if kind == "user":
        value = value.lower()
    elif kind == "repo":
        value = normalise_remote(value)
    if problem := scope_problem(kind, value):
        raise ValueError(f"scope {text!r}: {problem}")
    return kind, value  # type: ignore[return-value]


__all__ = [
    "GLOBAL",
    "HOLD_PAUSE",
    "MODES",
    "RUN",
    "SCOPE_KINDS",
    "STEP_KINDS",
    "UNCONFIRMED_PAUSE",
    "Action",
    "KillSwitch",
    "Mode",
    "NoKillSwitch",
    "RunKilled",
    "RunScope",
    "ScopeKind",
    "Switch",
    "Verdict",
    "decide",
    "matches",
    "parse_scope",
    "run_level",
    "scope_problem",
    "unconfirmed",
]
