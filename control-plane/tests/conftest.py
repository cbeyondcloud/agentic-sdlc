"""Shared fixtures for the control-plane tests."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """Async tests run on asyncio (asyncpg needs it)."""
    return "asyncio"
