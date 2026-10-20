"""The default symbol-level adapter: code-review-graph (M1-35, spike S5).

Every answer first makes the local index fresh (`LocalIndex.ensure`), then runs one
query in a short-lived subprocess (`_crg_query`) with the index's data folder set -
the framework process never imports code-review-graph or changes its environment.

`dependents` is spike S5's composed answer: files importing the seed, then the tests
of the seed file and of each symbol in it, then the remaining impact files. A failed
build or query raises `CodeIntelError`, which the agent tools turn into a refusal.
"""

import json
import re
import subprocess
import sys

from agentic.codeintel.index import LocalIndex
from agentic.codeintel.interface import Candidates, CodeIntelError
from agentic.codeintel.tools import tree_path

_DIFF_FILE = re.compile(r"^(?:\+\+\+ b/|--- a/)(.+?)\s*$", re.MULTILINE)


class CodeReviewGraph:
    name = "code-review-graph"
    supports = frozenset({"dependents", "impact", "tests_for", "overview"})

    def __init__(self, index: LocalIndex, *, timeout_s: float = 600) -> None:
        self.index, self.timeout_s = index, timeout_s

    def dependents(self, path: str) -> Candidates:
        return self._candidates({"op": "dependents", "path": path})

    def tests_for(self, path: str) -> Candidates:
        return self._candidates({"op": "tests_for", "path": path})

    def impact(self, diff: str) -> Candidates:
        # every path the diff names goes through the tree's confinement (SP9 review I7)
        named = (tree_path(self.index.tree, raw) for raw in _DIFF_FILE.findall(diff))
        files = [f for f in dict.fromkeys(named) if f and self._exists(f)]
        if not files:
            return Candidates((), source=self.name, note="the diff names no file in the tree")
        return self._candidates({"op": "impact", "files": files})

    def overview(self) -> str:
        return self._query({"op": "overview"}).get("text", "")

    def _exists(self, rel: str) -> bool:
        return (self.index.tree / rel).is_file()

    def _candidates(self, request: dict) -> Candidates:
        # a file removed since the last update is no candidate (SP9 review I1)
        files = [f for f in self._query(request).get("files", []) if self._exists(f)]
        return Candidates(tuple(files), source=self.name)

    def _query(self, request: dict) -> dict:
        self.index.ensure()  # CodeIntelError when it cannot be built or updated
        payload = json.dumps({**request, "tree": str(self.index.tree)})
        try:
            done = subprocess.run(
                [sys.executable, "-m", "agentic.codeintel._crg_query"],
                input=payload,
                env=self.index.env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CodeIntelError(f"code-review-graph query could not run: {error}") from error
        if done.returncode != 0:
            lines = done.stderr.strip().splitlines()
            raise CodeIntelError(
                f"code-review-graph query failed: {lines[-1] if lines else done.returncode}"
            )
        return parse_answer(done.stdout)


def parse_answer(stdout: str) -> dict:
    """The JSON answer: the last line that is a JSON object (a tool may print warnings
    before it, SP9 review I4)."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        if isinstance(answer, dict):
            return answer
    raise CodeIntelError("code-review-graph query returned no answer")
