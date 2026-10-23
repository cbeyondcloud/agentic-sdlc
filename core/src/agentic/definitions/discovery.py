"""Finding agent and pipeline files across the three layers: org, team, repo.

Every layer folder has the same shape:

    <layer>/agents/<name>.md
    <layer>/pipelines/<name>.yaml
    <layer>/handoffs/<kind>.yaml   (handoff template extensions, D37)

    org layer  = root of the org policy repo
    team layer = teams/<team>/ inside the policy repo
    repo layer = .agentic/ inside the target repo

When the same name exists in several layers, the nearest one wins as a whole
(repo > team > org). That only decides the *text* of a definition: its
restricted settings are still checked against org policy by the resolver.

Pipelines are admin-controlled (D33): repo-layer pipelines are ignored unless
org policy sets `allow_repo_pipelines: true`.

Handoff extensions do not replace each other: they stack, org then team then
repo, each only adding or tightening (D37). An agent that produces or consumes
a kind missing from the merged catalog is refused here.

With `rules` (the org policy's view for one team and repository, `PolicyRules`),
discovery then gives the run what that team in that repository really gets:

  * each org- or team-layer agent and pipeline at the version the team gets (its
    pin, else the previous version when it is outside the rollout, else the current
    file; M2-15, `agentic.definitions.versions`), an earlier version read from
    `<layer>/agents|pipelines/versions/<name>/<version>.*`;
  * the team's overlay for the repository applied to its pipelines (M2-23,
    `agentic.definitions.overlay`), never removing or weakening an org-required step.

`versions` records each choice worth telling a person (not the current version, or
deprecated), `overlays` each overlay applied with its notes; `pipeline_files` stays the
base file the pipeline was read from, `overlay_file` names the overlay.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agentic.definitions.agent import AgentDefinition, load_agent, valid_agent_name
from agentic.definitions.errors import DefinitionError
from agentic.definitions.overlay import OVERLAYS, apply_overlay, load_overlay, overlay_for
from agentic.definitions.pipeline import PipelineDefinition, load_pipeline
from agentic.definitions.versions import (
    ARCHIVE,
    Deprecations,
    Kind,
    Pins,
    Rollout,
    VersionChoice,
    archive_path,
    choose_version,
    deprecation_state,
)

if TYPE_CHECKING:
    from agentic.handoffs.schema import HandoffCatalog

Layer = Literal["org", "team", "repo"]


def _builtin_handoffs() -> "HandoffCatalog":
    from agentic.handoffs.schema import BUILTIN_CATALOG  # imported late: handoffs imports us

    return BUILTIN_CATALOG


class UnknownDefinitionError(LookupError):
    """Asked for an agent or pipeline name that no layer defines."""


def _today() -> date:
    return datetime.now(UTC).date()


@dataclass(frozen=True)
class PolicyRules:
    """What discovery needs from org policy for one team and repository
    (`OrgPolicy.rules`): who runs (`team`), where (`repo`, the origin's
    `host/owner/name`; None without one) and when (`today`, UTC), and the policy's
    rollout, deprecations, the team's pins and the org-required steps per pipeline."""

    team: str | None = None
    repo: str | None = None
    today: date = field(default_factory=_today)
    rollout: Rollout = Rollout()
    deprecated: Deprecations = Deprecations()
    pins: Pins = Pins()
    required_steps: Mapping[str, Sequence[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class AppliedOverlay:
    """The overlay file applied to a pipeline, and what it changed."""

    file: Path
    notes: tuple[str, ...]


@dataclass
class DefinitionSet:
    """All agents and pipelines visible to one run, with the layer each came from."""

    agents: dict[str, tuple[AgentDefinition, Layer]] = field(default_factory=dict)
    pipelines: dict[str, tuple[PipelineDefinition, Layer]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    handoffs: "HandoffCatalog" = field(default_factory=_builtin_handoffs)
    # the file each winning definition was read from (empty for a set built in code)
    agent_files: dict[str, Path] = field(default_factory=dict)
    pipeline_files: dict[str, Path] = field(default_factory=dict)
    # with PolicyRules (M2-15, M2-23): versions worth telling, overlays applied
    versions: dict[tuple[Kind, str], VersionChoice] = field(default_factory=dict)
    overlays: dict[str, AppliedOverlay] = field(default_factory=dict)
    rules: PolicyRules | None = None

    @classmethod
    def discover(
        cls,
        org: Path | None,
        team: Path | None,
        repo: Path | None,
        allow_repo_pipelines: bool = False,
        rules: PolicyRules | None = None,
    ) -> "DefinitionSet":
        from agentic.handoffs.extensions import extend_catalog, load_extension

        found = cls(rules=rules)
        agent_files = found.agent_files
        extensions = []
        # Load farthest first, so nearer layers overwrite: org, then team, then repo.
        for layer, root in (("org", org), ("team", team), ("repo", repo)):
            if root is None:
                continue
            # agents/versions/: earlier versions (M2-15), read only when a team gets one
            for path in _files(root / "agents", ".md", folders=(ARCHIVE,)):
                agent = load_agent(path)
                if path.stem != agent.name:  # else it silently replaces <name>.md (DF-03)
                    # offer the stem only when the name rule would accept it (DF-323)
                    other = f", or set name: {path.stem}" if valid_agent_name(path.stem) else ""
                    raise DefinitionError(
                        path,
                        [
                            f"{path.name} defines agent '{agent.name}': name the file "
                            f"{agent.name}.md{other}"
                        ],
                    )
                found.agents[agent.name] = (agent, layer)
                agent_files[agent.name] = path
            for path in _files(root / "handoffs", ".yaml"):
                extensions.append(load_extension(path, layer))
            # pipelines/overlays/: per-repo overlays (D33, M2-23), applied below;
            # pipelines/versions/: earlier versions (M2-15)
            folders = (OVERLAYS.name, ARCHIVE)
            for path in _files(root / "pipelines", ".yaml", folders=folders):
                if layer == "repo" and not allow_repo_pipelines:
                    found.warnings.append(
                        f"ignored repo pipeline {path.name}: pipelines are managed in the "
                        "policy repo (org policy allow_repo_pipelines is false)"
                    )
                    continue
                pipeline = load_pipeline(path)
                found.pipelines[pipeline.name] = (pipeline, layer)
                found.pipeline_files[pipeline.name] = path
        if rules is not None:
            layers: dict[str, Path | None] = {"org": org, "team": team}
            found._choose_versions(rules, layers)
            found._apply_overlay(rules, team)
        found.handoffs = extend_catalog(found.handoffs, extensions)
        for name, (agent, _) in found.agents.items():
            if problems := _unknown_kinds(agent, found.handoffs):
                raise DefinitionError(agent_files[name], problems)
        return found

    def _choose_versions(self, rules: PolicyRules, layers: Mapping[str, Path | None]) -> None:
        """Replace each org- or team-layer definition by the version the team gets."""
        for kind, table, files in (
            ("agent", self.agents, self.agent_files),
            ("pipeline", self.pipelines, self.pipeline_files),
        ):
            for name, (definition, layer) in list(table.items()):
                root = layers.get(layer)
                if root is None:  # a repo-layer definition is never versioned
                    continue
                current = definition.version
                version, reason = choose_version(
                    kind, name, current, rollout=rules.rollout, pins=rules.pins, team=rules.team
                )
                if version != current:
                    path = archive_path(root, kind, name, version)
                    table[name] = (load_version(kind, name, version, path), layer)
                    files[name] = path
                gone, blocked = deprecation_state(
                    kind, name, version, rules.deprecated, rules.today
                )
                if reason != "current" or gone is not None:
                    self.versions[(kind, name)] = VersionChoice(
                        kind, name, version, current, reason, gone, blocked
                    )

    def _apply_overlay(self, rules: PolicyRules, team: Path | None) -> None:
        """Apply the team's overlay for the repository to the pipelines it names."""
        path = overlay_for(team, rules.repo)
        if path is None:
            return
        overlay = load_overlay(path)
        for name, change in overlay.pipelines.items():
            if name not in self.pipelines:
                known = ", ".join(sorted(self.pipelines)) or "none"
                raise DefinitionError(
                    path, [f"pipelines.{name}: no pipeline named {name!r} (known: {known})"]
                )
            base, layer = self.pipelines[name]
            if layer == "repo":
                self.warnings.append(
                    f"overlay {path.name} not applied to {name}: it is a repo pipeline, and "
                    "overlays change policy-repo pipelines only"
                )
                continue
            result = apply_overlay(base, change, rules.required_steps.get(name, ()), source=path)
            self.pipelines[name] = (result.pipeline, layer)
            self.overlays[name] = AppliedOverlay(path, result.notes)

    def overlay_file(self, name: str) -> Path | None:
        """The overlay applied to pipeline `name`, or None."""
        applied = self.overlays.get(name)
        return applied.file if applied is not None else None

    def agent(self, name: str) -> AgentDefinition:
        return self._lookup(self.agents, "agent", name)[0]

    def agent_source(self, name: str) -> Layer:
        return self._lookup(self.agents, "agent", name)[1]

    def pipeline(self, name: str) -> PipelineDefinition:
        return self._lookup(self.pipelines, "pipeline", name)[0]

    def pipeline_source(self, name: str) -> Layer:
        return self._lookup(self.pipelines, "pipeline", name)[1]

    def pipeline_names(self) -> list[str]:
        return sorted(self.pipelines)

    def agent_file(self, name: str) -> Path | None:
        """The file agent `name` was read from, or None (a set built in code)."""
        return self.agent_files.get(name)

    def pipeline_file(self, name: str) -> Path | None:
        return self.pipeline_files.get(name)

    @staticmethod
    def _lookup(table: dict, kind: str, name: str) -> tuple:
        if name not in table:
            known = ", ".join(sorted(table)) or "none"
            raise UnknownDefinitionError(f"no {kind} named '{name}' (known: {known})")
        return table[name]


def load_version(
    kind: Kind, name: str, version: str, path: Path
) -> AgentDefinition | PipelineDefinition:
    """An archived version; its file must exist and say the name and version of its path."""
    if not path.is_file():
        raise DefinitionError(
            path,
            [
                f"{kind} {name}@{version} is the version your team gets (pin or rollout), "
                f"but the policy repo has no {path.parent.parent.name}/{name}/{path.name} "
                f"under {path.parent.parent.parent.name}/ (expected {path.as_posix()})"
            ],
        )
    definition = load_agent(path) if kind == "agent" else load_pipeline(path)
    if (definition.name, definition.version) != (name, version):
        raise DefinitionError(
            path,
            [
                f"this archived file says {kind} {definition.name} version "
                f"{definition.version}; its path says {name} version {version}"
            ],
        )
    return definition


def _unknown_kinds(agent: AgentDefinition, catalog: "HandoffCatalog") -> list[str]:
    known = ", ".join(catalog.kinds())
    wanted = [("produces", agent.produces)] + [("consumes", kind) for kind in agent.consumes or ()]
    return [
        f"{field_name}: unknown handoff kind '{kind}' (known: {known})"
        for field_name, kind in wanted
        if kind is not None and kind not in catalog.schemas
    ]


def _files(folder: Path, suffix: str, folders: tuple[str, ...] = ()) -> list[Path]:
    """Every visible file in `folder`, which must all end in `suffix`; the subfolders
    named in `folders` are someone else's and skipped.

    A wrong extension (reviewer.yml instead of reviewer.md, ship.yml instead of
    ship.yaml) or a stray file is an error: silently skipping it would hide a
    definition the author expects to exist.
    """
    if not folder.is_dir():
        return []
    entries = sorted(
        entry
        for entry in folder.iterdir()
        if not entry.name.startswith(".") and not (entry.name in folders and entry.is_dir())
    )
    unexpected = [entry for entry in entries if not (entry.is_file() and entry.suffix == suffix)]
    if unexpected:
        names = ", ".join(entry.name for entry in unexpected)
        raise DefinitionError(folder, [f"unexpected entries (only *{suffix} files): {names}"])
    return entries
