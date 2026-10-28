"""What a code-intelligence adapter answers (M1-34, spec §10.1).

Answers are candidates: a graph built from syntax suggests what a change touches;
Review and QA verify. Every answer names the adapter that produced it, and an
adapter declares which operations it supports - the agent is offered only those.
"""

from dataclasses import dataclass
from typing import Protocol

OPERATIONS = frozenset({"dependents", "impact", "tests_for", "overview"})


@dataclass(frozen=True)
class Candidates:
    files: tuple[str, ...]  # tree-relative POSIX paths, best first
    source: str  # the adapter that produced them
    note: str = ""


class CodeIntel(Protocol):
    name: str
    supports: frozenset[str]  # a subset of OPERATIONS

    def dependents(self, path: str) -> Candidates: ...

    def impact(self, diff: str) -> Candidates: ...

    def tests_for(self, path: str) -> Candidates: ...

    def overview(self) -> str: ...


class CodeIntelError(Exception):
    """The index could not be built or queried; the message says why."""
