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

from agentic.backends.processes import run_contained
from agentic.codeintel.index import LocalIndex
from agentic.codeintel.interface import Candidates, CodeIntelError
from agentic.codeintel.tools import tree_path

_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
_RENAME = ("rename from ", "rename to ", "copy from ", "copy to ")


def diff_files(diff: str) -> list[str]:
    """Every file name a unified diff gives, in order (DF-91): `---`/`+++` lines (a tab
    and what follows - `diff -u`'s timestamp - are not the name), `rename`/`copy` lines
    (a pure rename has no hunks), and the `diff --git` header (a mode change has nothing
    else). git's quoted names (`"b/caf\\303\\251.py"`) are unquoted. A name with an
    `a/` or `b/` prefix is also offered without it: a `--no-prefix` diff has none, and
    which one names a file in the tree decides."""
    names: list[tuple[str, bool]] = []  # (name, may carry a prefix)
    for line in diff.splitlines():
        if line.startswith(("--- ", "+++ ")):
            raw = line[4:]
            name = _unquote(raw)[0] if raw.startswith('"') else raw.split("\t", 1)[0].rstrip()
            names.append((name, True))
        elif line.startswith(_RENAME):
            raw = line.split(" ", 2)[2]
            names.append((_unquote(raw)[0] if raw.startswith('"') else raw, False))
        elif line.startswith("diff --git "):
            names += [(name, True) for name in _header_names(line[len("diff --git ") :])]
    found: list[str] = []
    for name, prefixed in names:
        if not name or name == "/dev/null":
            continue
        found.append(name)
        if prefixed and name[:2] in ("a/", "b/") and len(name) > 2:
            found.append(name[2:])
    return list(dict.fromkeys(found))


def _unquote(text: str) -> tuple[str, str]:
    """git's C-style quoted name at the start of `text`, and the text after it."""
    out, i = bytearray(), 1
    while i < len(text) and text[i] != '"':
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            following = text[i + 1]
            if following in "01234567":
                digits = re.match(r"[0-7]{1,3}", text[i + 1 :]).group(0)
                out.append(int(digits, 8) & 0xFF)
                i += 1 + len(digits)
                continue
            if following in _ESCAPES:
                out.append(_ESCAPES[following])
            else:  # not one of git's escapes (DF-331): keep the character, whatever it is
                out += following.encode("utf-8")
            i += 2
            continue
        out += char.encode("utf-8")
        i += 1
    return out.decode("utf-8", errors="replace"), text[i + 1 :]


def _header_names(rest: str) -> list[str]:
    """The two names of a `diff --git` header; [] when spaces make them ambiguous (the
    `rename` lines then name the files)."""
    if rest.startswith('"'):
        first, after = _unquote(rest)
        after = after.strip()
        second = _unquote(after)[0] if after.startswith('"') else after
        return [first, second]
    if ' "' in rest:
        first, _, after = rest.partition(' "')
        return [first, _unquote('"' + after)[0]]
    half = len(rest) // 2
    first, gap, second = rest[:half], rest[half : half + 1], rest[half + 1 :]
    if gap == " " and (first == second or (first[2:] == second[2:] and first[:2] == "a/")):
        return [first, second]
    return []


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
        named = (tree_path(self.index.tree, raw) for raw in diff_files(diff))
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
            done = run_contained(
                [sys.executable, "-m", "agentic.codeintel._crg_query"],
                input=payload,
                env=self.index.env,
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
