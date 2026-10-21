"""Agent tools over a `CodeIntel` adapter (M1-34).

One tool per operation the adapter supports, plus `context_pack`. Paths are read
relative to the run's tree in any spelling (`src\\app.py`, `./src/app.py`, an
absolute path inside the tree); a path outside the tree is refused and never
queried, and a UNC or device path is refused by its spelling alone, before any
filesystem call could reach a network share (SP9 review I6).

`context_pack` reads files only through the guard's read check (`read_refusal`),
the same one `read_file` uses - a protected or denied file never reaches the model
(SP9 review C1) - and answers leave out the paths that check refuses. Answers list at
most MAX_LISTED files and always end with the label that they are candidates. No
error inside a tool ends the run: it comes back as a refusal (SP9 review I4).
"""

import functools
import os
import posixpath
import re
from collections.abc import Callable
from pathlib import Path

from pydantic_ai import Tool

from agentic.codeintel.interface import Candidates, CodeIntel

MAX_LISTED = 50
MAX_BUDGET_CHARS = 100_000
_DRIVE = re.compile(r"^[A-Za-z]:")
_SHORT_GIT = re.compile(r"git~\d+")

ReadRefusal = Callable[[Path], str | None]


def _refused(reason: str) -> str:
    return reason if reason.startswith("REFUSED") else f"REFUSED: {reason}"


def tree_path(tree: Path, raw: str) -> str | None:
    """`raw` as a tree-relative POSIX path, or None when it is not inside the tree."""
    text = raw.strip().replace("\\", "/")
    if not text or "\0" in text or text.startswith("//"):  # UNC and \\?\ \\.\ device paths
        return None
    root = tree.resolve()
    if text.startswith("/") or _DRIVE.match(text):
        # judged by spelling first: only a path that names the tree is ever resolved
        lexical = os.path.normcase(os.path.normpath(text))
        prefix = os.path.normcase(str(root))
        if lexical != prefix and not lexical.startswith(prefix.rstrip(os.sep) + os.sep):
            return None
        try:
            relative = Path(text).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            return None
    else:
        relative = posixpath.normpath(text)
        if relative == ".." or relative.startswith("../"):
            return None
    if relative == "." or any(names_git(part) for part in relative.split("/")):
        return None  # git's metadata is not code: a worktree's `.git` names the main repo
    try:
        opened = (root / relative).resolve().relative_to(root)
    except (ValueError, OSError):
        return None
    if any(names_git(part) for part in opened.parts):  # a link to `.git`, or a short name
        return None
    return relative


def names_git(part: str) -> bool:
    """Whether a path component can open `.git`: in any case, with the trailing dots and
    spaces or the `:stream` Windows ignores, or as an 8.3 short name (`GIT~1`) - refused
    on every OS, the safe side (final review F1)."""
    name = part.split(":", 1)[0].rstrip(". ").lower()
    return name == ".git" or _SHORT_GIT.fullmatch(name) is not None


def _label(source: str) -> str:
    return f"(candidates from {source} - verify before relying on them)"


def _safe(tool):
    """Any error inside a tool comes back to the model as a refusal, never ends the run."""

    @functools.wraps(tool)
    def run(*args, **kwargs):
        try:
            return tool(*args, **kwargs)
        except Exception as error:  # noqa: BLE001 - see the docstring
            return _refused(f"{type(error).__name__}: {error}")

    return run


def codeintel_tools(
    intel: CodeIntel, tree: Path, *, read_refusal: ReadRefusal | None = None
) -> list[Tool]:
    """The agent tools for the operations `intel` supports."""
    tree = Path(tree)

    def allowed(rel: str) -> bool:
        return read_refusal is None or read_refusal(tree / rel) is None

    def listing(found: Candidates) -> str:
        files = [f for f in found.files if allowed(f)]
        lines = files[:MAX_LISTED] or ["(none)"]
        if len(files) > MAX_LISTED:
            lines.append(f"+{len(files) - MAX_LISTED} more")
        if found.note:
            lines.append(found.note)
        return "\n".join(lines) + f"\n{_label(found.source)}"

    def asked(raw: str) -> str:
        path = tree_path(tree, raw)
        if path is None:
            raise ValueError(f"{raw!r} is not a path inside the working tree")
        return path

    @_safe
    def dependents(path: str) -> str:
        """Files that depend on a file (imports, calls, tests): candidates to check."""
        return listing(intel.dependents(asked(path)))

    @_safe
    def tests_for(path: str) -> str:
        """The tests that cover a file: candidates to run."""
        return listing(intel.tests_for(asked(path)))

    @_safe
    def impact(diff: str) -> str:
        """What a unified diff may affect, tests included: candidates to check."""
        return listing(intel.impact(diff))

    @_safe
    def overview() -> str:
        """A short map of the codebase: size and the most depended-on files."""
        return intel.overview()

    @_safe
    def context_pack(path: str, budget_chars: int = 20_000) -> str:
        """A file's text with the names of its tests and dependents, within a budget."""
        target = asked(path)
        if read_refusal is not None and (why := read_refusal(tree / target)):
            return _refused(why)
        source = tree / target
        if not source.is_file():
            return _refused(f"{target} is not a file in the working tree")
        budget = max(0, min(int(budget_chars), MAX_BUDGET_CHARS))
        tests = intel.tests_for(target).files if "tests_for" in intel.supports else ()
        users = intel.dependents(target).files if "dependents" in intel.supports else ()
        tests = [f for f in tests if allowed(f)][:20]
        users = [f for f in users if allowed(f)][:20]
        names = [f"Tests: {', '.join(tests) or '(none)'}"]
        names.append(f"Dependents: {', '.join(users) or '(none)'}")
        room = max(budget - sum(len(n) + 1 for n in names) - len(target) - 12, 0)
        with source.open(encoding="utf-8", errors="replace") as handle:
            text = handle.read(room + 1)  # never more than the budget into memory
        body = text if len(text) <= room else text[:room] + "\n[... file truncated]"
        return "\n".join([f"=== {target} ===", body, *names, _label(intel.name)])

    offered = {
        "dependents": dependents,
        "impact": impact,
        "tests_for": tests_for,
        "overview": overview,
    }
    tools = [Tool(offered[op]) for op in offered if op in intel.supports]
    if intel.supports & {"dependents", "tests_for"}:
        tools.append(Tool(context_pack))
    return tools
