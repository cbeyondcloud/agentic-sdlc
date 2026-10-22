"""Where an agent step runs: `native` (Pydantic AI over TokenLean) or an agent CLI such
as `claude-code`, behind one provider-agnostic protocol (M1-27..M1-29)."""

import importlib.util
import os
import shutil

from agentic.backends.capabilities import (
    CAPABILITIES,
    Action,
    BackendCapabilities,
    Issue,
    backend_issues,
)

#: Backend name -> the optional package it needs (an extra of agentic-core).
OPTIONAL = {"claude-code": "claude_agent_sdk", "codex": "tomli_w", "opencode": "acp"}
#: Backend name -> the external CLI it drives, found on PATH; POSIX only (on Windows,
#: inside WSL2 - D38). Its pinned version is checked when a step starts.
EXTERNAL = {"codex": "codex"}


def available() -> frozenset[str]:
    """The backends this installation can run: native always, others when their extra
    is installed (and their CLI is on PATH, outside native Windows)."""
    found = {"native"}
    found |= {name for name, module in OPTIONAL.items() if importlib.util.find_spec(module)}
    for name, program in EXTERNAL.items():
        if os.name == "nt" or shutil.which(program) is None:
            found.discard(name)
    return frozenset(found)


__all__ = [
    "CAPABILITIES",
    "EXTERNAL",
    "OPTIONAL",
    "Action",
    "BackendCapabilities",
    "Issue",
    "available",
    "backend_issues",
]
