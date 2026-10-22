"""The `domain-graph` code-intelligence adapter (M2-43, spec §10.1, spike S5 decision 3).

Zyroflo's domain graph answers at the service level and across repositories: which
files in this and other repositories a change may reach through endpoints, topics,
tables, config and deploys. This adapter gives those answers the shape of core's
`CodeIntel` protocol (`agentic.codeintel`): `name`, `supports`, `dependents`,
`impact`, `tests_for`, `overview`. Commons does not import core (as `release.py`),
so answers are `GraphCandidates`, with the fields of core's `Candidates`; the CLI
turns them into core's types at the boundary.

How a run's tree meets the graph:

  * `component` set - the tree is that component (a repository of the estate):
    tree paths are component paths;
  * no `component` - the tree is the workspace root (a monorepo estate): a path
    belongs to the component with the longest matching path.

Files inside the tree, that exist there, are the answer's `files` (tree-relative
POSIX paths, best first). Files in other repositories and the services reached go in
`note`, so a cross-repo answer reaches the agent without a path outside the tree. A
graph path that is absolute, climbs out with `..` or names `.git` is never joined to
the tree. When the graph cites nothing in the asked file, Zyroflo's directory-scope
guess is not passed on as an impact answer: the answer is empty and says why.
"""

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

from sdlc_commons.domain_graph import DomainGraph

#: At most this many files of other repositories, and services, are named in a note.
MAX_NOTED_FILES = 20
MAX_NOTED_SERVICES = 10

_DRIVE = re.compile(r"^[A-Za-z]:")
_Located = tuple[str, str]  # (component, component-relative path)


@dataclass(frozen=True)
class GraphCandidates:
    """An answer: the fields of core's `agentic.codeintel.Candidates`."""

    files: tuple[str, ...]  # tree-relative POSIX paths, best first
    source: str
    note: str = ""


class DomainGraphCodeIntel:
    name = "domain-graph"
    supports = frozenset({"dependents", "impact", "tests_for", "overview"})

    def __init__(self, graph: DomainGraph, tree: Path, component: str | None = None) -> None:
        self.graph, self.tree, self.component = graph, Path(tree), component

    def dependents(self, path: str) -> GraphCandidates:
        located = self._locate(path)
        if located is None:
            return self._empty(f"{path} is in no component of the domain graph")
        return self._answer(self.graph.impact(*located), {located})

    def impact(self, diff: str) -> GraphCandidates:
        changed: dict[str, list[tuple[str, list[tuple[int, int]] | None]]] = {}
        for path, ranges in self.graph.parse_diff(diff).items():
            if (located := self._locate(path)) is not None:
                changed.setdefault(located[0], []).append((located[1], ranges))
        if not changed:
            return self._empty("the diff names no file in a component of the domain graph")
        results = [self.graph.impact_of_targets(c, sorted(t)) for c, t in sorted(changed.items())]
        exclude = {(c, p) for c, targets in changed.items() for p, _ in targets}
        return self._answer(_merged(results), exclude)

    def tests_for(self, path: str) -> GraphCandidates:
        located = self._locate(path)
        if located is None:
            return self._empty(f"{path} is in no component of the domain graph")
        result = self.graph.impact(*located)
        tests = self.graph.test_files()
        result = {**result, "files": [r for r in result["files"] if _key(r) in tests]}
        return self._answer(result, {located}, services=False)

    def overview(self) -> str:
        status = self.graph.status()
        nodes = ", ".join(f"{kind} {count}" for kind, count in status["nodes"].items())
        edges = ", ".join(f"{kind} {count}" for kind, count in status["edges"].items())
        lines = [
            f"Domain graph of workspace {status['workspace'] or '(unnamed)'} "
            f"(build {status['build_status'] or 'unknown'})",
            f"Components: {', '.join(status['components'])}",
            f"Nodes: {nodes}",
            f"Edges: {edges}",
        ]
        if self.component:
            lines.append(f"this tree is component {self.component}")
        return "\n".join(lines)

    # ── tree <-> graph ───────────────────────────────────────────────────────

    def _locate(self, path: str) -> _Located | None:
        rel = _clean(path)
        if rel is None:
            return None
        if self.component is not None:
            return self.component, rel
        best: tuple[int, str, str] | None = None  # (prefix length, component, inner path)
        for name, root in self.graph.components().items():
            prefix = _clean(root) if root not in ("", ".") else ""
            if prefix is None or not (prefix == "" or rel.startswith(prefix + "/")):
                continue
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), name, rel[len(prefix) :].lstrip("/"))
        return (best[1], best[2]) if best is not None else None

    def _root_of(self, component: str) -> str:
        root = self.graph.components().get(component, "")
        return "" if root in ("", ".") else root

    def _in_tree(self, component: str, path: str) -> str | None:
        """The tree-relative path of a graph file, or None when it is not in the tree."""
        rel = _clean(path)
        if rel is None:
            return None
        if self.component is not None:
            found = rel if component == self.component else None
        elif component in self.graph.components():
            root = self._root_of(component)
            found = f"{_clean(root)}/{rel}" if root else rel
        else:
            found = None
        return found if found and (self.tree / found).is_file() else None

    # ── answers ──────────────────────────────────────────────────────────────

    def _answer(self, result: dict, exclude: set[_Located], *, services: bool = True):
        fallback = result.get("seed", {}).get("fallback")
        if fallback:
            return self._empty(
                f"the domain graph cites no integration fact in {fallback.get('path', 'it')} "
                f"({fallback.get('basis', 'uncited')}), so it has no impact answer for it"
            )
        files: list[str] = []
        elsewhere: list[str] = []
        for row in result.get("files", []):
            if _key(row) in exclude:
                continue
            here = self._in_tree(row["component"], row["path"])
            if here is not None:
                files.append(here)
            elif _clean(row["path"]) is not None and self._elsewhere(row["component"]):
                elsewhere.append(f"{row['component']}/{row['path']} ({row['score']:g})")
        notes = []
        if elsewhere:
            listed = elsewhere[:MAX_NOTED_FILES]
            more = len(elsewhere) - len(listed)
            notes.append(
                "Other repositories (domain graph): "
                + ", ".join(listed)
                + (f", +{more} more" if more > 0 else "")
            )
        reached = [r for r in result.get("services", []) if r.get("hops", 0) > 0]
        if services and reached:
            named = [_service(r) for r in reached[:MAX_NOTED_SERVICES]]
            notes.append("Services reached: " + ", ".join(named))
        return GraphCandidates(tuple(dict.fromkeys(files)), self.name, "\n".join(notes))

    def _elsewhere(self, component: str) -> bool:
        """Whether a graph file of `component` lies outside this tree (another repo)."""
        return self.component is not None and component != self.component

    def _empty(self, note: str) -> GraphCandidates:
        return GraphCandidates((), self.name, note)


def _key(row: dict) -> _Located:
    return str(row["component"]), str(row["path"])


def _service(row: dict) -> str:
    name = str(row["id"]).removeprefix("svc:")
    service, _, owner = name.partition("@")
    label = service if owner == service else name
    hops = row.get("hops", 0)
    return f"{label} ({hops} hop{'' if hops == 1 else 's'})"


def _merged(results: list[dict]) -> dict:
    """Several components' impact answers as one: best score per file and per service."""
    files: dict[_Located, dict] = {}
    services: dict[str, dict] = {}
    for result in results:
        for row in result.get("files", []):
            if _key(row) not in files or row["score"] > files[_key(row)]["score"]:
                files[_key(row)] = row
        for row in result.get("services", []):
            if row["id"] not in services or row["score"] > services[row["id"]]["score"]:
                services[row["id"]] = row
    seeded = [r for r in results if not r.get("seed", {}).get("fallback")]
    best = sorted(files.values(), key=lambda r: (-r["score"], _key(r)))
    order = sorted(services.values(), key=lambda r: (-r["score"], r["id"]))
    seed = {} if seeded else results[0].get("seed", {})
    return {"seed": seed, "files": best, "services": order}


def _clean(path: str) -> str | None:
    """`path` as a relative POSIX path that stays where it is joined, or None."""
    text = str(path).strip().replace("\\", "/")
    if not text or "\0" in text or text.startswith("/") or _DRIVE.match(text):
        return None
    rel = posixpath.normpath(text)
    parts = rel.split("/")
    if rel == "." or ".." in parts or any(p.rstrip(". ").lower() == ".git" for p in parts):
        return None
    return rel
