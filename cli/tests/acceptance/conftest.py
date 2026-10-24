"""Fixtures for the acceptance scenarios (cli/tests/acceptance): M1-68..M1-70 in standalone
mode, M2-53 and M2-55 on the org stack."""

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


@pytest.fixture(scope="session")
def org_postgres():
    """The Postgres of the org scenarios, once per session (a skip naming what is missing,
    or a failure where AGENTIC_CP_REQUIRE_PG=1)."""
    from _org_stack import postgres

    server = postgres()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def org(org_postgres, tmp_path):
    """The org stack on a fresh database: the control plane serving tenant acme."""
    from _org_stack import OrgStack

    stack = OrgStack(org_postgres, tmp_path / "org").start()
    try:
        yield stack
    finally:
        stack.stop()
