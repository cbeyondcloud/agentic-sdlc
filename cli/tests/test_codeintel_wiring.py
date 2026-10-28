"""Which code intelligence a run's agents get, chosen by the repo's `codeintel.adapter`
(M1-35, M2-43): code-review-graph, the domain graph alongside it, or none."""

import shutil
from pathlib import Path

import pytest

from agentic.codeintel import Candidates, CodeIntelError, Combined
from agentic_cli import codeintel
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from sdlc_commons import domain_graph
from sdlc_commons.domain_graph import DomainGraphError
from sdlc_commons.graph_codeintel import GraphCandidates


@pytest.fixture(scope="module")
def estate(tmp_path_factory) -> Path:
    """Zyroflo's pantry estate with its domain graph built into the estate's
    `.agentic/local/graph` (the default folder): the repo is the whole estate."""
    if not domain_graph.installer_available():
        pytest.skip("no Zyroflo installer to build the graph with (Zyroflo PR Z7)")
    root = tmp_path_factory.mktemp("cli-estate") / "pantry"
    shutil.copytree(domain_graph.fixture_estate("pantry"), root)
    domain_graph.build_graph(root / "workspace.toml", root / ".agentic" / "local" / "graph")
    return root


def settings(**section):
    return codeintel.codeintel_settings({"codeintel": section} if section else {})


# ── settings ─────────────────────────────────────────────────────────────────


def test_no_section_means_code_review_graph():
    assert settings().adapter == "code-review-graph"


@pytest.mark.parametrize(
    "config", [{"codeintel": {"adapter": "graphify"}}, {"codeintel": "domain-graph"}]
)
def test_an_invalid_section_is_refused_as_invalid(config):
    with pytest.raises(CliError) as refused:
        codeintel.codeintel_settings(config)
    assert refused.value.code == ExitCode.INVALID and "codeintel" in str(refused.value)


@pytest.mark.parametrize(
    ("section", "where", "hint"),
    [
        ({"adapter": "graphify"}, "codeintel.adapter", "code-review-graph, domain-graph or none"),
        ("none", "codeintel", "a mapping"),
        ({"adapter": "none", "colour": "red"}, "codeintel.colour", "adapter, graph, component"),
        ({"adapter": "domain-graph", "graph": ""}, "codeintel.graph", "domain graph's folder"),
        ({"adapter": "domain-graph", "component": ""}, "codeintel.component", "component"),
    ],
)
def test_each_invalid_section_gets_its_own_hint(section, where, hint):
    # DF-464: the adapter hint was shown for every error, even a misspelt key
    with pytest.raises(CliError) as refused:
        codeintel.codeintel_settings({"codeintel": section})
    assert f"{where}:" in str(refused.value) and hint in refused.value.hint
    if where != "codeintel.adapter":
        assert "code-review-graph, domain-graph or none" not in refused.value.hint


def test_the_graph_folder_is_read_against_the_repo(tmp_path):
    assert codeintel.graph_dir(tmp_path, settings()) == tmp_path / ".agentic" / "local" / "graph"
    assert codeintel.graph_dir(tmp_path, settings(graph="../g")) == tmp_path / ".." / "g"
    absolute = tmp_path / "elsewhere"
    assert codeintel.graph_dir(tmp_path, settings(graph=str(absolute))) == absolute
    home = codeintel.graph_dir(tmp_path, settings(graph="~/graphs/estate"))
    assert home == Path.home() / "graphs" / "estate"


# ── the factory ──────────────────────────────────────────────────────────────


def test_adapter_none_gives_agents_no_code_intelligence(tmp_path):
    assert codeintel.codeintel_for(tmp_path, settings(adapter="none"), crg_available=True) is None


def test_code_review_graph_needs_its_extra(tmp_path):
    assert codeintel.codeintel_for(tmp_path, settings(), crg_available=False) is None
    make = codeintel.codeintel_for(tmp_path, settings(), crg_available=True)
    assert make(tmp_path / "tree", tmp_path / "data").name == "code-review-graph"


def test_the_domain_graph_answers_in_cores_types(estate, tmp_path):
    chosen = settings(adapter="domain-graph")

    make = codeintel.codeintel_for(estate, chosen, crg_available=False)
    intel = make(estate, tmp_path / "data")
    found = intel.dependents("pantry-api/src/pantry_api/publisher.py")

    assert intel.name == "domain-graph" and "dependents" in intel.supports
    assert isinstance(found, Candidates) and found.source == "domain-graph"
    assert "restock-worker/src/restock_worker/consumer.py" in found.files
    assert "restock-worker (1 hop)" in found.note


def test_the_domain_graph_answers_alongside_code_review_graph(estate, tmp_path):
    chosen = settings(adapter="domain-graph")

    intel = codeintel.codeintel_for(estate, chosen, crg_available=True)(estate, tmp_path / "d")

    assert isinstance(intel, Combined)
    assert intel.name == "code-review-graph + domain-graph"


def test_a_component_setting_reaches_the_adapter(estate, tmp_path):
    tree = tmp_path / "pantry-api"
    shutil.copytree(estate / "pantry-api", tree)
    chosen = settings(adapter="domain-graph", graph=str(estate / ".agentic/local/graph"))
    chosen = chosen.model_copy(update={"component": "pantry-api"})

    intel = codeintel.codeintel_for(tmp_path, chosen, crg_available=False)(tree, tmp_path / "d")
    found = intel.dependents("src/pantry_api/publisher.py")

    assert "src/pantry_api/routes.py" in found.files
    assert "restock-worker/src/restock_worker/consumer.py" in found.note


def test_domain_graph_without_a_graph_warns_and_keeps_crg(tmp_path):
    chosen = settings(adapter="domain-graph")

    warning = codeintel.codeintel_warning(tmp_path, chosen, crg_available=True)
    make = codeintel.codeintel_for(tmp_path, chosen, crg_available=True)

    assert warning is not None and "domain graph" in warning
    assert str(tmp_path / ".agentic" / "local" / "graph") in warning
    assert "code-review-graph only" in warning
    assert make(tmp_path / "tree", tmp_path / "data").name == "code-review-graph"


def test_domain_graph_without_a_graph_or_the_extra_gives_none(tmp_path):
    chosen = settings(adapter="domain-graph")

    warning = codeintel.codeintel_warning(tmp_path, chosen, crg_available=False)

    assert "without code intelligence tools" in warning
    assert codeintel.codeintel_for(tmp_path, chosen, crg_available=False) is None


def test_warnings_for_the_other_adapters(tmp_path, estate):
    off = settings(adapter="none")
    assert codeintel.codeintel_warning(tmp_path, off, crg_available=False) is None
    missing = codeintel.codeintel_warning(tmp_path, settings(), crg_available=False)
    assert "agentic-core[codeintel]" in missing and "search" in missing
    assert codeintel.codeintel_warning(tmp_path, settings(), crg_available=True) is None
    built = settings(adapter="domain-graph")
    assert codeintel.codeintel_warning(estate, built, crg_available=False) is None


# ── the boundary ─────────────────────────────────────────────────────────────


class Inner:
    name = "domain-graph"
    supports = frozenset({"dependents", "impact", "tests_for", "overview"})

    def dependents(self, path):
        return GraphCandidates(("a.py",), "domain-graph", "note")

    def impact(self, diff):
        raise DomainGraphError("graph.jsonl:3 damaged")

    def tests_for(self, path):
        raise DomainGraphError("component 'x' is not in this graph")

    def overview(self):
        raise DomainGraphError("no graph")


def test_an_imported_export_of_another_tool_version_is_a_warning(tmp_path, monkeypatch):
    # M2-44: every run would try the seed, be refused, and build from scratch
    seed = tmp_path / ".agentic" / "local" / "index-export"
    seed.mkdir(parents=True)
    (seed / "manifest.json").write_text('{"tool_version": "2.3.1"}', encoding="utf-8")
    monkeypatch.setattr(codeintel, "installed_tool_version", lambda: "2.3.9")

    warning = codeintel.codeintel_warning(tmp_path, settings(), crg_available=True)

    assert "2.3.1" in warning and "2.3.9" in warning and "agentic index import" in warning
    monkeypatch.setattr(codeintel, "installed_tool_version", lambda: "2.3.1")
    assert codeintel.codeintel_warning(tmp_path, settings(), crg_available=True) is None


def test_a_commons_error_becomes_a_codeintel_error():
    intel = codeintel.GraphIntel(Inner())

    assert intel.dependents("m.py") == Candidates(("a.py",), source="domain-graph", note="note")
    with pytest.raises(CodeIntelError, match="graph.jsonl:3"):
        intel.impact("diff")
    with pytest.raises(CodeIntelError, match="not in this graph"):
        intel.tests_for("m.py")
    with pytest.raises(CodeIntelError, match="no graph"):
        intel.overview()
