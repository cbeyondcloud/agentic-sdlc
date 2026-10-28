"""Domain-graph access over Zyroflo's `tools.graph` (M2-28; D34: reuse, never rewrite).

Zyroflo's domain graph covers the service level across repositories: services,
endpoints, HTTP calls, Kafka topics, tables and migrations, compose/Helm deploys,
config keys and tests, each edge citing the file and line it was read from. This
module puts it behind the framework's names, unchanged:

  * build_graph()  - `tools.graph.build.build`: an estate's workspace file -> a graph
                     folder (`graph.jsonl`, `build_report.json`, ...);
  * DomainGraph    - one built graph, read through Zyroflo's `GraphService` (the MCP
                     server's tools as methods, reloaded whenever the graph on disk
                     changes): status, components, impact of a file or of a diff's
                     files (`impact` / `impact_diff`, with the graph's traversal
                     rules), test files, context packs;
  * mcp_server()   - Zyroflo's MCP server over a graph (fastmcp, which the codeintel
                     extra already brings);
  * open_store()   - the graph loaded into a store (`load_into`): SQLite in memory by
                     default; neo4j only when the caller has a server (optional).

Every Zyroflo refusal becomes `DomainGraphError`, in Zyroflo's own words: a damaged
graph is refused by line, never read as a smaller one.

**Zyroflo gap Z7.** `build` imports Zyroflo's installer (`sdlc-init.py`) from the
checkout root, which the wheel does not ship. Until the Zyroflo PR lands,
`build_graph` uses Zyroflo's own loader when it works, else the installer of a
Zyroflo checkout at the pinned commit (`harness=`, by default the one uv keeps in
its git cache after installing the pin), else refuses naming Z7. Impact, the MCP
server and the stores need no installer.
"""

import importlib.metadata
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import tools.graph
from tools.graph import build as zyroflo_build
from tools.graph import mcp_server as zyroflo_mcp
from tools.graph.core.store.load import load_into
from tools.graph.impact import GraphError, impact_diff, known_components, parse_diff
from tools.graph.rules import RulesError, discover_rules

#: The tools Zyroflo's MCP server serves.
MCP_TOOLS: tuple[str, ...] = tuple(zyroflo_mcp.TOOL_NAMES)
#: The store backends Zyroflo implements.
STORES = ("sqlite", "neo4j")
#: Zyroflo's installer, which the graph build reads workspaces and components with.
INSTALLER = "sdlc-init.py"

_DISTRIBUTION = "sdlc-harness"
_INSTALLER_LOCK = threading.RLock()
_READ_ERRORS = (GraphError, RulesError, ValueError, OSError)


class DomainGraphError(Exception):
    """The graph could not be built, read or served; the message says why."""


# ── build ────────────────────────────────────────────────────────────────────


def build_graph(
    workspace: Path,
    out: Path,
    *,
    harness: Path | None = None,
    components: list[str] | None = None,
) -> dict:
    """Build `workspace` (Zyroflo's workspace TOML) into `out`; returns the build report.

    `harness` is a Zyroflo checkout at the pinned commit, used only for its installer
    while the wheel lacks it (Z7). Raises DomainGraphError.
    """
    with _installer(harness):
        try:
            return zyroflo_build.build(Path(workspace), Path(out), components_filter=components)
        except (zyroflo_build.BuildError, OSError) as error:
            raise DomainGraphError(str(error)) from error


def installer_available(harness: Path | None = None) -> bool:
    """Whether `build_graph` can find Zyroflo's installer on this machine."""
    if _packaged_installer():
        return True
    source = harness if harness is not None else zyroflo_checkout()
    return source is not None and (Path(source) / INSTALLER).is_file()


def zyroflo_checkout() -> Path | None:
    """The source checkout of the pinned Zyroflo commit that uv keeps in its git cache
    (`git-v0/checkouts/<repo>/<short commit>`), or None. A stopgap until Z7."""
    commit, cache = _pinned_commit(), _uv_cache()
    if not commit or cache is None:
        return None
    for found in sorted((cache / "git-v0" / "checkouts").glob(f"*/{commit[:7]}")):
        if (found / INSTALLER).is_file():
            return found
    return None


def fixture_estate(name: str) -> Path:
    """One of the fixture estates Zyroflo ships with answer keys (`orderflow`, `pantry`)."""
    folder = Path(tools.graph.__file__).parent / "tests" / "fixtures" / name
    if not (folder / "workspace.toml").is_file():
        raise DomainGraphError(f"Zyroflo ships no fixture estate called {name!r}")
    return folder


@contextmanager
def _installer(harness: Path | None) -> Iterator[None]:
    if _packaged_installer():
        yield
        return
    source = Path(harness) if harness is not None else zyroflo_checkout()
    installer = source / INSTALLER if source is not None else None
    if installer is None or not installer.is_file():
        where = f"{installer} does not exist" if installer is not None else "none was found"
        raise DomainGraphError(
            f"Zyroflo's installed package cannot build a graph: its build reads {INSTALLER} "
            "from a checkout, which the wheel does not ship (Zyroflo PR Z7). Name a Zyroflo "
            f"checkout at the pinned commit ({where}), or build with Zyroflo's own CLI from "
            "a checkout and point the adapter at the output folder"
        )
    with _INSTALLER_LOCK:
        found = zyroflo_build.load_installer
        zyroflo_build.load_installer = lambda: zyroflo_build._load_module(installer, "sdlc_init")
        try:
            yield
        finally:
            zyroflo_build.load_installer = found


def _packaged_installer() -> bool:
    try:
        zyroflo_build.load_installer()
    except OSError:
        return False
    return True


def _pinned_commit() -> str | None:
    try:
        raw = importlib.metadata.distribution(_DISTRIBUTION).read_text("direct_url.json")
        commit = json.loads(raw or "{}").get("vcs_info", {}).get("commit_id")
    except (importlib.metadata.PackageNotFoundError, ValueError, AttributeError):
        return None
    return commit if isinstance(commit, str) and len(commit) >= 7 else None


def _uv_cache() -> Path | None:
    if named := os.environ.get("UV_CACHE_DIR"):
        return Path(named)
    uv = shutil.which("uv")
    if uv is None:
        return None
    try:
        done = subprocess.run(
            [uv, "cache", "dir"], capture_output=True, text=True, encoding="utf-8", timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = done.stdout.strip()
    return Path(text) if done.returncode == 0 and text else None


# ── one built graph ──────────────────────────────────────────────────────────


class DomainGraph:
    """A built graph folder, read through Zyroflo's `GraphService`."""

    def __init__(self, graph_dir: Path, *, workspace: Path | None = None) -> None:
        self.graph_dir = Path(graph_dir)
        self._service = zyroflo_mcp.GraphService(self.graph_dir, workspace=workspace)

    def status(self) -> dict:
        """Zyroflo's `graph_status`: build provenance, components, node and edge counts."""
        return _ask(self._service.status)

    def data(self) -> tuple[list[dict], list[dict]]:
        """The graph's (nodes, edges) as Zyroflo's one reader returns them."""
        return _ask(self._service.data)

    def components(self) -> dict[str, str]:
        """Each declared component and its path relative to the workspace root."""
        nodes, _ = self.data()
        return {
            node["id"].split(":", 1)[1]: str((node.get("props") or {}).get("path", ""))
            for node in nodes
            if node.get("type") == "repo"
        }

    def impact(self, component: str, path: str, lines: str | None = None) -> dict:
        """The blast radius of one component-relative file (`lines`: e.g. "12-40,55")."""
        return _ask(self._service.impact_seed, component, path, lines)

    def impact_of_targets(
        self, component: str, targets: Iterable[tuple[str, list[tuple[int, int]] | None]]
    ) -> dict:
        """One component's changed files -> one blast radius (Zyroflo's `impact_diff`).
        `targets` are (component-relative path, hunk ranges or None)."""
        targets = list(targets)
        if not targets:
            raise DomainGraphError("no changed files to seed an impact from")
        nodes, edges = self.data()
        rules, _ = _ask(discover_rules, self.graph_dir)
        result = impact_diff(nodes, edges, component, targets, rules=rules)
        if result is None:
            known = ", ".join(sorted(known_components(nodes)))
            raise DomainGraphError(
                f"component {component!r} is not in this graph; components: {known}"
            )
        return result

    @staticmethod
    def parse_diff(text: str) -> dict[str, list[tuple[int, int]] | None]:
        """A unified diff's files and new-side hunk ranges (Zyroflo's `parse_diff`)."""
        return parse_diff(text)

    def test_files(self) -> set[tuple[str, str]]:
        """(component, component-relative path) of every test file the graph knows."""
        nodes, _ = self.data()
        found = set()
        for node in nodes:
            props = node.get("props") or {}
            if node.get("type") == "test" and props.get("component") and props.get("path"):
                found.add((str(props["component"]), str(props["path"])))
        return found

    def context_pack(self, component: str, **options: Any) -> dict:
        """Zyroflo's budgeted context pack (`path=` or `diff_text=`, `budget=`, `format=`)."""
        return _ask(self._service.context_pack, component, **options)


def _ask(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except _READ_ERRORS as error:
        raise DomainGraphError(str(error)) from error


# ── serving and storing ──────────────────────────────────────────────────────


def mcp_server(graph_dir: Path, *, workspace: Path | None = None):
    """Zyroflo's MCP server (its eight tools) over a built graph. Raises DomainGraphError."""
    if zyroflo_mcp.FastMCP is None:
        raise DomainGraphError(
            "fastmcp is not installed, and Zyroflo's MCP server needs it: install the "
            "codeintel extra (agentic-core[codeintel]), which brings it"
        )
    if problem := zyroflo_mcp.graph_problem(Path(graph_dir)):
        raise DomainGraphError(problem)
    return zyroflo_mcp.build_server(zyroflo_mcp.GraphService(Path(graph_dir), workspace=workspace))


def open_store(graph_dir: Path, backend: str = "sqlite", **options: Any):
    """The graph loaded into a Zyroflo store: (open store, what went in). SQLite is in
    memory unless `path=` names a file; neo4j reads `uri=`/`user=`/`password=` or
    Zyroflo's NEO4J_* variables and needs a running server. Raises DomainGraphError."""
    if backend not in STORES:
        raise DomainGraphError(f"unknown store {backend!r}: Zyroflo has {', '.join(STORES)}")
    errors: tuple[type[BaseException], ...] = (GraphError, ValueError, OSError)
    if backend == "sqlite":
        options.setdefault("path", ":memory:")
    else:
        from neo4j import exceptions as neo4j_errors

        errors += (neo4j_errors.Neo4jError, neo4j_errors.DriverError)
    try:
        return load_into(Path(graph_dir), backend, **options)
    except errors as error:
        raise DomainGraphError(f"the graph could not be loaded into {backend}: {error}") from error
