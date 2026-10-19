"""What the agent tools work with: the run's tree, its guard, and pluggable services.

The tools take small interfaces, not concrete classes, so `core` does not depend
on the Zyroflo-backed commons package: the engine passes the real
`sdlc_commons.guards.Guard`, the real sandbox (M1-32/33), code intelligence
(M1-34) and the skill catalog (M1-37/38).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ToolConfigError(Exception):
    """The tools cannot be built as asked (an unknown capability, a missing service)."""


class Block(Protocol):
    wall: str
    why: str


class PathGuard(Protocol):
    """`sdlc_commons.guards.Guard`: every reason a write or a command is refused."""

    def writes(self, paths: Sequence[Path | str]) -> Sequence[Block]: ...

    def command(self, command: str) -> Sequence[Block]: ...


@dataclass(frozen=True)
class ShellResult:
    exit_code: int | None  # None when the command was stopped (timed out)
    stdout: str
    stderr: str
    timed_out: bool


class Sandbox(Protocol):
    isolated: bool  # True only for a real OS sandbox

    def run(self, command: str, cwd: Path, timeout_s: float) -> ShellResult: ...


class CodeIntel(Protocol):
    def dependents(self, target: str) -> list[str]: ...

    def impact(self, diff: str) -> list[str]: ...

    def tests_for(self, path: str) -> list[str]: ...


class SkillCatalog(Protocol):
    def get(self, name: str) -> str: ...


@dataclass(frozen=True)
class ToolContext:
    tree: Path  # the run's working tree: tools read and write only here
    guard: PathGuard
    sandbox: Sandbox | None = None
    codeintel: CodeIntel | None = None
    skills: SkillCatalog | None = None
    allow_unisolated: bool = False  # run_shell without an isolated sandbox (tests, dev)
    shell_timeout_s: float = 600.0


def refused(reason: str) -> str:
    """What a refused call returns to the model: it can read the reason and try
    something else (a ModelRetry would count against the run's retry limit)."""
    return f"REFUSED: {reason}"
