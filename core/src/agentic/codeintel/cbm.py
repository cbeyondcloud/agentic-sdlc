"""codebase-memory-mcp as an optional symbol adapter for JVM repos (M3-27; S5 decision 3).

Spike S5 found it the most accurate on Java, but it is a large native binary. It runs
only where the org approved it: a build pinned by version and sha256 in org policy
(`tool-pins.yaml`), installed by the verified tool fetcher (M1-51). The caller passes
the verified binary's command; nothing here downloads or looks one up.

Every answer is one-shot `cli` commands (JSON arguments on stdin; `--json` gives the
MCP envelope). It never starts an MCP session, because the update check its
SECURITY.md describes starts on MCP `initialize`. Each answer first re-indexes the tree
(incremental after the first time; `index_timeout_s`), then queries it
(`QUERY_TIMEOUT_S` each). The tool's home and caches stay inside the run's data
folder, and it gets a small allow-listed environment: no credentials and no proxy
settings.

Every call runs inside the OS sandbox (`SrtSandbox`, D38) with no allowed host, so no
update check or other request leaves the machine; it writes only its data folder, and
policy's `deny_read` paths are hidden from it (DF-921, owner decision 2026-11-07). There
is no unsandboxed fallback: without `srt` the tool cannot run, and the caller offers
this adapter only where the sandbox is ready (`agentic_cli.codeintel.locate_backend`).

  * `tests_for`: the files with a TESTS edge into the seed;
  * `dependents`: files with a code edge into the seed, ranked by edge count, then the
    files with an edge into those (S5's best variant, "2-hop"). Similarity, containment
    and git co-change edges are not dependencies;
  * `impact`: the diff's files, then the dependents of the first `MAX_IMPACT_SEEDS`.

A tool that fails, or answers something unreadable, raises `CodeIntelError`, and
`Combined` then answers from the other adapters. A tool that cannot start or times out
is not tried again by this adapter: every later answer fails at once.
"""

import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from agentic.backends.processes import run_contained
from agentic.codeintel.crg import diff_files
from agentic.codeintel.interface import Candidates, CodeIntelError
from agentic.codeintel.tools import tree_path
from agentic.sandbox.rules import SandboxRules
from agentic.sandbox.srt import SrtSandbox

#: Edge types that are structure, similarity or git history, not dependencies (S5).
NOISE = frozenset(
    {
        "DEFINES",
        "DEFINES_METHOD",
        "CONTAINS_FILE",
        "CONTAINS_FOLDER",
        "CONTAINS_PACKAGE",
        "SEMANTICALLY_RELATED",
        "SIMILAR_TO",
        "FILE_CHANGES_WITH",
        "HAS_BRANCH",
        "MEMBER_OF",
    }
)
MAX_ROWS = 99_998
MAX_SECOND_HOP = 50  # direct dependents whose own dependents are asked for
MAX_IMPACT_SEEDS = 50  # changed files whose dependents `impact` adds
QUERY_TIMEOUT_S = 120.0
#: The only variables the tool gets from this process (the OS needs them to start it).
_PASSED = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")
#: What `srt` itself also gets (a Node program: its own home); the tool's are set inside.
_SRT_PASSED = (*_PASSED, "HOME", "USERPROFILE")


def _quoted(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


class CodebaseMemory:
    name = "codebase-memory-mcp"
    supports = frozenset({"dependents", "impact", "tests_for", "overview"})

    def __init__(
        self,
        command: Sequence[str],
        tree: Path,
        data: Path,
        *,
        srt: Sequence[str] = ("srt",),
        deny_read: Sequence[Path] = (),  # policy's `sandbox.deny_read`, absolute
        index_timeout_s: float = 600,
    ) -> None:
        self.command, self.tree, self.data = list(command), Path(tree), Path(data)
        self.index_timeout_s = index_timeout_s
        self._broken: str | None = None  # why the tool cannot run; set once
        folder = self.data.resolve()
        folder.mkdir(parents=True, exist_ok=True)
        rules = SandboxRules(write=(folder,), deny_read=tuple(deny_read), allowed_domains=())
        # a settings folder of its own: adapters of parallel steps share the data folder
        settings = Path(tempfile.mkdtemp(prefix="sandbox-", dir=folder))
        self.sandbox = SrtSandbox(rules, settings, srt=srt)

    def tests_for(self, path: str) -> Candidates:
        rows = self._edges_into([path], self._project())
        tests = Counter()
        for source, kind, count in rows:
            if kind == "TESTS" and source != path:
                tests[source] += count
        return self._candidates(_ranked(tests))

    def dependents(self, path: str) -> Candidates:
        return self._candidates(self._dependents(path, self._project()))

    def impact(self, diff: str) -> Candidates:
        named = (tree_path(self.tree, raw) for raw in diff_files(diff))
        files = [f for f in dict.fromkeys(named) if f and (self.tree / f).is_file()]
        if not files:
            return Candidates((), source=self.name, note="the diff names no file in the tree")
        project = self._project()
        merged = list(files)
        for rel in files[:MAX_IMPACT_SEEDS]:
            merged += self._dependents(rel, project)
        found = self._candidates(merged)
        if len(files) > MAX_IMPACT_SEEDS:
            note = f"dependents of the first {MAX_IMPACT_SEEDS} changed files only"
            return Candidates(found.files, source=self.name, note=note)
        return found

    def overview(self) -> str:
        project = self._project()
        rows = self._query(project, "MATCH (n) RETURN count(DISTINCT n.file_path), count(n)")
        try:
            files, nodes = (int(value) for value in rows[0])
        except (IndexError, TypeError, ValueError) as error:
            raise CodeIntelError("codebase-memory-mcp gave an unreadable overview") from error
        return f"{files} files, {nodes} symbols (codebase-memory-mcp graph)"

    # ── queries ──────────────────────────────────────────────────────────────

    def _dependents(self, path: str, project: str) -> list[str]:
        direct = Counter()
        for source, _kind, count in self._edges_into([path], project):
            if source != path:
                direct[source] += count
        first = _ranked(direct)
        seen = {path, *first}
        second = Counter()
        for source, _kind, count in self._edges_into(first[:MAX_SECOND_HOP], project):
            if source not in seen:
                second[source] += count
        return first + _ranked(second)

    def _edges_into(self, paths: list[str], project: str) -> list[tuple[str, str, int]]:
        if not paths:
            return []
        where = (
            f"b.file_path = {_quoted(paths[0])}"
            if len(paths) == 1
            else f"b.file_path IN [{', '.join(_quoted(p) for p in paths)}]"
        )
        query = f"MATCH (a)-[r]->(b) WHERE {where} RETURN a.file_path, type(r), count(*)"
        rows = []
        for row in self._query(project, query):
            try:
                source, kind, count = row
                if source is None:  # an edge from outside any file
                    continue
                if not isinstance(source, str) or not isinstance(kind, str):
                    raise TypeError(f"row {row!r}")
                rows.append((source, kind, int(count)))
            except (TypeError, ValueError) as error:
                message = f"codebase-memory-mcp gave an unreadable row: {error}"
                raise CodeIntelError(message) from error
        return [row for row in rows if row[1] not in NOISE]

    def _project(self) -> str:
        """Index (or re-index) the tree; the project name the tool gave it."""
        args = {"repo_path": str(self.tree.resolve())}
        answer = self._run("index_repository", args, timeout=self.index_timeout_s)
        project = answer.get("project")
        if not isinstance(project, str) or not project:
            raise CodeIntelError("codebase-memory-mcp did not index the tree")
        return project

    def _query(self, project: str, query: str) -> list:
        args = {"project": project, "query": query, "max_rows": MAX_ROWS, "format": "json"}
        rows = self._run("query_graph", args, timeout=QUERY_TIMEOUT_S).get("rows", [])
        if not isinstance(rows, list):
            raise CodeIntelError("codebase-memory-mcp gave rows that are not a list")
        return rows

    def _candidates(self, files: list[str]) -> Candidates:
        # the tool's spelling (`./a`, `a\\b`) normalised; a file removed since the last
        # index is no candidate
        named = (tree_path(self.tree, f) for f in files if isinstance(f, str))
        found = [f for f in dict.fromkeys(named) if f and (self.tree / f).is_file()]
        return Candidates(tuple(found), source=self.name)

    # ── the tool ─────────────────────────────────────────────────────────────

    def _run(self, tool: str, args: dict, *, timeout: float) -> dict:
        if self._broken is not None:
            raise CodeIntelError(f"codebase-memory-mcp could not run: {self._broken}")
        if shutil.which(self.command[0]) is None:  # inside srt, a missing tool is no OSError
            self._broken = f"{self.command[0]} is not an installed program"
            raise CodeIntelError(f"codebase-memory-mcp could not run: {self._broken}")
        try:
            prefix = self.sandbox.command_prefix()  # ends `env NAME=value ...`: add the tool's
            inner = [*prefix, *self._env(), *self.command, "cli", "--quiet", "--json", tool]
            done = run_contained(
                inner,
                input=json.dumps(args),
                env={k: v for k, v in os.environ.items() if k.upper() in _SRT_PASSED},
                cwd=self.data,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self._broken = str(error) or type(error).__name__
            raise CodeIntelError(f"codebase-memory-mcp could not run: {error}") from error
        if done.returncode != 0:
            lines = done.stderr.strip().splitlines()
            raise CodeIntelError(
                f"codebase-memory-mcp {tool} failed: {lines[-1] if lines else done.returncode}"
            )
        return _payload(done.stdout, tool)

    def _env(self) -> list[str]:
        """The tool's home and caches, as `NAME=value` for the `env` inside the sandbox:
        srt keeps its own home, the tool gets the data folder's."""
        home, cache = self.data / "cbm-home", self.data / "cbm-cache"
        for folder in (home, cache):
            folder.mkdir(parents=True, exist_ok=True)
        names = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME")
        env = {name: str(home) for name in names}
        env["XDG_CACHE_HOME"] = env["CBM_CACHE_DIR"] = str(cache)
        return [f"{name}={value}" for name, value in env.items()]


def _ranked(counts: Counter) -> list[str]:
    return [f for f, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _payload(stdout: str, tool: str) -> dict:
    """The tool's JSON answer out of the MCP envelope `--json` prints."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            envelope = json.loads(line)
        except ValueError:
            continue
        if not isinstance(envelope, dict):
            continue
        content = envelope.get("content")
        if not isinstance(content, list):
            break
        text = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if envelope.get("isError"):
            raise CodeIntelError(f"codebase-memory-mcp {tool} refused: {text[:300]}")
        try:
            payload = json.loads(text)
        except ValueError:
            break
        if isinstance(payload, dict):
            return payload
        break
    raise CodeIntelError(f"codebase-memory-mcp {tool} returned no answer")
