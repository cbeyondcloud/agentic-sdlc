"""Agent tools over a `CodeIntel` adapter (M1-34).

One tool per operation the adapter supports, plus `context_pack`. Paths are read
relative to the run's tree in any spelling (`src\\app.py`, `./src/app.py`, an
absolute path inside the tree); a path outside the tree is refused and never
queried. Answers list at most MAX_LISTED files and always end with the label that
they are candidates.
"""

import posixpath
import re
from pathlib import Path

from pydantic_ai import Tool

from agentic.codeintel.interface import Candidates, CodeIntel, CodeIntelError

MAX_LISTED = 50
_DRIVE = re.compile(r"^[A-Za-z]:")


def _refused(reason: str) -> str:
    return f"REFUSED: {reason}"


def tree_path(tree: Path, raw: str) -> str | None:
    """`raw` as a tree-relative POSIX path, or None when it is not inside the tree."""
    text = raw.strip().replace("\\", "/")
    if not text:
        return None
    root = tree.resolve()
    if text.startswith("/") or _DRIVE.match(text):
        try:
            relative = Path(text).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            return None
    else:
        relative = posixpath.normpath(text)
        if relative == ".." or relative.startswith("../") or relative == ".":
            return None
    try:
        (root / relative).resolve().relative_to(root)
    except (ValueError, OSError):
        return None
    return relative


def _listing(found: Candidates) -> str:
    lines = list(found.files[:MAX_LISTED]) or ["(none)"]
    if len(found.files) > MAX_LISTED:
        lines.append(f"+{len(found.files) - MAX_LISTED} more")
    if found.note:
        lines.append(found.note)
    return "\n".join(lines) + f"\n{_label(found.source)}"


def _label(source: str) -> str:
    return f"(candidates from {source} - verify before relying on them)"


def codeintel_tools(intel: CodeIntel, tree: Path) -> list[Tool]:
    """The agent tools for the operations `intel` supports."""
    tree = Path(tree)

    def asked(raw: str):
        path = tree_path(tree, raw)
        return path, (None if path else _refused(f"{raw!r} is not a path inside the working tree"))

    def dependents(path: str) -> str:
        """Files that depend on a file (imports, calls, tests): candidates to check."""
        target, refusal = asked(path)
        if refusal:
            return refusal
        try:
            return _listing(intel.dependents(target))
        except CodeIntelError as error:
            return _refused(str(error))

    def tests_for(path: str) -> str:
        """The tests that cover a file: candidates to run."""
        target, refusal = asked(path)
        if refusal:
            return refusal
        try:
            return _listing(intel.tests_for(target))
        except CodeIntelError as error:
            return _refused(str(error))

    def impact(diff: str) -> str:
        """What a unified diff may affect, tests included: candidates to check."""
        try:
            return _listing(intel.impact(diff))
        except CodeIntelError as error:
            return _refused(str(error))

    def overview() -> str:
        """A short map of the codebase: size and the most depended-on files."""
        try:
            return intel.overview()
        except CodeIntelError as error:
            return _refused(str(error))

    def context_pack(path: str, budget_chars: int = 20_000) -> str:
        """A file's text with the names of its tests and dependents, within a budget."""
        target, refusal = asked(path)
        if refusal:
            return refusal
        source = tree / target
        if not source.is_file():
            return _refused(f"{target} is not a file in the working tree")
        try:
            tests = intel.tests_for(target).files if "tests_for" in intel.supports else ()
            users = intel.dependents(target).files if "dependents" in intel.supports else ()
        except CodeIntelError as error:
            return _refused(str(error))
        names = [f"Tests: {', '.join(tests[:20]) or '(none)'}"]
        names.append(f"Dependents: {', '.join(users[:20]) or '(none)'}")
        room = max(budget_chars - sum(len(n) + 1 for n in names) - len(target) - 12, 0)
        text = source.read_text(encoding="utf-8", errors="replace")
        body = text if len(text) <= room else text[:room] + "\n[... file truncated]"
        return "\n".join([f"=== {target} ===", body, *names, _label(intel.name)])

    offered = {
        "dependents": [Tool(dependents)],
        "tests_for": [Tool(tests_for)],
        "impact": [Tool(impact)],
        "overview": [Tool(overview)],
    }
    tools = [
        tool
        for op in ("dependents", "impact", "tests_for", "overview")
        if op in intel.supports
        for tool in offered[op]
    ]
    if intel.supports & {"dependents", "tests_for"}:
        tools.append(Tool(context_pack))
    return tools
