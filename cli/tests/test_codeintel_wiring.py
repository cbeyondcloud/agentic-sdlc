"""Which code intelligence a run's agents get, chosen by the repo's `codeintel.adapter`
(M1-35, M2-43): code-review-graph, the domain graph alongside it, or none."""

import shutil
from pathlib import Path

import pytest
from cli_helpers import skip_unless_graph_required

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
        skip_unless_graph_required("no Zyroflo installer to build the graph with (Zyroflo PR Z7)")
    root = tmp_path_factory.mktemp("cli-estate") / "pantry"
    shutil.copytree(domain_graph.fixture_estate("pantry"), root)
    domain_graph.build_graph(root / "workspace.toml", root / ".agentic" / "local" / "graph")
    return root


def settings(**section):
    return codeintel.codeintel_settings({"codeintel": section} if section else {})


# ── settings ─────────────────────────────────────────────────────────────────


def test_no_section_means_routed_by_language():
    assert settings().adapter == "auto"


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
        ({"adapter": "graphify"}, "codeintel.adapter", "codebase-memory, domain-graph or none"),
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
        assert "codebase-memory, domain-graph or none" not in refused.value.hint


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


def test_missing_installer_fails_where_ci_requires_the_graph():
    """As commons/tests/graph_ci.py: under scripts/check.py the estate tests run (DF-463)."""
    with pytest.raises(pytest.fail.Exception, match="no installer"):
        skip_unless_graph_required("no installer", environ={"AGENTIC_REQUIRE_ZYROFLO_GRAPH": "1"})
    with pytest.raises(pytest.skip.Exception, match="no installer"):
        skip_unless_graph_required("no installer", environ={})


# ── JVM repos (M3-27) ────────────────────────────────────────────────────────


@pytest.fixture
def jvm_repo(tmp_path) -> Path:
    root = tmp_path / "jvm" / "shop"  # two levels down: tmp_path itself is no JVM repo
    for rel, text in {
        "pom.xml": "<project/>",
        "src/main/java/a/Cart.java": "package a;\nclass Cart {}\n",
        "src/test/java/a/CartTest.java": "package a;\nclass CartTest {}\n",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


PINNED_MISSING = codeintel.Backend(None, pinned=True, why="not installed; run `agentic init`")


@pytest.mark.parametrize(
    ("adapter", "jvm", "route"),
    [
        ("auto", True, "codebase-memory"),
        ("auto", False, "code-review-graph"),
        ("domain-graph", True, "codebase-memory"),
        ("code-review-graph", True, "code-review-graph"),
        ("codebase-memory", False, "codebase-memory"),
        ("none", True, None),
    ],
)
def test_the_symbol_adapter_is_routed_by_language_unless_overridden(
    adapter, jvm, route, jvm_repo, tmp_path
):
    repo = jvm_repo if jvm else tmp_path
    assert codeintel.symbol_route(repo, settings(adapter=adapter)) == route


def test_a_jvm_repo_gets_the_conventions_first_then_code_review_graph(jvm_repo, tmp_path):
    intel = codeintel.codeintel_for(jvm_repo, settings(), crg_available=True)(jvm_repo, tmp_path)
    assert intel.name == "jvm-conventions + code-review-graph"


def test_an_explicit_code_review_graph_keeps_the_conventions_on_a_jvm_repo(jvm_repo, tmp_path):
    chosen = settings(adapter="code-review-graph")
    backend = codeintel.Backend(tmp_path / "cbm", pinned=True)
    make = codeintel.codeintel_for(jvm_repo, chosen, crg_available=True, backend=backend)
    assert make(jvm_repo, tmp_path).name == "jvm-conventions + code-review-graph"


def test_a_located_backend_answers_for_a_jvm_repo(jvm_repo, tmp_path):
    backend = codeintel.Backend(tmp_path / "cbm", pinned=True)
    make = codeintel.codeintel_for(jvm_repo, settings(), crg_available=True, backend=backend)
    intel = make(jvm_repo, tmp_path / "data")
    assert intel.name == "jvm-conventions + codebase-memory-mcp"
    assert intel.adapters[1].command == [str(tmp_path / "cbm")]
    assert intel.adapters[1].data == tmp_path / "data" / "codebase-memory"


def test_a_python_repo_never_gets_the_backend(tmp_path):
    backend = codeintel.Backend(tmp_path / "cbm", pinned=True)
    make = codeintel.codeintel_for(tmp_path, settings(), crg_available=True, backend=backend)
    assert make(tmp_path / "tree", tmp_path / "data").name == "code-review-graph"


def test_a_jvm_repo_without_the_extra_still_gets_its_tests(jvm_repo, tmp_path):
    intel = codeintel.codeintel_for(jvm_repo, settings(), crg_available=False)(jvm_repo, tmp_path)
    found = intel.tests_for("src/main/java/a/Cart.java")
    assert found.files == ("src/test/java/a/CartTest.java",)
    assert found.source == "jvm-conventions"


def test_a_failing_backend_never_fails_the_answer(jvm_repo, tmp_path):
    backend = codeintel.Backend(tmp_path / "no-such-binary", pinned=True)
    make = codeintel.codeintel_for(jvm_repo, settings(), crg_available=False, backend=backend)
    found = make(jvm_repo, tmp_path / "data").tests_for("src/main/java/a/Cart.java")
    assert found.files == ("src/test/java/a/CartTest.java",)
    assert "codebase-memory-mcp" in found.note and "could not run" in found.note


def test_a_pinned_backend_missing_here_is_a_warning_naming_the_fallback(jvm_repo):
    warning = codeintel.codeintel_warning(
        jvm_repo, settings(), crg_available=True, backend=PINNED_MISSING
    )
    assert "codebase-memory-mcp is not available" in warning
    assert "not installed; run `agentic init`" in warning
    assert "code-review-graph and the Maven/Gradle test conventions" in warning


def test_an_unpinned_backend_is_no_warning_under_auto_but_is_when_asked_for(jvm_repo):
    unpinned = codeintel.Backend(None, pinned=False, why="org policy pins no codebase-memory-mcp")
    quiet = codeintel.codeintel_warning(jvm_repo, settings(), crg_available=True, backend=unpinned)
    assert quiet is None  # the org has not approved it: the conventions are the design
    chosen = settings(adapter="codebase-memory")
    asked = codeintel.codeintel_warning(jvm_repo, chosen, crg_available=False, backend=unpinned)
    assert "org policy pins no codebase-memory-mcp" in asked
    assert "the Maven/Gradle test conventions only" in asked


def test_a_located_backend_needs_no_extra_and_warns_nothing(jvm_repo, tmp_path):
    backend = codeintel.Backend(tmp_path / "cbm", pinned=True)
    assert (
        codeintel.codeintel_warning(jvm_repo, settings(), crg_available=False, backend=backend)
        is None
    )


def test_a_jvm_repo_without_the_extra_says_the_conventions_still_answer(jvm_repo):
    warning = codeintel.codeintel_warning(jvm_repo, settings(), crg_available=False)
    assert "agentic-core[codeintel]" in warning and "Maven/Gradle test conventions" in warning


def test_the_backend_is_located_in_the_verified_tool_cache_only(tmp_path):
    class Locator:
        pins = {"codebase-memory-mcp": _Pin({"linux-x86_64": _Artefact(signed=False)})}
        platform = "linux-x86_64"

        def locate(self, name):
            assert name == codeintel.BACKEND_TOOL
            return None, "not installed"

    assert codeintel.locate_backend(Locator()) == codeintel.Backend(None, True, "not installed")
    Locator.pins = {}
    assert codeintel.locate_backend(Locator()).pinned is False


def test_codeintel_status_explains_the_choice(jvm_repo, tmp_path):
    status, detail, fix = codeintel.codeintel_status(
        jvm_repo, settings(), crg_available=True, backend=PINNED_MISSING
    )
    assert status == "warn" and "codebase-memory-mcp is not available" in detail and fix
    status, detail, _ = codeintel.codeintel_status(
        jvm_repo, settings(), crg_available=True, backend=codeintel.Backend(None, False, "x")
    )
    assert status == "info" and "Maven/Gradle test conventions" in detail
    status, detail, _ = codeintel.codeintel_status(tmp_path, settings(), crg_available=True)
    assert status == "ok" and detail == "code-review-graph"
    status, detail, _ = codeintel.codeintel_status(
        tmp_path, settings(adapter="none"), crg_available=True
    )
    assert status == "info" and "none" in detail


def test_the_backend_is_looked_up_only_when_the_repo_routes_to_it(jvm_repo, tmp_path):
    def never():
        raise AssertionError("looked up for a repo that does not use it")

    assert codeintel.backend_for(tmp_path, settings(), never) == codeintel.NO_BACKEND
    assert codeintel.backend_for(jvm_repo, settings(adapter="none"), never) == codeintel.NO_BACKEND
    chosen = settings(adapter="code-review-graph")
    assert codeintel.backend_for(jvm_repo, chosen, never) == codeintel.NO_BACKEND


def test_a_failing_lookup_is_no_backend_with_the_reason(jvm_repo):
    def broken():
        raise RuntimeError("policy cache unreadable")

    found = codeintel.backend_for(jvm_repo, settings(), broken)
    assert found.path is None and "policy cache unreadable" in found.why


class _Pin:
    def __init__(self, assets):
        self.assets = assets


class _Artefact:
    def __init__(self, signed: bool):
        self.file, self.sha256, self.signed = "cbm.zip", "0" * 64, signed


@pytest.mark.parametrize(
    ("platform", "assets", "permanent"),
    [
        ("windows-x86_64", {"windows-x86_64": _Artefact(signed=False)}, True),
        ("windows-x86_64", {"windows-x86_64": _Artefact(signed=True)}, False),
        ("darwin-aarch64", {"linux-x86_64": _Artefact(signed=False)}, True),
        ("darwin-aarch64", {"darwin-aarch64": _Artefact(signed=False)}, False),
    ],
)
def test_a_build_this_machine_can_never_run_is_permanent(platform, assets, permanent):
    class Locator:
        pins = {"codebase-memory-mcp": _Pin(assets)}

        def __init__(self):
            self.platform = platform

        def locate(self, name):
            return None, "not here"

    assert codeintel.locate_backend(Locator()).permanent is permanent


def test_a_permanent_gap_is_info_not_a_warning_unless_asked_for(jvm_repo):
    gap = codeintel.Backend(None, pinned=True, why="no signed Windows build", permanent=True)
    assert (
        codeintel.codeintel_warning(jvm_repo, settings(), crg_available=True, backend=gap) is None
    )
    status, detail, _ = codeintel.codeintel_status(
        jvm_repo, settings(), crg_available=True, backend=gap
    )
    assert status == "info" and "no signed Windows build" in detail
    asked = settings(adapter="codebase-memory")
    assert codeintel.codeintel_warning(jvm_repo, asked, crg_available=True, backend=gap)
