"""Finding agent and pipeline files across the three layers: org, team, repo.

Every layer folder has the same shape:

    <layer>/agents/<name>.md
    <layer>/pipelines/<name>.yaml

    org layer  = root of the org policy repo
    team layer = teams/<team>/ inside the policy repo
    repo layer = .agentic/ inside the target repo

When the same name exists in several layers, the nearest one wins as a whole
(repo > team > org). That only decides the *text* of a definition: its
restricted settings are still checked against org policy by the resolver.

Pipelines are admin-controlled (D33): repo-layer pipelines are ignored unless
org policy sets `allow_repo_pipelines: true`.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agentic.definitions.agent import AgentDefinition, load_agent
from agentic.definitions.errors import DefinitionError
from agentic.definitions.pipeline import PipelineDefinition, load_pipeline

Layer = Literal["org", "team", "repo"]


class UnknownDefinitionError(LookupError):
    """Asked for an agent or pipeline name that no layer defines."""


@dataclass
class DefinitionSet:
    """All agents and pipelines visible to one run, with the layer each came from."""

    agents: dict[str, tuple[AgentDefinition, Layer]] = field(default_factory=dict)
    pipelines: dict[str, tuple[PipelineDefinition, Layer]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def discover(
        cls,
        org: Path | None,
        team: Path | None,
        repo: Path | None,
        allow_repo_pipelines: bool = False,
    ) -> "DefinitionSet":
        found = cls()
        # Load farthest first, so nearer layers overwrite: org, then team, then repo.
        for layer, root in (("org", org), ("team", team), ("repo", repo)):
            if root is None:
                continue
            for path in _files(root / "agents", ".md"):
                agent = load_agent(path)
                found.agents[agent.name] = (agent, layer)
            for path in _files(root / "pipelines", ".yaml"):
                if layer == "repo" and not allow_repo_pipelines:
                    found.warnings.append(
                        f"ignored repo pipeline {path.name}: pipelines are managed in the "
                        "policy repo (org policy allow_repo_pipelines is false)"
                    )
                    continue
                pipeline = load_pipeline(path)
                found.pipelines[pipeline.name] = (pipeline, layer)
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

    @staticmethod
    def _lookup(table: dict, kind: str, name: str) -> tuple:
        if name not in table:
            known = ", ".join(sorted(table)) or "none"
            raise UnknownDefinitionError(f"no {kind} named '{name}' (known: {known})")
        return table[name]


def _files(folder: Path, suffix: str) -> list[Path]:
    """Every visible file in `folder`, which must all end in `suffix`.

    A wrong extension (reviewer.yml instead of reviewer.md, ship.yml instead of
    ship.yaml) or a stray file is an error: silently skipping it would hide a
    definition the author expects to exist.
    """
    if not folder.is_dir():
        return []
    entries = sorted(entry for entry in folder.iterdir() if not entry.name.startswith("."))
    unexpected = [entry for entry in entries if not (entry.is_file() and entry.suffix == suffix)]
    if unexpected:
        names = ", ".join(entry.name for entry in unexpected)
        raise DefinitionError(folder, [f"unexpected entries (only *{suffix} files): {names}"])
    return entries
