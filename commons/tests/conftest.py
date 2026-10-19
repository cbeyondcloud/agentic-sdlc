"""Shared test setup for the commons adapters."""

import pytest

from sdlc_commons import ledger


@pytest.fixture(autouse=True)
def fresh_run_identities(monkeypatch):
    """Each test starts as a new process would: no run identities held."""
    monkeypatch.setattr(ledger, "_IDENTITIES", {})
