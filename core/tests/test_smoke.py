"""Smoke test: proves the workspace is wired up and the core package imports."""

import agentic


def test_version_exposed():
    assert agentic.__version__ == "0.1.0"
