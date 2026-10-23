"""Versions of agents and pipelines: team-by-team rollout, pins and deprecation (M2-15,
spec §12.5).

Agents, pipelines and skills are versioned in the policy repo. The current version of a
definition is its usual file; earlier versions a team may still get are kept beside it:

    <layer>/agents/versions/<name>/<version>.md
    <layer>/pipelines/versions/<name>/<version>.yaml

Org admins roll a new version out team by team in `policy/rollout.yaml`, and deprecate
old versions there; a team's leads may pin a version in `policy/teams/<team>.yaml`:

    rollout:                                  # policy/rollout.yaml
      agents:
        developer: {version: 1.3.0, previous: 1.2.0, teams: [payments]}
    deprecated:
      agents:
        developer:
          "1.1.0": {blocked_from: 2026-12-01, note: use 1.2.0 or later}

    pins:                                     # policy/teams/<team>.yaml
      pipelines: {feature-delivery: 1.0.0}

Which version a team gets (the resolution order):

1. the team's pin, when it pins this definition;
2. else, when the rollout lists the definition and the team is not in its `teams`, the
   rollout's `previous` version (no team at all is outside every rollout);
3. else the current file.

The chosen version's deprecation then applies: before `blocked_from` the run is warned,
from that day (UTC) it is refused. The same rules run in the CLI (discovery and
preflight) and in the control plane (run registration), so neither can be skipped.

Skills are deprecated by version too (their catalog entry carries it); rolling a skill
out per team needs two installed copies of one skill and is not offered yet.

These models live here, not in `agentic.policy`, because the policy model already imports
the definitions (never the other way round)."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from agentic.definitions.pipeline import PlainId

Kind = Literal["agent", "pipeline"]
#: The folder of each kind in a layer, and its files' suffix.
FOLDERS: dict[str, tuple[str, str]] = {
    "agent": ("agents", ".md"),
    "pipeline": ("pipelines", ".yaml"),
}
#: The archive folder inside `agents/` and `pipelines/`.
ARCHIVE = "versions"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RolloutEntry(_Strict):
    """A new `version` that only the listed `teams` get; every other team keeps
    `previous` (an archived version) until the entry is removed."""

    version: PlainId
    previous: PlainId
    teams: list[str] = []

    @model_validator(mode="after")
    def previous_differs(self) -> Self:
        if self.previous == self.version:
            raise ValueError("'previous' must differ from 'version'")
        return self


class Rollout(_Strict):
    agents: dict[str, RolloutEntry] = {}
    pipelines: dict[str, RolloutEntry] = {}

    def entry(self, kind: Kind, name: str) -> RolloutEntry | None:
        return (self.agents if kind == "agent" else self.pipelines).get(name)


class Deprecation(_Strict):
    """A deprecated version: runs are warned before `blocked_from`, refused from it."""

    blocked_from: date
    note: str = ""


class Deprecations(_Strict):
    """name -> version -> deprecation, per kind."""

    agents: dict[str, dict[str, Deprecation]] = {}
    pipelines: dict[str, dict[str, Deprecation]] = {}
    skills: dict[str, dict[str, Deprecation]] = {}

    def of(self, kind: Kind | Literal["skill"], name: str, version: str) -> Deprecation | None:
        table = {"agent": self.agents, "pipeline": self.pipelines, "skill": self.skills}[kind]
        return table.get(name, {}).get(version)


class Pins(_Strict):
    """name -> the version a team keeps, per kind."""

    agents: dict[str, PlainId] = {}
    pipelines: dict[str, PlainId] = {}

    def of(self, kind: Kind, name: str) -> str | None:
        return (self.agents if kind == "agent" else self.pipelines).get(name)


@dataclass(frozen=True)
class VersionChoice:
    """The version of one definition a team gets, and why."""

    kind: Kind
    name: str
    version: str
    current: str
    reason: Literal["current", "pinned", "held"]
    deprecation: Deprecation | None = None
    blocked: bool = False

    def describe(self) -> str:
        """One line for a person: which version, why, and any deprecation."""
        why = {
            "current": "the current version",
            "pinned": "pinned by your team",
            "held": f"your team is not in the rollout of {self.current}",
        }[self.reason]
        text = f"{self.kind} {self.name}@{self.version} ({why})"
        if self.deprecation is not None:
            when = self.deprecation.blocked_from.isoformat()
            text += (
                f": deprecated, refused since {when}"
                if self.blocked
                else f": deprecated, refused from {when}"
            )
            if self.deprecation.note:
                text += f" - {self.deprecation.note}"
        return text


def choose_version(
    kind: Kind,
    name: str,
    current: str,
    *,
    rollout: Rollout,
    pins: Pins,
    team: str | None,
) -> tuple[str, Literal["current", "pinned", "held"]]:
    """The version `team` gets of a definition whose current file is `current`."""
    if (pinned := pins.of(kind, name)) is not None:
        return pinned, "pinned"
    entry = rollout.entry(kind, name)
    if entry is not None and (team is None or team not in entry.teams):
        return entry.previous, "held"
    return current, "current"


def deprecation_state(
    kind: Kind | Literal["skill"],
    name: str,
    version: str,
    deprecated: Deprecations,
    today: date,
) -> tuple[Deprecation | None, bool]:
    """(the version's deprecation or None, whether it is refused on `today`)."""
    found = deprecated.of(kind, name, version)
    return found, found is not None and today >= found.blocked_from


def version_problem(
    kind: Kind,
    name: str,
    version: str,
    *,
    rollout: Rollout,
    pins: Pins,
    deprecated: Deprecations,
    team: str | None,
    today: date,
) -> str | None:
    """Why a run of `team` may not use `name@version`, or None: the check the control
    plane makes at run registration with its own clock, from the policy alone."""
    found, blocked = deprecation_state(kind, name, version, deprecated, today)
    if blocked and found is not None:
        note = f" - {found.note}" if found.note else ""
        return (
            f"{kind} {name}@{version} is deprecated and refused since "
            f"{found.blocked_from.isoformat()}{note}"
        )
    pinned = pins.of(kind, name)
    if pinned is not None and pinned != version:
        return f"team {team} pins {kind} {name} to {pinned}, not {version}"
    entry = rollout.entry(kind, name)
    if pinned is None and entry is not None and version == entry.version:
        if team is None or team not in entry.teams:
            who = f"team {team}" if team else "a run without a team"
            return (
                f"{kind} {name}@{version} is not rolled out to {who} yet (it keeps "
                f"{entry.previous})"
            )
    return None


def archive_path(layer_root: Path, kind: Kind, name: str, version: str) -> Path:
    folder, suffix = FOLDERS[kind]
    return layer_root / folder / ARCHIVE / name / f"{version}{suffix}"


def archived(layer_root: Path, kind: Kind, name: str) -> dict[str, Path]:
    """The archived versions of one definition in a layer: version -> file."""
    folder, suffix = FOLDERS[kind]
    where = layer_root / folder / ARCHIVE / name
    if not where.is_dir():
        return {}
    return {
        path.name.removesuffix(suffix): path
        for path in sorted(where.iterdir())
        if path.is_file() and path.name.endswith(suffix) and not path.name.startswith(".")
    }
