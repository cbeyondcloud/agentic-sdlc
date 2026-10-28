"""Tests for domain-graph access over Zyroflo's `tools.graph` (M2-28).

The estate is Zyroflo's `pantry` fixture (EXPECTED.md is its answer key): pantry-api
publishes `restock-events-v2`, restock-worker consumes it, calls pantry-api's items
endpoint and owns the `restock_events` table.
"""

import asyncio
import os
import shutil

import pytest
from tools.graph import build as zyroflo_build

from sdlc_commons import domain_graph
from sdlc_commons.domain_graph import DomainGraph, DomainGraphError

ROUTES_DIFF = """\
diff --git a/src/pantry_api/routes.py b/src/pantry_api/routes.py
--- a/src/pantry_api/routes.py
+++ b/src/pantry_api/routes.py
@@ -10,1 +10,1 @@
-router = APIRouter(prefix="/pantry/v1")
+router = APIRouter(prefix="/pantry/v1", tags=["items"])
"""


def files_of(result: dict) -> list[tuple[str, str]]:
    return [(row["component"], row["path"]) for row in result["files"]]


# ── build ────────────────────────────────────────────────────────────────────


def test_the_build_reports_every_component_of_the_estate(pantry):
    estate, graph = pantry

    assert (graph / "graph.jsonl").is_file()
    assert DomainGraph(graph).components() == {
        "deploy-compose": "deploy-compose",
        "label-printer": "label-printer",
        "ops-runbook": "ops-runbook",
        "pantry-api": "pantry-api",
        "pantry-shared": "pantry-shared",
        "restock-worker": "restock-worker",
    }


def test_without_an_installer_the_build_is_refused_naming_the_zyroflo_gap(tmp_path, monkeypatch):
    def missing():
        raise FileNotFoundError("sdlc-init.py")

    monkeypatch.setattr(zyroflo_build, "load_installer", missing)
    monkeypatch.setattr(domain_graph, "zyroflo_checkout", lambda: None)

    with pytest.raises(DomainGraphError, match="Z7"):
        domain_graph.build_graph(tmp_path / "workspace.toml", tmp_path / "graph")
    assert zyroflo_build.load_installer is missing  # put back as found


def test_a_named_checkout_without_the_installer_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(zyroflo_build, "load_installer", _raise_missing)

    with pytest.raises(DomainGraphError, match="sdlc-init.py"):
        domain_graph.build_graph(tmp_path / "w.toml", tmp_path / "g", harness=tmp_path)


def _raise_missing():
    raise FileNotFoundError("sdlc-init.py")


def test_a_workspace_zyroflo_cannot_read_is_refused_in_its_words(tmp_path, pantry):
    (tmp_path / "workspace.toml").write_text("schema_version = 99\n", encoding="utf-8")

    with pytest.raises(DomainGraphError):
        domain_graph.build_graph(tmp_path / "workspace.toml", tmp_path / "graph")


# ── impact ───────────────────────────────────────────────────────────────────


def test_impact_of_the_publisher_reaches_the_consumer_in_another_repo(pantry):
    _, graph = pantry

    result = DomainGraph(graph).impact("pantry-api", "src/pantry_api/publisher.py")

    reached = files_of(result)
    assert ("restock-worker", "src/restock_worker/consumer.py") in reached
    assert ("restock-worker", "migrations/0001_create_restock_events.sql") in reached
    services = {row["id"]: row["hops"] for row in result["services"]}
    assert services["svc:restock-worker@restock-worker"] == 1
    assert result["seed"]["mode"] == "provenance"


def test_impact_of_a_diff_reaches_the_client_that_calls_the_endpoint(pantry):
    _, graph = pantry
    intel = DomainGraph(graph)

    targets = intel.parse_diff(ROUTES_DIFF)
    result = intel.impact_of_targets("pantry-api", sorted(targets.items()))

    assert targets == {"src/pantry_api/routes.py": [(10, 10)]}
    assert ("restock-worker", "src/restock_worker/client.py") in files_of(result)


def test_the_graph_knows_which_files_are_tests(pantry):
    _, graph = pantry

    assert DomainGraph(graph).test_files() == {("pantry-api", "tests/test_routes.py")}


def test_an_unknown_component_is_refused_with_the_known_ones(pantry):
    _, graph = pantry

    with pytest.raises(DomainGraphError, match="restock-worker"):
        DomainGraph(graph).impact("billing", "src/app.py")


def test_status_counts_the_graph(pantry):
    _, graph = pantry

    status = DomainGraph(graph).status()

    assert status["workspace"] == "pantry"
    assert status["nodes"]["svc"] == 3 and status["nodes"]["topic"] == 1


def test_a_context_pack_is_budgeted_markdown(pantry):
    _, graph = pantry

    pack = DomainGraph(graph).context_pack("pantry-api", path="src/pantry_api/publisher.py")

    assert pack["format"] == "md" and "restock" in pack["markdown"]


# ── refusals ─────────────────────────────────────────────────────────────────


def test_a_missing_graph_is_refused(tmp_path):
    with pytest.raises(DomainGraphError, match="graph.jsonl"):
        DomainGraph(tmp_path).status()


def test_a_damaged_graph_is_refused_by_line(tmp_path, pantry):
    _, graph = pantry
    damaged = tmp_path / "graph"
    shutil.copytree(graph, damaged)
    with (damaged / "graph.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"id": "svc:half\n')

    with pytest.raises(DomainGraphError, match=r"graph\.jsonl:\d+"):
        DomainGraph(damaged).impact("pantry-api", "src/pantry_api/publisher.py")


# ── serving and storing ──────────────────────────────────────────────────────


def test_the_mcp_server_serves_zyroflos_eight_tools(pantry):
    _, graph = pantry

    server = domain_graph.mcp_server(graph)
    names = [tool.name for tool in asyncio.run(server.list_tools())]

    assert names == list(domain_graph.MCP_TOOLS)
    assert "impact" in names and "context_pack" in names


def test_the_mcp_server_is_refused_without_fastmcp(pantry, monkeypatch):
    from tools.graph import mcp_server

    _, graph = pantry
    monkeypatch.setattr(mcp_server, "FastMCP", None)

    with pytest.raises(DomainGraphError, match="fastmcp"):
        domain_graph.mcp_server(graph)


def test_the_graph_loads_into_a_sqlite_store(pantry, tmp_path):
    _, graph = pantry

    store, counts = domain_graph.open_store(graph, "sqlite", path=str(tmp_path / "g.db"))
    try:
        assert counts["nodes"] > 0 and counts["edges"] > 0 and counts["stubs"] == 0
        assert store.get_node("svc:pantry-api@pantry-api") is not None
    finally:
        store.close()


@pytest.mark.skipif(
    not os.environ.get("NEO4J_URI"),
    reason="no neo4j server: set NEO4J_URI (and NEO4J_USER, NEO4J_PASSWORD) to run the neo4j "
    "path - neo4j is optional and never installed on a developer machine",
)
def test_the_graph_loads_into_neo4j(pantry):
    _, graph = pantry

    store, counts = domain_graph.open_store(graph, "neo4j")
    try:
        assert counts["nodes"] > 0
        assert store.get_node("svc:pantry-api@pantry-api") is not None
    finally:
        store.close()


def test_an_unknown_store_backend_is_refused(pantry):
    _, graph = pantry

    with pytest.raises(DomainGraphError, match="sqlite"):
        domain_graph.open_store(graph, "kuzu")
