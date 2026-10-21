"""M1-81 hardening of the code index (DF-84, DF-88..DF-94): no secret reaches
code-review-graph, a corrupt index heals, parallel calls build once, the query matches
spike S5's measured setup, and `.git` is never a tree path."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from _codeintel_support import git

from agentic.codeintel import CodeIntelError, tree_path
from agentic.codeintel import _crg_query as query
from agentic.codeintel import index as index_module
from agentic.codeintel.crg import CodeReviewGraph
from agentic.codeintel.index import LocalIndex, available

needs_crg = pytest.mark.skipif(not available(), reason="needs the codeintel extra")
SECRET_NAME = "AGENTIC_TEST_" + "TOKEN"  # built from pieces


def index_for(repo: Path, tmp_path: Path, **kwargs) -> LocalIndex:
    return LocalIndex(
        repo, tmp_path / "state" / "index", home_dir=tmp_path / "state" / "crg-home", **kwargs
    )


# ── DF-88: no secret in code-review-graph's or git's environment ───────────────


def test_the_indexer_never_sees_a_secret(fixture_repo, tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_NAME, "s3cr3t-value")
    dump = tmp_path / "env.json"
    cli = tmp_path / "dump_env.py"
    cli.write_text(
        "import json, os, sys\n"
        f"open({str(dump)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
        "print('{}')\n",
        encoding="utf-8",
    )
    index = index_for(fixture_repo, tmp_path, cli=[sys.executable, str(cli)])
    index._run("status", "--json")
    seen = json.loads(dump.read_text(encoding="utf-8"))
    assert SECRET_NAME not in seen
    assert "CRG_DATA_DIR" in seen and "PATH" in {k.upper() for k in seen}
    assert SECRET_NAME not in index._git_env()


# ── DF-89: a corrupt or half-built index is rebuilt, not a permanent refusal ───


@needs_crg
@pytest.mark.parametrize("content", [b"not a database at all", b""])
def test_a_corrupt_index_is_rebuilt(fixture_repo, tmp_path, content):
    index = index_for(fixture_repo, tmp_path)
    index.ensure()
    index.db.write_bytes(content)
    index.ensure()
    assert "pkg/models.py" in index.indexed_files()


@needs_crg
def test_an_index_without_its_tables_is_rebuilt(fixture_repo, tmp_path):
    index = index_for(fixture_repo, tmp_path)
    index.data_dir.mkdir(parents=True)
    sqlite3.connect(index.db).close()  # an empty database: a build that crashed early
    index.ensure()
    assert "pkg/models.py" in index.indexed_files()


# ── DF-92: parallel tool calls build once ──────────────────────────────────────


@needs_crg
def test_parallel_calls_build_once(fixture_repo, tmp_path):
    index = index_for(fixture_repo, tmp_path)
    builds: list[str] = []
    real = index._run

    def slow(command: str, *args: str) -> str:
        if command in ("build", "update"):
            builds.append(command)
            time.sleep(0.5)  # both calls are inside ensure() together
        return real(command, *args)

    index._run = slow
    threads = [threading.Thread(target=index.ensure) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert builds == ["build"]


def test_two_indexes_of_one_data_folder_share_a_lock(tmp_path):
    a = LocalIndex(tmp_path, tmp_path / "data", home_dir=tmp_path / "home")
    b = LocalIndex(tmp_path, tmp_path / "data" / ".", home_dir=tmp_path / "home")
    assert a._lock is b._lock


# ── DF-90: the query as spike S5 measured it ───────────────────────────────────


def make_graph(db: Path, tree: Path) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as con:
        con.execute("create table nodes (file_path text, qualified_name text, kind text)")
        con.execute("create table edges (target_qualified text, kind text)")
        rows = [
            (str(tree / "pkg" / "models.py"), "pkg.models.Model", "Class"),
            (str(tree / "pkg" / "models.py"), "pkg.models.Model.name", "Function"),
            (str(tree / "pkg" / "service.py"), "pkg.service.serve", "Function"),
            (str(tree / "pkg" / "models.py"), "pkg.models", "File"),
        ]
        con.executemany("insert into nodes values (?, ?, ?)", rows)


def test_the_query_opens_a_data_folder_with_uri_characters(tmp_path):
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    db = tmp_path / "state #1 100%" / "graph.db"
    make_graph(db, tree)
    names = query._symbols(db, tree / "pkg" / "models.py")
    assert names == ["pkg.models.Model", "pkg.models.Model.name"]
    assert query._overview(tree, db).startswith("2 files, 4 symbols")


class FakeTools:
    def __init__(self, tree: Path) -> None:
        self.tree, self.calls = tree, []

    def query_graph(self, pattern, target, repo_root=None, max_results=100):
        self.calls.append((pattern, target, max_results))
        return {"results": []}

    def get_impact_radius(self, changed_files=None, max_depth=2, max_results=500, repo_root=None):
        self.calls.append(("impact", tuple(changed_files), max_results))
        names = ["pkg/zeta.py", "pkg/alpha.py", "pkg/mid.py"]  # the tool's own ranking
        return {"impacted_files": [str(self.tree / n) for n in names]}


def test_result_limits_and_order_match_spike_s5(tmp_path):
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    db = tmp_path / "graph.db"
    make_graph(db, tree)
    tools = FakeTools(tree)
    found = query._composed(tools, tree, "pkg/models.py", db)
    limits = {(pattern, target): n for pattern, target, n in tools.calls}
    assert limits[("importers_of", "pkg/models.py")] == 500
    assert limits[("tests_for", "pkg/models.py")] == 500
    assert limits[("tests_for", "pkg.models.Model")] == 200
    assert limits[("impact", ("pkg/models.py",))] == 5000
    assert found == ["pkg/zeta.py", "pkg/alpha.py", "pkg/mid.py"]  # not re-sorted


# ── DF-93: `.git` is never a tree path ─────────────────────────────────────────


@pytest.mark.parametrize("raw", [".git", ".GIT/config", "src/.git", "./.git/HEAD", ".Git"])
def test_git_metadata_is_not_a_tree_path(tmp_path, raw):
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
    assert tree_path(tmp_path, raw) is None


def test_a_name_that_only_starts_with_git_is_a_tree_path(tmp_path):
    assert tree_path(tmp_path, ".github/workflows/ci.yml") == ".github/workflows/ci.yml"
    assert tree_path(tmp_path, ".gitignore") == ".gitignore"


@pytest.mark.parametrize(
    "raw",
    [".git.", ".GIT.", ".git ", ".git. ./config", ".git::$INDEX_ALLOCATION", "GIT~1", "git~2/x"],
)
def test_an_alias_spelling_of_git_is_not_a_tree_path(tmp_path, raw):
    """Windows opens `.git.`, `.git ` and the 8.3 name `GIT~1` as `.git` (final review F1):
    every spelling that can name it is refused, on every OS (the safe side)."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[remote]\n", encoding="utf-8")
    assert tree_path(tmp_path, raw) is None


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 short names are a Windows feature")
def test_the_short_name_windows_gives_git_is_not_a_tree_path(tmp_path):
    (tmp_path / ".git").write_text("gitdir: C:/elsewhere/.git/worktrees/x\n", encoding="utf-8")
    short = next(
        (n for n in ("GIT~1", "GIT~2", "GIT~3") if (tmp_path / n).exists()),
        None,
    )
    if short is None:
        pytest.skip("8.3 short names are off on this volume")
    assert tree_path(tmp_path, short) is None
    assert tree_path(tmp_path, short.lower()) is None


def test_a_link_to_git_is_not_a_tree_path(tmp_path):
    """A symlink is judged by what it opens, not by its spelling (final review F1)."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[remote]\n", encoding="utf-8")
    try:
        (tmp_path / "meta").symlink_to(tmp_path / ".git", target_is_directory=True)
    except OSError:
        pytest.skip("this machine cannot make symbolic links")
    assert tree_path(tmp_path, "meta") is None
    assert tree_path(tmp_path, "meta/config") is None


# ── DF-94: test gaps ───────────────────────────────────────────────────────────


def test_a_missing_indexer_is_a_codeintel_error(tmp_path, monkeypatch):
    monkeypatch.setattr(index_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(index_module.sys, "executable", str(tmp_path / "python"))
    with pytest.raises(CodeIntelError, match="not installed"):
        index_module._default_cli()


def test_a_query_that_times_out_is_a_codeintel_error(fixture_repo, tmp_path, monkeypatch):
    index = index_for(fixture_repo, tmp_path)
    monkeypatch.setattr(index, "ensure", lambda: None)

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="query", timeout=0.01)

    monkeypatch.setattr("agentic.codeintel.crg.subprocess.run", hang)
    with pytest.raises(CodeIntelError, match="could not run"):
        CodeReviewGraph(index, timeout_s=0.01).overview()


def test_a_link_leaving_the_tree_is_refused(tmp_path):
    tree, outside = tmp_path / "tree", tmp_path / "outside"
    tree.mkdir()
    outside.mkdir()
    (outside / "secret.py").write_text("x = 1\n", encoding="utf-8")
    try:
        os.symlink(outside, tree / "link", target_is_directory=True)
    except OSError:
        pytest.skip("this machine cannot make symbolic links")
    assert tree_path(tree, "link/secret.py") is None


@needs_crg
def test_a_committed_file_is_indexed_after_the_commit(fixture_repo, tmp_path):
    index = index_for(fixture_repo, tmp_path)
    index.ensure()
    (fixture_repo / "pkg" / "extra.py").write_text("from pkg.models import Model\n", "utf-8")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "extra")
    index.ensure()
    assert "pkg/extra.py" in index.indexed_files()


@needs_crg
def test_an_unchanged_dirty_tree_is_not_updated_again(fixture_repo, tmp_path):  # DF-84
    index = index_for(fixture_repo, tmp_path)
    (fixture_repo / "pkg" / "report.py").write_text("from pkg.api import handle\n", "utf-8")
    index.ensure()
    ran: list[str] = []
    real = index._run

    def spy(command: str, *args: str) -> str:
        ran.append(command)
        return real(command, *args)

    index._run = spy
    index.ensure()
    assert [c for c in ran if c in ("build", "update")] == []
