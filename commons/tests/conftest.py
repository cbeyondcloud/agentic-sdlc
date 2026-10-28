"""Shared test setup for the commons adapters."""

import shutil
from pathlib import Path

import pytest
from graph_ci import skip_unless_required

from sdlc_commons import domain_graph, ledger


@pytest.fixture(autouse=True)
def fresh_run_identities(monkeypatch):
    """Each test starts as a new process would: no run identities held."""
    monkeypatch.setattr(ledger, "_IDENTITIES", {})


@pytest.fixture(scope="session")
def pantry(tmp_path_factory) -> tuple[Path, Path]:
    """Zyroflo's `pantry` fixture estate, copied, and its domain graph built:
    (estate root, graph folder). Skips when no Zyroflo installer can be found (Z7),
    or fails where CI requires the graph tests (DF-463)."""
    if not domain_graph.installer_available():
        skip_unless_required(
            "Zyroflo's wheel does not ship sdlc-init.py (Zyroflo PR Z7) and no checkout "
            "of the pinned commit is in uv's git cache - the graph cannot be built here"
        )
    root = tmp_path_factory.mktemp("estate")
    estate = root / "pantry"
    shutil.copytree(domain_graph.fixture_estate("pantry"), estate)
    graph = root / "graph"
    domain_graph.build_graph(estate / "workspace.toml", graph)
    return estate, graph
