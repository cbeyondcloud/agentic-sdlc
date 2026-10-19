"""Reading, searching and editing files in the run's tree.

Every path goes through the guard first. Writes are refused for every wall
(outside the tree, protected files, framework-protected files, deny globs).
Reads are refused for the same walls except the framework one: an agent may
read AGENTS.md or a CI workflow, it may not change them - but reading a secret
(`.env`) or a denied path would hand it to the model, so those are refused too.
"""

import re
from pathlib import Path

from pydantic_ai import Tool

from agentic.agents.tools.context import ToolContext, refused

MAX_READ_BYTES = 200_000
MAX_LIST = 500
MAX_MATCHES = 200
MAX_SEARCH_FILES = 2_000
MAX_SEARCH_FILE_BYTES = 1_000_000


def _target(ctx: ToolContext, path: str) -> Path:
    given = Path(path)
    return given if given.is_absolute() else ctx.tree / given


def _refusal(ctx: ToolContext, target: Path, *, reading: bool) -> str | None:
    """Why `target` may not be touched, or None. The tree is checked here first: the
    guard confines a run to its run folder, which holds every component's tree, so a
    path one level above this tree would otherwise pass. Resolving also follows links."""
    try:
        target.resolve().relative_to(ctx.tree.resolve())
    except ValueError:
        return refused(f"{target.as_posix()} is outside the working tree")
    blocks = list(ctx.guard.writes([target]))
    if reading:
        blocks = [b for b in blocks if b.wall != "framework"]
    return refused("; ".join(b.why for b in blocks)) if blocks else None


def _shown(ctx: ToolContext, target: Path) -> str:
    try:
        return target.resolve().relative_to(ctx.tree.resolve()).as_posix()
    except ValueError:
        return target.as_posix()


def read_tools(ctx: ToolContext) -> list[Tool]:
    def read_file(path: str) -> str:
        """Read a text file in the working tree (path relative to the tree)."""
        target = _target(ctx, path)
        if why := _refusal(ctx, target, reading=True):
            return why
        if not target.is_file():
            return refused(f"no such file: {path}")
        data = target.read_bytes()
        text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        if len(data) > MAX_READ_BYTES:
            text += f"\n[... truncated at {MAX_READ_BYTES} bytes of {len(data)}]"
        return text

    def list_dir(path: str = ".") -> str:
        """List a directory in the working tree."""
        target = _target(ctx, path)
        if why := _refusal(ctx, target, reading=True):
            return why
        if not target.is_dir():
            return refused(f"no such directory: {path}")
        names = sorted(e.name + ("/" if e.is_dir() else "") for e in target.iterdir())
        shown = names[:MAX_LIST]
        more = f"\n[... {len(names) - MAX_LIST} more]" if len(names) > MAX_LIST else ""
        return "\n".join(shown) + more

    return [Tool(read_file), Tool(list_dir)]


def search_tools(ctx: ToolContext) -> list[Tool]:
    def search(pattern: str, glob: str = "**/*") -> str:
        """Search files in the working tree for a regular expression; lines as path:line: text."""
        try:
            regex = re.compile(pattern)
        except re.error as error:
            return refused(f"not a valid regular expression: {error}")
        matches: list[str] = []
        scanned = 0
        for file in sorted(ctx.tree.glob(glob)):
            if len(matches) >= MAX_MATCHES or scanned >= MAX_SEARCH_FILES:
                break
            if ".git" in file.relative_to(ctx.tree).parts or not file.is_file():
                continue
            if file.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            if _refusal(ctx, file, reading=True):
                continue
            data = file.read_bytes()
            if b"\x00" in data[:8192]:
                continue  # binary
            scanned += 1
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{_shown(ctx, file)}:{number}: {line.strip()[:300]}")
                    if len(matches) >= MAX_MATCHES:
                        break
        return "\n".join(matches) if matches else "(no matches)"

    return [Tool(search)]


def edit_tools(ctx: ToolContext) -> list[Tool]:
    def edit_file(path: str, old: str, new: str) -> str:
        """Replace one exact, unique piece of text in a file of the working tree."""
        target = _target(ctx, path)
        if why := _refusal(ctx, target, reading=False):
            return why
        if not target.is_file():
            return refused(f"no such file: {path}")
        text = target.read_bytes().decode("utf-8", errors="replace")  # line endings kept
        count = text.count(old) if old else 0
        if count != 1:
            return refused(f"the text to replace must appear exactly once; found {count}")
        target.write_bytes(text.replace(old, new, 1).encode("utf-8"))
        return f"edited {_shown(ctx, target)}"

    def write_file(path: str, text: str) -> str:
        """Create or overwrite a file in the working tree."""
        target = _target(ctx, path)
        if why := _refusal(ctx, target, reading=False):
            return why
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        return f"wrote {_shown(ctx, target)} ({len(text)} characters)"

    return [Tool(edit_file), Tool(write_file)]
