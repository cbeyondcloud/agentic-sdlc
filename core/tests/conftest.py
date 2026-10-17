"""Shared pytest fixtures for the core package tests."""

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    """Folder holding sample agent, pipeline, policy and handoff files."""
    return FIXTURES_DIR
