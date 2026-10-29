"""Smoke test: proves the workspace is wired up and the core package imports."""

from importlib.metadata import version
from pathlib import Path

import agentic


def test_version_exposed():
    """One version source (M3-21): the package metadata, never a second literal."""
    assert agentic.__version__ == version("agentic-core")
    assert '__version__ = "' not in Path(agentic.__file__).read_text(encoding="utf-8")
