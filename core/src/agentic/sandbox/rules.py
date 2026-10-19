"""What a sandboxed command may touch, and the `srt` settings file that says so.

`srt` refuses a settings file that lacks any key rather than falling back to its
defaults (spike S6), so `srt_settings` always writes every key - an empty list
says "none", never "unset".
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


class SandboxError(Exception):
    """A sandbox cannot be set up as asked."""


@dataclass(frozen=True)
class SandboxRules:
    write: tuple[Path, ...]  # the only writable places: the run's tree, and policy's caches
    deny_write: tuple[Path, ...] = ()  # never writable, even inside `write`
    deny_read: tuple[Path, ...] = ()  # hidden: credentials and other broad regions
    allow_read: tuple[Path, ...] = ()  # readable again inside a `deny_read` region
    allowed_domains: tuple[str, ...] = ()  # the only hosts a command may reach


def srt_settings(rules: SandboxRules) -> dict:
    """The `srt --settings` file for these rules: every key, paths as POSIX text."""
    if not rules.write:
        raise SandboxError("a sandbox needs at least one writable path (the run's tree)")

    def paths(values: tuple[Path, ...]) -> list[str]:
        for path in values:
            if not Path(path).is_absolute():
                raise SandboxError(f"sandbox paths must be absolute, not {str(path)!r}")
        return [Path(path).as_posix() for path in values]

    return {
        "network": {"allowedDomains": list(rules.allowed_domains), "deniedDomains": []},
        "filesystem": {
            "denyRead": paths(rules.deny_read),
            "allowRead": paths(rules.allow_read),
            "allowWrite": paths(rules.write),
            "denyWrite": paths(rules.deny_write),
        },
    }


def expand_home(entries: Iterable[str], home: Path) -> tuple[Path, ...]:
    """Policy paths as absolute paths: `~` and `~/x` under `home`, others as written."""
    out = []
    for entry in entries:
        if entry == "~":
            out.append(Path(home))
        elif entry.startswith("~/"):
            out.append(Path(home) / entry[2:])
        else:
            out.append(Path(entry))
    return tuple(out)
