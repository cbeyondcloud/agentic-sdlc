"""Tests for the domain-graph code-intelligence adapter (M2-43) on Zyroflo's pantry estate.

Two ways a run's tree meets the graph: the tree is one component (a repository of the
estate: `component` is set), or the tree is the whole workspace (a monorepo estate).
"""

import json
import shutil

import pytest

from sdlc_commons.domain_graph import DomainGraph, DomainGraphError
from sdlc_commons.graph_codeintel import DomainGraphCodeIntel, GraphCandidates

PUBLISHER = "src/pantry_api/publisher.py"
ROUTES_DIFF = """\
diff --git a/src/pantry_api/routes.py b/src/pantry_api/routes.py
--- a/src/pantry_api/routes.py
+++ b/src/pantry_api/routes.py
@@ -10,1 +10,1 @@
-router = APIRouter(prefix="/pantry/v1")
+router = APIRouter(prefix="/pantry/v1", tags=["items"])
"""


@pytest.fixture
def repo_intel(pantry, tmp_path):
    """The run's tree is the pantry-api repository."""
    estate, graph = pantry
    tree = tmp_path / "pantry-api"
    shutil.copytree(estate / "pantry-api", tree)
    return DomainGraphCodeIntel(DomainGraph(graph), tree, component="pantry-api")


@pytest.fixture
def estate_intel(pantry):
    """The run's tree is the whole estate."""
    estate, graph = pantry
    return DomainGraphCodeIntel(DomainGraph(graph), estate)


def test_it_declares_what_it_answers(repo_intel):
    assert repo_intel.name == "domain-graph"
    assert repo_intel.supports == {"dependents", "impact", "tests_for", "overview"}


def test_component_mode_puts_other_repos_in_the_note(repo_intel):
    found = repo_intel.dependents(PUBLISHER)

    assert isinstance(found, GraphCandidates) and found.source == "domain-graph"
    assert "src/pantry_api/routes.py" in found.files
    assert "tests/test_routes.py" in found.files
    assert PUBLISHER not in found.files
    assert "restock-worker/src/restock_worker/consumer.py" in found.note
    assert "restock-worker/migrations/0001_create_restock_events.sql" in found.note
    assert "restock-worker (1 hop)" in found.note


def test_component_mode_lists_only_files_that_exist_in_the_tree(repo_intel):
    found = repo_intel.dependents(PUBLISHER)

    # the graph also cites pantry-api's view of the compose file, which lives in
    # deploy-compose: no such file in this tree, so it is no candidate here
    assert "docker-compose.yml" not in found.files
    assert all((repo_intel.tree / f).is_file() for f in found.files)


def test_workspace_mode_lists_other_components_as_tree_files(estate_intel):
    found = estate_intel.dependents("pantry-api/" + PUBLISHER)

    assert "restock-worker/src/restock_worker/consumer.py" in found.files
    assert "pantry-api/src/pantry_api/routes.py" in found.files
    assert "deploy-compose/docker-compose.yml" in found.files
    assert "restock-worker (1 hop)" in found.note


def test_impact_of_a_diff_reaches_the_client_in_another_repo(repo_intel, estate_intel):
    in_repo = repo_intel.impact(ROUTES_DIFF)
    estate_diff = ROUTES_DIFF.replace("src/pantry_api", "pantry-api/src/pantry_api")
    in_estate = estate_intel.impact(estate_diff)

    assert "restock-worker/src/restock_worker/client.py" in in_repo.note
    assert "src/pantry_api/routes.py" not in in_repo.files  # the change itself
    assert "restock-worker/src/restock_worker/client.py" in in_estate.files


def test_a_diff_touching_no_component_file_answers_empty(estate_intel):
    found = estate_intel.impact("--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n")

    assert found.files == () and "no file" in found.note


def test_tests_for_names_the_tests_the_graph_links(repo_intel, estate_intel):
    assert repo_intel.tests_for("src/pantry_api/routes.py").files == ("tests/test_routes.py",)
    found = estate_intel.tests_for("pantry-api/src/pantry_api/routes.py")
    assert found.files == ("pantry-api/tests/test_routes.py",)


def test_a_file_the_graph_cites_nothing_in_gets_no_directory_guess(repo_intel):
    found = repo_intel.dependents("src/pantry_api/settings.py")

    assert found.files == ()
    assert "cites no" in found.note


def test_a_path_in_no_component_answers_empty(estate_intel):
    found = estate_intel.dependents("README.md")

    assert found.files == () and "no component" in found.note


def test_overview_names_the_workspace_and_its_components(repo_intel):
    text = repo_intel.overview()

    assert "pantry" in text and "restock-worker" in text
    assert "svc 3" in text and "this tree is component pantry-api" in text


def test_graph_paths_outside_the_tree_are_never_listed(pantry, tmp_path):
    estate, graph = pantry
    forged = tmp_path / "graph"
    shutil.copytree(graph, forged)
    rows = (forged / "graph.jsonl").read_text(encoding="utf-8").splitlines()
    evil = []
    for raw in rows:
        row = json.loads(raw)
        if row.get("source_file") == "src/pantry_api/routes.py":
            for bad in ("../outside.py", "/etc/passwd", ".git/config", "C:/Windows/win.ini"):
                evil.append(json.dumps({**row, "source_file": bad, "line": row["line"] + 1}))
    (forged / "graph.jsonl").write_text("\n".join(rows + evil) + "\n", encoding="utf-8")
    tree = tmp_path / "tree"
    shutil.copytree(estate / "pantry-api", tree)
    (tmp_path / "outside.py").write_text("x = 1\n", encoding="utf-8")

    found = DomainGraphCodeIntel(DomainGraph(forged), tree, "pantry-api").dependents(PUBLISHER)

    assert "src/pantry_api/routes.py" in found.files
    assert not any(".." in f or f.startswith("/") or ".git" in f or ":" in f for f in found.files)


@pytest.mark.parametrize(
    "alias", ["GIT~1/config", "git~2/x.py", ".git::$INDEX_ALLOCATION/config", ".GIT. /config"]
)
def test_graph_paths_that_can_open_git_are_never_listed(pantry, tmp_path, alias):
    """Windows opens `GIT~1` (8.3 name), `.git::$INDEX_ALLOCATION` and `.git. ` as `.git`
    (final review F8): such a graph row is no candidate, as core's `names_git` refuses."""
    estate, graph = pantry
    forged = tmp_path / "graph"
    shutil.copytree(graph, forged)
    rows = (forged / "graph.jsonl").read_text(encoding="utf-8").splitlines()
    planted = []
    for raw in rows:
        row = json.loads(raw)
        if row.get("source_file") == "src/pantry_api/routes.py":
            planted.append(json.dumps({**row, "source_file": alias, "line": row["line"] + 1}))
    (forged / "graph.jsonl").write_text("\n".join(rows + planted) + "\n", encoding="utf-8")
    tree = tmp_path / "tree"
    shutil.copytree(estate / "pantry-api", tree)
    if ":" not in alias and "." not in alias.split("/")[0]:  # a name every OS can create
        (tree / alias).parent.mkdir(parents=True, exist_ok=True)
        (tree / alias).write_text("x = 1\n", encoding="utf-8")

    found = DomainGraphCodeIntel(DomainGraph(forged), tree, "pantry-api").dependents(PUBLISHER)

    assert "src/pantry_api/routes.py" in found.files
    assert not any("git" in f.lower() for f in found.files)
    assert alias not in found.note


class TwoComponentGraph:
    """Workspace components `a` and `b`: a's changed file cites no fact, so Zyroflo falls
    back to a directory-scope guess for it; b's changed file seeds a real impact."""

    def components(self):
        return {"a": "a", "b": "b"}

    @staticmethod
    def parse_diff(text):
        return {"a/src/settings.py": None, "b/src/routes.py": None}

    def impact_of_targets(self, component, targets):
        if component == "a":
            return {
                "seed": {"fallback": {"basis": "uncited", "path": "src/settings.py"}},
                "files": [{"component": "a", "path": "src/guess.py", "score": 0.8}],
                "services": [],
            }
        return {
            "seed": {"nodes": ["ep:b"]},
            "files": [{"component": "b", "path": "src/client.py", "score": 0.5}],
            "services": [],
        }


def test_a_directory_guess_never_joins_a_multi_component_diff(tmp_path):
    """Final review F2: the guess files of a component whose seed fell back are not
    passed on as impact because another component of the same diff seeded properly."""
    for path in ("a/src/guess.py", "b/src/client.py"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("x = 1\n", encoding="utf-8")

    found = DomainGraphCodeIntel(TwoComponentGraph(), tmp_path).impact("(diff)")

    assert found.files == ("b/src/client.py",)


class RecordingGraph:
    """A graph whose impact records which (component, path) it was asked about."""

    def __init__(self, components):
        self._components, self.asked = components, []

    def components(self):
        return self._components

    def impact(self, component, path, lines=None):
        self.asked.append((component, path))
        return {"seed": {}, "files": [], "services": []}


def test_workspace_mode_picks_the_component_with_the_longest_path(tmp_path):
    graph = RecordingGraph({"estate": ".", "shared": "libs/shared", "deep": "libs/shared/x"})
    intel = DomainGraphCodeIntel(graph, tmp_path)

    intel.dependents("libs/shared/src/a.py")
    intel.dependents("libs\\shared\\x\\b.py")
    intel.dependents("src/main.py")
    intel.dependents("libs/sharedother/c.py")

    assert graph.asked == [
        ("shared", "src/a.py"),
        ("deep", "b.py"),
        ("estate", "src/main.py"),
        ("estate", "libs/sharedother/c.py"),
    ]


def test_an_unknown_component_is_refused(pantry, tmp_path):
    _, graph = pantry

    with pytest.raises(DomainGraphError, match="billing"):
        DomainGraphCodeIntel(DomainGraph(graph), tmp_path, "billing").dependents("src/a.py")
