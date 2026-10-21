"""Fixtures for the M1 acceptance scenarios (cli/tests/acceptance, M1-68..M1-70)."""

import pytest
from _acceptance_support import Scenario

# the sample repository's own tests run inside the scenarios, never in this suite
collect_ignore = ["sample-repo"]


@pytest.fixture
def scenario(request, tmp_path, monkeypatch, capsys):
    """A fresh world per scenario; the test module's SCENARIO names its cassette."""
    world = Scenario(tmp_path, monkeypatch, capsys, name=request.module.SCENARIO).start()
    yield world
    world.stop()
