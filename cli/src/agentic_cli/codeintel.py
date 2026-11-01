"""Which code intelligence a run's agents get (M1-35, M2-43; spec §10.1).

The repo chooses with `codeintel.adapter` in `.agentic/config.yaml`:

  * `auto` (default) - the symbol adapter routed by the repo's language (M3-27): for a
    Maven or Gradle repo codebase-memory-mcp when the org pins it and it is installed
    here, otherwise code-review-graph (when the codeintel extra is installed);
  * `code-review-graph` or `codebase-memory` - that symbol adapter, whatever the language;
  * `domain-graph` - routed like `auto`, plus the estate's domain graph (Zyroflo, through
    `sdlc_commons`), built beforehand into `codeintel.graph` (default
    `.agentic/local/graph`), answering alongside (`Combined`); `codeintel.component`
    names the repo's component in it (unset: the repo is the whole estate);
  * `none` - no code intelligence tools.

A Maven or Gradle repo also gets the JVM test conventions (`JvmConventions`, pure Python)
first, whatever the symbol adapter: Java is never left without `tests_for`. The native
codebase-memory-mcp binary comes only from the verified tool cache (`tool-pins.yaml`,
installed by `agentic init`); it is never downloaded here, and it is looked up only for
a repo routed to it (`backend_for`). It replaces code-review-graph: when it cannot run,
the JVM test conventions answer alone (`Combined`) and preflight says why
(`codeintel_warning`). A build this machine can never run (none for its platform, or an
unsigned Windows build) is permanent: under `auto` that is information, not a warning.

Core never imports commons: this module joins them. `codeintel_for` is the factory
`StepWiring` takes; `GraphIntel` is the boundary turning commons answers and errors
into core's `Candidates` and `CodeIntelError`. A missing graph or extra never
refuses a run: preflight warns (`codeintel_warning`) and agents get what is there.
"""

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from agentic.codeintel import Candidates, CodeIntel, CodeIntelError, Combined
from agentic.codeintel.export import MANIFEST, installed_tool_version
from agentic.codeintel.jvm import JvmConventions, is_jvm_repo
from agentic.repoconfig import CodeIntelConfig
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.workspace import REPO_CONFIG
from sdlc_commons.domain_graph import DomainGraph, DomainGraphError
from sdlc_commons.graph_codeintel import DomainGraphCodeIntel
from sdlc_commons.tool_fetch import windows_refusal

Factory = Callable[[Path, Path], CodeIntel]  # (run tree, data folder) -> adapter
LOCAL_INDEX_REL = Path(".agentic") / "local" / "index"  # the developer's index (D39)
#: The published index export `agentic index import` keeps (M2-44): every run's index
#: starts from it and catches up with an incremental update.
INDEX_EXPORT_REL = Path(".agentic") / "local" / "index-export"
#: The optional JVM backend's name in org policy's tool-pins.yaml (M3-27).
BACKEND_TOOL = "codebase-memory-mcp"
CONVENTIONS = "the Maven/Gradle test conventions"
Route = Literal["code-review-graph", "codebase-memory"]


@dataclass(frozen=True)
class Backend:
    """Where the optional JVM backend stands on this machine."""

    path: Path | None  # the verified binary, or None
    pinned: bool  # whether org policy pins it at all
    why: str = ""  # why there is no path
    permanent: bool = False  # no build this machine may ever run (platform, Windows signing)


class Locator(Protocol):
    pins: Mapping[str, Any]
    platform: str

    def locate(self, name: str) -> tuple[Path | None, str]: ...


NO_BACKEND = Backend(None, pinned=False, why=f"org policy pins no {BACKEND_TOOL}")


def locate_backend(locator: Locator) -> Backend:
    """The backend from the verified tool cache (`sdlc_commons.review_prepass.ToolLocator`)."""
    path, why = locator.locate(BACKEND_TOOL)
    pin = locator.pins.get(BACKEND_TOOL)
    artefact = pin.assets.get(locator.platform) if pin is not None else None
    permanent = pin is not None and (
        artefact is None or windows_refusal(locator.platform, artefact) is not None
    )
    return Backend(path, pinned=pin is not None, why=why, permanent=path is None and permanent)


def backend_for(repo: Path, settings: CodeIntelConfig, locate: Callable[[], Backend]) -> Backend:
    """The backend for a repo routed to it, else NO_BACKEND without looking (locating
    hashes the binary). A lookup that fails is no backend, with the reason."""
    if symbol_route(repo, settings) != "codebase-memory":
        return NO_BACKEND
    try:
        return locate()
    except Exception as error:  # noqa: BLE001 - code intelligence never stops a run
        return Backend(None, pinned=False, why=f"{type(error).__name__}: {error}")


def symbol_route(repo: Path, settings: CodeIntelConfig) -> Route | None:
    """The symbol adapter the repo asks for: by language unless set; None for `none`."""
    if settings.adapter == "none":
        return None
    if settings.adapter in ("code-review-graph", "codebase-memory"):
        return settings.adapter
    return "codebase-memory" if is_jvm_repo(repo) else "code-review-graph"


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
            hint=_hint(problem),
        ) from error


#: What each codeintel setting is, for the hint under an invalid one (DF-464).
_HINTS = {
    "adapter": (
        "codeintel.adapter is auto, code-review-graph, codebase-memory, domain-graph or none"
    ),
    "graph": "codeintel.graph is the domain graph's folder: repo-relative, absolute or ~/...",
    "component": "codeintel.component names this repo's component in the domain graph",
}


def _hint(problem: Mapping[str, Any]) -> str:
    """The hint for one pydantic error of the codeintel section."""
    loc = tuple(problem.get("loc") or ())
    if not loc:  # the section itself is not a mapping (`codeintel: none`)
        return "codeintel is a mapping of settings, for example `codeintel: {adapter: none}`"
    if problem.get("type") == "extra_forbidden" or loc[0] not in _HINTS:
        return f"codeintel takes only the settings {', '.join(_HINTS)}"
    return _HINTS[str(loc[0])]


def graph_dir(repo: Path, settings: CodeIntelConfig) -> Path:
    """The domain graph's folder: absolute, `~`, or relative to the repository."""
    folder = Path(os.path.expanduser(settings.graph_folder))
    return folder if folder.is_absolute() else repo / folder


def codeintel_warning(
    repo: Path,
    settings: CodeIntelConfig,
    *,
    crg_available: bool,
    backend: Backend = NO_BACKEND,
) -> str | None:
    """Why agents allowed `codeintel` get less than the repo asked for, or None."""
    if settings.adapter == "none":
        return None
    if settings.adapter == "domain-graph" and not _built(graph_dir(repo, settings)):
        fallback = "code-review-graph only" if crg_available else "without code intelligence tools"
        return (
            f"the domain graph is not built at {graph_dir(repo, settings)} (codeintel.graph): "
            f"it runs with {fallback} until the graph is built there"
        )
    if settings.adapter == "domain-graph":
        try:  # DF-467: a damaged graph.jsonl would fail every domain-graph answer
            DomainGraph(graph_dir(repo, settings)).status()
        except DomainGraphError as error:
            return (
                f"the domain graph at {graph_dir(repo, settings)} cannot be read ({error}): "
                "rebuild it; its answers fail until then"
            )
    route = symbol_route(repo, settings)
    if route == "codebase-memory" and backend.path is None:
        # under `auto` an org that never pinned it chose the conventions, and a build this
        # machine can never run is no news on every run: nothing to say
        if settings.adapter == "codebase-memory" or (backend.pinned and not backend.permanent):
            return _backend_missing(repo, backend, crg_available=crg_available)
    elif route == "codebase-memory":
        return None  # the backend answers; code-review-graph is not used
    missing_extra = not crg_available and settings.adapter != "domain-graph"  # built: it answers
    if missing_extra and is_jvm_repo(repo):
        return (
            "the code intelligence extra is not installed (agentic-core[codeintel]): it runs "
            f"with {CONVENTIONS} only"
        )
    if missing_extra:
        return (
            "the code intelligence extra is not installed (agentic-core[codeintel]): it runs "
            "without it and uses search instead"
        )
    if crg_available and seeded(repo):
        made, installed = _seed_tool_version(repo), installed_tool_version()
        if made != installed:  # every run's index would be refused the seed and built anew
            return (
                f"the imported index export ({INDEX_EXPORT_REL.as_posix()}) was made with "
                f"code-review-graph {made} and this machine has {installed}: runs build the "
                "index from scratch until `agentic index import` brings an export of this "
                "version"
            )
    return None


def _fallback(repo: Path, *, crg_available: bool) -> str:
    if not is_jvm_repo(repo):
        return "code-review-graph" if crg_available else "no code intelligence tools"
    return f"code-review-graph and {CONVENTIONS}" if crg_available else f"{CONVENTIONS} only"


def _backend_missing(repo: Path, backend: Backend, *, crg_available: bool) -> str:
    return (
        f"{BACKEND_TOOL} is not available ({backend.why}): code intelligence runs on "
        f"{_fallback(repo, crg_available=crg_available)}"
    )


def codeintel_status(
    repo: Path,
    settings: CodeIntelConfig,
    *,
    crg_available: bool,
    backend: Backend = NO_BACKEND,
) -> tuple[Literal["ok", "warn", "info"], str, str]:
    """(status, what answers, how to fix) for `agentic doctor` (M3-27)."""
    if settings.adapter == "none":
        return "info", "off (codeintel.adapter: none)", ""
    warning = codeintel_warning(repo, settings, crg_available=crg_available, backend=backend)
    if warning is not None:
        fix = (
            f"ask the platform team to pin an approved {BACKEND_TOOL} build in tool-pins.yaml, "
            "then run `agentic init`; or set codeintel.adapter: code-review-graph"
            if BACKEND_TOOL in warning
            else "install agentic-core[codeintel], or build the domain graph, as the warning says"
        )
        return "warn", warning, fix
    route = symbol_route(repo, settings)
    if route == "codebase-memory" and backend.path is not None:
        return "ok", f"{BACKEND_TOOL} {backend.path} with {CONVENTIONS}", ""
    detail = _fallback(repo, crg_available=crg_available)
    if route == "codebase-memory" and backend.permanent:
        return "info", f"{detail} ({BACKEND_TOOL}: {backend.why})", ""
    if is_jvm_repo(repo):  # the org has not pinned the backend: the conventions answer
        return "info", f"{detail} ({BACKEND_TOOL} not pinned by org policy)", ""
    return "ok", detail, ""


def _seed_tool_version(repo: Path) -> str | None:
    try:
        manifest = json.loads((repo / INDEX_EXPORT_REL / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return manifest.get("tool_version") if isinstance(manifest, dict) else None


def codeintel_for(
    repo: Path,
    settings: CodeIntelConfig,
    *,
    crg_available: bool,
    backend: Backend = NO_BACKEND,
) -> Factory | None:
    """The adapter factory for a run's steps, or None (agents get no codeintel tools)."""
    route = symbol_route(repo, settings)
    if route is None:
        return None
    folder = graph_dir(repo, settings)
    graph = DomainGraph(folder) if settings.adapter == "domain-graph" and _built(folder) else None
    jvm = is_jvm_repo(repo)
    binary = backend.path if route == "codebase-memory" else None
    if graph is None and not crg_available and not jvm and binary is None:
        return None

    def make(tree: Path, data: Path) -> CodeIntel:
        adapters: list[CodeIntel] = []
        if jvm:  # small, name-ranked answers first (M3-27)
            adapters.append(JvmConventions(tree))
        if binary is not None:
            from agentic.codeintel.cbm import CodebaseMemory

            adapters.append(CodebaseMemory([str(binary)], tree, data / "codebase-memory"))
        elif crg_available:
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
