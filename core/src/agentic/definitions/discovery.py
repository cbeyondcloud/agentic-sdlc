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
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agentic.definitions.agent import AgentDefinition, load_agent, valid_agent_name
from agentic.definitions.errors import DefinitionError
from agentic.definitions.pipeline import PipelineDefinition, load_pipeline

if TYPE_CHECKING:
    from agentic.handoffs.schema import HandoffCatalog

Layer = Literal["org", "team", "repo"]


def _builtin_handoffs() -> "HandoffCatalog":
    from agentic.handoffs.schema import BUILTIN_CATALOG  # imported late: handoffs imports us

    return BUILTIN_CATALOG


class UnknownDefinitionError(LookupError):
    """Asked for an agent or pipeline name that no layer defines."""


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

    @classmethod
    def discover(
        cls,
        org: Path | None,
        team: Path | None,
        repo: Path | None,
        allow_repo_pipelines: bool = False,
    ) -> "DefinitionSet":
        from agentic.handoffs.extensions import extend_catalog, load_extension

        found = cls()
        agent_files = found.agent_files
        extensions = []
        # Load farthest first, so nearer layers overwrite: org, then team, then repo.
        for layer, root in (("org", org), ("team", team), ("repo", repo)):
            if root is None:
                continue
            for path in _files(root / "agents", ".md"):
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
            # pipelines/overlays/: per-repo overlays (D33, loaded by M2-23), not pipelines
            for path in _files(root / "pipelines", ".yaml", folders=("overlays",)):
                if layer == "repo" and not allow_repo_pipelines:
                    found.warnings.append(
                        f"ignored repo pipeline {path.name}: pipelines are managed in the "
                        "policy repo (org policy allow_repo_pipelines is false)"
                    )
                    continue
                pipeline = load_pipeline(path)
                found.pipelines[pipeline.name] = (pipeline, layer)
                found.pipeline_files[pipeline.name] = path
        found.handoffs = extend_catalog(found.handoffs, extensions)
        for name, (agent, _) in found.agents.items():
            if problems := _unknown_kinds(agent, found.handoffs):
                raise DefinitionError(agent_files[name], problems)
        return found

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
