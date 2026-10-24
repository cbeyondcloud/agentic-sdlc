"""Which code intelligence a run's agents get (M1-35, M2-43; spec §10.1).

The repo chooses with `codeintel.adapter` in `.agentic/config.yaml`:

  * `code-review-graph` (default) - the symbol-level index of the run's tree, when the
    codeintel extra is installed;
  * `domain-graph` - the estate's domain graph (Zyroflo, through `sdlc_commons`),
    built beforehand into `codeintel.graph` (default `.agentic/local/graph`), answering
    alongside code-review-graph (`Combined`); `codeintel.component` names the repo's
    component in it (unset: the repo is the whole estate);
  * `none` - no code intelligence tools.

Core never imports commons: this module joins them. `codeintel_for` is the factory
`StepWiring` takes; `GraphIntel` is the boundary turning commons answers and errors
into core's `Candidates` and `CodeIntelError`. A missing graph or extra never
refuses a run: preflight warns (`codeintel_warning`) and agents get what is there.
"""

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from agentic.codeintel import Candidates, CodeIntel, CodeIntelError, Combined
from agentic.codeintel.export import MANIFEST
from agentic.repoconfig import CodeIntelConfig
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.workspace import REPO_CONFIG
from sdlc_commons.domain_graph import DomainGraph, DomainGraphError
from sdlc_commons.graph_codeintel import DomainGraphCodeIntel

Factory = Callable[[Path, Path], CodeIntel]  # (run tree, data folder) -> adapter
LOCAL_INDEX_REL = Path(".agentic") / "local" / "index"  # the developer's index (D39)
#: The published index export `agentic index import` keeps (M2-44): every run's index
#: starts from it and catches up with an incremental update.
INDEX_EXPORT_REL = Path(".agentic") / "local" / "index-export"


def seeded(repo: Path) -> bool:
    """Whether `agentic index import` has kept a published export for this repo."""
    return (repo / INDEX_EXPORT_REL / MANIFEST).is_file()


def codeintel_settings(config: Mapping[str, Any]) -> CodeIntelConfig:
    """The repo config's `codeintel` section, checked. Raises CliError (INVALID)."""
    section = config.get("codeintel")
    try:
        return CodeIntelConfig.model_validate({} if section is None else section)
    except ValueError as error:  # pydantic's ValidationError (core's model, core's dependency)
        problem = error.errors()[0] if hasattr(error, "errors") else {"loc": (), "msg": str(error)}
        where = ".".join(str(part) for part in ("codeintel", *problem["loc"]))
        raise CliError(
            f"{REPO_CONFIG}: {where}: {problem['msg']}",
            ExitCode.INVALID,
            hint="codeintel.adapter is code-review-graph, domain-graph or none",
        ) from error


def graph_dir(repo: Path, settings: CodeIntelConfig) -> Path:
    """The domain graph's folder: absolute, `~`, or relative to the repository."""
    folder = Path(os.path.expanduser(settings.graph_folder))
    return folder if folder.is_absolute() else repo / folder


def codeintel_warning(repo: Path, settings: CodeIntelConfig, *, crg_available: bool) -> str | None:
    """Why agents allowed `codeintel` get less than the repo asked for, or None."""
    if settings.adapter == "none":
        return None
    if settings.adapter == "domain-graph" and not _built(graph_dir(repo, settings)):
        fallback = "code-review-graph only" if crg_available else "without code intelligence tools"
        return (
            f"the domain graph is not built at {graph_dir(repo, settings)} (codeintel.graph): "
            f"it runs with {fallback} until the graph is built there"
        )
    if settings.adapter == "code-review-graph" and not crg_available:
        return (
            "the code intelligence extra is not installed (agentic-core[codeintel]): it runs "
            "without it and uses search instead"
        )
    return None


def codeintel_for(repo: Path, settings: CodeIntelConfig, *, crg_available: bool) -> Factory | None:
    """The adapter factory for a run's steps, or None (agents get no codeintel tools)."""
    if settings.adapter == "none":
        return None
    folder = graph_dir(repo, settings)
    graph = DomainGraph(folder) if settings.adapter == "domain-graph" and _built(folder) else None
    if graph is None and not crg_available:
        return None

    def make(tree: Path, data: Path) -> CodeIntel:
        adapters: list[CodeIntel] = []
        if crg_available:
            from agentic.codeintel.crg import CodeReviewGraph
            from agentic.codeintel.index import LocalIndex

            seed = repo / INDEX_EXPORT_REL  # a published export: seconds, not a full build
            index = LocalIndex(
                tree, data, home_dir=data / "home", seed=seed if seeded(repo) else None
            )
            adapters.append(CodeReviewGraph(index))
        if graph is not None:
            adapters.append(GraphIntel(DomainGraphCodeIntel(graph, tree, settings.component)))
        return Combined.of(*adapters)

    return make


class GraphIntel:
    """The commons domain-graph adapter seen through core's `CodeIntel` types."""

    def __init__(self, inner: DomainGraphCodeIntel) -> None:
        self.inner, self.name, self.supports = inner, inner.name, inner.supports

    def dependents(self, path: str) -> Candidates:
        return self._candidates(self.inner.dependents, path)

    def impact(self, diff: str) -> Candidates:
        return self._candidates(self.inner.impact, diff)

    def tests_for(self, path: str) -> Candidates:
        return self._candidates(self.inner.tests_for, path)

    def overview(self) -> str:
        try:
            return self.inner.overview()
        except DomainGraphError as error:
            raise CodeIntelError(str(error)) from error

    @staticmethod
    def _candidates(ask: Callable[[str], Any], arg: str) -> Candidates:
        try:
            found = ask(arg)
        except DomainGraphError as error:
            raise CodeIntelError(str(error)) from error
        return Candidates(tuple(found.files), source=found.source, note=found.note)


def _built(folder: Path) -> bool:
    return (folder / "graph.jsonl").is_file()
