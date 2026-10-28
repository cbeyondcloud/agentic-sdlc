"""Skip or fail the domain-graph tests when no Zyroflo installer is found (DF-463)."""

import os
from collections.abc import Mapping

import pytest

#: Set to 1 by scripts/check.py: there a missing installer is a failure, not a skip.
REQUIRE_GRAPH = "AGENTIC_REQUIRE_ZYROFLO_GRAPH"


def skip_unless_required(reason: str, environ: Mapping[str, str] = os.environ) -> None:
    """Skip the test for `reason`, or fail it where CI requires the graph tests to run."""
    if environ.get(REQUIRE_GRAPH, "").strip() == "1":
        pytest.fail(f"{reason} ({REQUIRE_GRAPH}=1)")
    pytest.skip(reason)
