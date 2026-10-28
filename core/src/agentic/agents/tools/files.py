"""Reading, searching and editing files in the run's tree.

Every path goes through the guard first. Writes are refused for every wall
(outside the tree, protected files, framework-protected files, deny globs).
Reads are refused for the same walls except the framework one: an agent may
read AGENTS.md or a CI workflow, it may not change them - but reading a secret
(`.env`) or a denied path would hand it to the model, so those are refused too.

Arguments come from the model, so every tool is `guarded`: a bad one is refused,
never raised. Searches use `regex` with time limits, so a pattern that backtracks
catastrophically is refused instead of freezing the run.
"""

import time
from pathlib import Path, PurePosixPath, PureWindowsPath

import regex
from pydantic_ai import Tool

from agentic.agents.tools.context import ToolContext, guarded, refused
from agentic.codeintel.tools import names_git

MAX_READ_BYTES = 200_000
MAX_LIST = 500
MAX_MATCHES = 200
MAX_SEARCH_FILES = 2_000
MAX_SEARCH_FILE_BYTES = 1_000_000
MAX_PATTERN = 200
MAX_LINE = 2_000  # longer lines are searched in their first MAX_LINE characters
LINE_TIMEOUT_S = 0.1
SEARCH_DEADLINE_S = 5.0

# Names Windows maps to devices in every folder, with or without an extension.
DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{n}" for n in range(1, 10)]
    + [f"LPT{n}" for n in range(1, 10)]
)


def _target(ctx: ToolContext, path: str) -> Path:
    given = Path(path)
    return given if given.is_absolute() else ctx.tree / given


def _refusal(ctx: ToolContext, target: Path, *, reading: bool) -> str | None:
    """Why `target` may not be touched, or None. The tree is checked here first: the
    guard confines a run to its run folder, which holds every component's tree, so a
    path one level above this tree would otherwise pass. Resolving also follows links.
    Device names are refused on every OS, so a run behaves the same everywhere."""
    if device := next((p for p in target.parts if _is_device(p)), None):
        return refused(f"{device} is a Windows device name")
    try:
        inside = target.resolve().relative_to(ctx.tree.resolve())
    except ValueError:
        return refused(f"{target.as_posix()} is outside the working tree")
    # git's metadata is never read either: `.git/config` can hold a remote URL with a
    # token, and a worktree's `.git` names the main repository (DF-339). Every alias
    # counts (`.GIT`, `.git.`, `GIT~1`), as spelt and as resolved.
    spelt = target.relative_to(ctx.tree) if target.is_relative_to(ctx.tree) else inside
    if any(names_git(part) for part in (*spelt.parts, *inside.parts)):
        return refused("git's metadata (.git) is not part of the working tree's files")
    blocks = list(ctx.guard.writes([target]))
    if reading:
        blocks = [b for b in blocks if b.wall != "framework"]
    return refused("; ".join(b.why for b in blocks)) if blocks else None


def _is_device(part: str) -> bool:
    return part.split(".", 1)[0].rstrip(" ").upper() in DEVICE_NAMES


def _shown(ctx: ToolContext, target: Path) -> str:
    try:
        return target.resolve().relative_to(ctx.tree.resolve()).as_posix()
    except ValueError:
        return target.as_posix()


def read_tools(ctx: ToolContext) -> list[Tool]:
    @guarded
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

    @guarded
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


def _glob_refusal(glob: str) -> str | None:
    posix = PurePosixPath(glob.replace("\\", "/"))
    if not glob.strip():
        return refused("the glob is empty")
    if posix.is_absolute() or PureWindowsPath(glob).anchor:
        return refused("the glob must be relative to the working tree")
    if ".." in posix.parts:
        return refused("the glob must not leave the working tree ('..')")
    return None


def search_tools(ctx: ToolContext) -> list[Tool]:
    @guarded
    def search(pattern: str, glob: str = "**/*") -> str:
        """Search files in the working tree for a regular expression; lines as path:line: text."""
        if len(pattern) > MAX_PATTERN:
            return refused(f"the pattern is longer than {MAX_PATTERN} characters")
        if why := _glob_refusal(glob):
            return why
        try:
            compiled = regex.compile(pattern)
        except regex.error as error:
            return refused(f"not a valid regular expression: {error}")
        deadline = time.monotonic() + SEARCH_DEADLINE_S
        matches: list[str] = []
        scanned = 0
        try:
            for file in sorted(ctx.tree.glob(glob)):
                if len(matches) >= MAX_MATCHES or scanned >= MAX_SEARCH_FILES:
                    break
                if any(names_git(p) for p in file.relative_to(ctx.tree).parts):
                    continue
                if not file.is_file():
                    continue
                if file.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                if _refusal(ctx, file, reading=True):
                    continue
                data = file.read_bytes()
                if b"\x00" in data[:8192]:
                    continue  # binary
                scanned += 1
                lines = data.decode("utf-8", errors="replace").splitlines()
                for number, line in enumerate(lines, 1):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError
                    if compiled.search(line[:MAX_LINE], timeout=min(LINE_TIMEOUT_S, left)):
                        matches.append(f"{_shown(ctx, file)}:{number}: {line.strip()[:300]}")
                        if len(matches) >= MAX_MATCHES:
                            break
        except TimeoutError:
            return refused("the pattern is too slow; use a simpler one or a narrower glob")
        return "\n".join(matches) if matches else "(no matches)"

    return [Tool(search)]


def _replaced(text: str, old: str, new: str) -> str | None:
    """`text` with the one occurrence of `old` replaced, or None. In a file with
    Windows line endings, text the model sends with plain newlines still matches,
    and the replacement keeps the file's line endings."""
    if old and text.count(old) == 1:
        return text.replace(old, new, 1)
    if "\r\n" in text and "\r\n" not in old:
        crlf_old, crlf_new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        if crlf_old and text.count(crlf_old) == 1:
            return text.replace(crlf_old, crlf_new, 1)
    return None


def edit_tools(ctx: ToolContext) -> list[Tool]:
    @guarded
    def edit_file(path: str, old: str, new: str) -> str:
        """Replace one exact, unique piece of text in a file of the working tree."""
        target = _target(ctx, path)
        if why := _refusal(ctx, target, reading=False):
            return why
        if not target.is_file():
            return refused(f"no such file: {path}")
        try:
            text = target.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return refused(f"{path} is not UTF-8 text; it was left unchanged")
        edited = _replaced(text, old, new)
        if edited is None:
            count = text.count(old) if old else 0
            return refused(f"the text to replace must appear exactly once; found {count}")
        target.write_bytes(edited.encode("utf-8"))
        return f"edited {_shown(ctx, target)}"

    @guarded
    def write_file(path: str, text: str) -> str:
        """Create or overwrite a file in the working tree."""
        target = _target(ctx, path)
        if why := _refusal(ctx, target, reading=False):
            return why
        if target.is_dir():
            return refused(f"{path} is a directory")
        data = text.encode("utf-8")  # before any change: text that cannot be encoded is refused
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return f"wrote {_shown(ctx, target)} ({len(text)} characters)"

    return [Tool(edit_file), Tool(write_file)]
