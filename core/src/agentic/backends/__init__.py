"""Where an agent step runs: `native` (Pydantic AI over TokenLean) or an agent CLI such
as `claude-code`, behind one provider-agnostic protocol (M1-27..M1-29)."""

import importlib.util

from agentic.backends.capabilities import (
    CAPABILITIES,
    Action,
    BackendCapabilities,
    Issue,
    backend_issues,
)

#: Backend name -> the optional package it needs (an extra of agentic-core).
OPTIONAL = {"claude-code": "claude_agent_sdk"}


def available() -> frozenset[str]:
    """The backends this installation can run: native always, others when their extra
    is installed."""
    found = {"native"}
    found |= {name for name, module in OPTIONAL.items() if importlib.util.find_spec(module)}
    return frozenset(found)


__all__ = [
    "CAPABILITIES",
    "OPTIONAL",
    "Action",
    "BackendCapabilities",
    "Issue",
    "available",
    "backend_issues",
]
