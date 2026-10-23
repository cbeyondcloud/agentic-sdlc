"""Which file names a policy tree may hold (review F4): every laptop - Windows, macOS and
Linux - must extract the tree the control plane validated to the same files, so the CLI's
`tree_digest` matches the server's. The control plane refuses a commit holding a name that
fails here (`policy.path`), and the CLI checks a bundle's names before extracting it.

A name fails when Windows cannot create it (a reserved character, a device name such as
`CON` or `nul.txt`, a trailing dot or space), when it is too long for a default Windows
path under `~/.agentic/policy/<org>/repo/`, when it is not in Unicode NFC (some macOS
file systems store names decomposed), or when two names differ only in case or Unicode
form (Windows and macOS keep one of them). Paths are git's: relative, '/'-separated."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

#: The longest tree path kept: Windows' default MAX_PATH (260) less room for the cache
#: folder under the user's home (`C:\Users\<name>\.agentic\policy\<org>\repo\`).
MAX_PATH_CHARS = 160
#: The longest single name most file systems allow.
MAX_NAME_CHARS = 255
_RESERVED_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')
#: Names Windows reserves for devices, whatever follows the first dot.
_DEVICE = re.compile(r"(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)(\..*)?", re.I)


def portable_path_problems(paths: Iterable[str]) -> list[str]:
    """One `<path>: ...` line per path some laptop could not extract as it is ([] = all
    portable), in the order given."""
    problems: list[str] = []
    seen: dict[str, str] = {}  # folded path (or folder) -> the spelling met first
    for path in paths:
        problem = _name_problem(path)
        if problem is None:
            problem = _collision(path, seen)
        if problem is not None:
            problems.append(f"{path}: {problem} (policy.path: every laptop must extract it)")
    return problems


def _name_problem(path: str) -> str | None:
    if len(path) > MAX_PATH_CHARS:
        return f"the path is longer than {MAX_PATH_CHARS} characters"
    if unicodedata.normalize("NFC", path) != path:
        return "the name is not in Unicode NFC form"
    for part in path.split("/"):
        if not part or part in (".", ".."):
            return "the path has an empty, '.' or '..' part"
        if len(part) > MAX_NAME_CHARS:
            return f"a name is longer than {MAX_NAME_CHARS} characters"
        if _RESERVED_CHARS.search(part):
            return 'a name holds a character Windows refuses (< > : " | ? * \\ or a control)'
        if part[-1] in ". ":
            return "a name ends with a dot or a space, which Windows drops"
        if _DEVICE.fullmatch(part):
            return f"{part!r} is a Windows device name"
    return None


def _collision(path: str, seen: dict[str, str]) -> str | None:
    """The other spelling `path` (or one of its folders) collides with, case-folded."""
    parts = path.split("/")
    for end in range(1, len(parts) + 1):
        prefix = "/".join(parts[:end])
        folded = unicodedata.normalize("NFC", prefix).casefold()
        first = seen.setdefault(folded, prefix)
        if first != prefix:
            return f"collides with {first!r} on Windows and macOS (names differ only in case)"
    return None
