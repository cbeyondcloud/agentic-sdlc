"""Code intelligence for agents: which files depend on a file, what a change affects,
which tests cover it (M1-34..M1-36, spec §10). Answers are candidates."""

from agentic.codeintel.interface import OPERATIONS, Candidates, CodeIntel, CodeIntelError
from agentic.codeintel.tools import MAX_LISTED, codeintel_tools, tree_path

__all__ = [
    "MAX_LISTED",
    "OPERATIONS",
    "Candidates",
    "CodeIntel",
    "CodeIntelError",
    "codeintel_tools",
    "tree_path",
]
