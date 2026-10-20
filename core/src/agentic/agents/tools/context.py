"""What the agent tools work with: the run's tree, its guard, and pluggable services.

The tools take small interfaces, not concrete classes, so `core` does not depend
on the Zyroflo-backed commons package: the engine passes the real
`sdlc_commons.guards.Guard`, the real sandbox (M1-32/33), code intelligence
(M1-34) and the skill catalog (M1-37/38).
"""

import functools
import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from agentic.codeintel.interface import CodeIntel  # re-exported: the tools take one
from agentic.sandbox.process import ShellResult  # re-exported: tools return it
from agentic.security import SecretScanner

if TYPE_CHECKING:
    from agentic.policy import ResolvedAgent

# What a bad argument from the model raises inside a tool: a directory where a file
# was meant, text that cannot be encoded, an unusable glob, a pattern that is too slow.
TOOL_ERRORS = (OSError, ValueError, NotImplementedError, TimeoutError)


class ToolConfigError(Exception):
    """The tools cannot be built as asked (an unknown capability, a missing service)."""


class Block(Protocol):
    wall: str
    why: str


class PathGuard(Protocol):
    """`sdlc_commons.guards.Guard`: every reason a write or a command is refused."""

    def writes(self, paths: Sequence[Path | str]) -> Sequence[Block]: ...

    def command(self, command: str) -> Sequence[Block]: ...

    def asks(self, command: str) -> Sequence[str]:
        """The ask rules a command hits: it runs only once a person approves it."""
        ...


class Sandbox(Protocol):
    """Runs a command. Async, so that stopping the run (time limit, kill switch)
    cancels the call and the sandbox stops the command with it."""

    isolated: bool  # True only for a real OS sandbox

    async def run(self, command: str, cwd: Path, timeout_s: float) -> ShellResult: ...


class SkillCatalog(Protocol):
    def get(self, name: str) -> str: ...


@runtime_checkable
class SkillLibrary(SkillCatalog, Protocol):
    """A catalog of installed skills (agentic.skills.InstalledSkills): it can also read a
    skill's bundled files and write the skill block for an agent's instructions."""

    def resource(self, name: str, path: str) -> str: ...

    def prompt_for(self, agent: "ResolvedAgent") -> str: ...


@dataclass(frozen=True)
class ToolContext:
    tree: Path  # the run's working tree: tools read and write only here
    guard: PathGuard
    sandbox: Sandbox | None = None
    codeintel: CodeIntel | None = None
    skills: SkillCatalog | None = None
    allow_unisolated: bool = False  # run_shell without an isolated sandbox (tests, dev)
    shell_timeout_s: float = 600.0
    # what tool output is scanned with (M1-41, DF-23); None = block, default detectors
    secret_scanner: SecretScanner | None = None


class Refusal(str):
    """A refusal written by the framework. Its TYPE marks it - the one tool result
    not labelled as untrusted data (M1-42) - so file text that merely starts with
    'REFUSED:' cannot pass for one."""


def refused(reason: str) -> str:
    """What a refused call returns to the model: it can read the reason and try
    something else (a ModelRetry would count against the run's retry limit)."""
    return Refusal(f"REFUSED: {reason}")


def guarded[F: Callable](tool: F) -> F:
    """`tool`, returning a refusal instead of raising on a bad argument - an exception
    would end the whole run, a refusal lets the model read why and try again."""

    if inspect.iscoroutinefunction(tool):

        @functools.wraps(tool)
        async def run_async(*args, **kwargs):
            try:
                return await tool(*args, **kwargs)
            except TOOL_ERRORS as error:
                return refused(f"{type(error).__name__}: {error}")

        return run_async  # type: ignore[return-value]

    @functools.wraps(tool)
    def run(*args, **kwargs):
        try:
            return tool(*args, **kwargs)
        except TOOL_ERRORS as error:
            return refused(f"{type(error).__name__}: {error}")

    return run  # type: ignore[return-value]
