"""The local code-intelligence index (M1-36, D39): built into run state on first use, kept
fresh by commit, never written into the tree or the user's home."""

import sys
from pathlib import Path

import pytest
from _codeintel_support import git

from agentic.codeintel import CodeIntelError
from agentic.codeintel.index import LocalIndex, available

pytestmark = pytest.mark.skipif(not available(), reason="needs the codeintel extra")


def index_for(repo: Path, tmp_path: Path, **kwargs) -> LocalIndex:
    return LocalIndex(
        repo, tmp_path / "state" / "index", home_dir=tmp_path / "state" / "crg-home", **kwargs
    )


def tree_files(repo: Path) -> set[str]:
    return {p.relative_to(repo).as_posix() for p in repo.rglob("*") if ".git" not in p.parts}


def test_first_use_builds_into_run_state_only(fixture_repo, tmp_path):
    before = tree_files(fixture_repo)
    index = index_for(fixture_repo, tmp_path)
    assert index.recorded_commit() is None
    index.ensure()
    assert (tmp_path / "state" / "index" / "graph.db").is_file()
    assert index.recorded_commit() == git(fixture_repo, "rev-parse", "HEAD")
    assert tree_files(fixture_repo) == before  # nothing written into the tree


def test_a_fresh_index_is_not_rebuilt(fixture_repo, tmp_path):
    index = index_for(fixture_repo, tmp_path)
    index.ensure()
    assert index.fresh()
    ran: list[str] = []
    real = index._run

    def spy(command: str, *args: str) -> str:
        if command in ("build", "update"):
            ran.append(command)
        return real(command, *args)

    index._run = spy
    index.ensure()
    assert ran == []


def test_a_new_commit_makes_it_stale_and_update_refreshes_it(fixture_repo, tmp_path):
    index = index_for(fixture_repo, tmp_path)
    index.ensure()
    (fixture_repo / "pkg" / "extra.py").write_text("from pkg.models import Model\n", "utf-8")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "extra")
    assert not index.fresh()
    index.ensure()
    assert index.recorded_commit() == git(fixture_repo, "rev-parse", "HEAD")


def test_an_edit_is_seen_after_update(fixture_repo, tmp_path):  # Review Focus 2
    index = index_for(fixture_repo, tmp_path)
    index.ensure()
    (fixture_repo / "pkg" / "report.py").write_text("from pkg.api import handle\n", "utf-8")
    assert not index.fresh()  # an uncommitted edit makes it stale
    index.ensure()
    assert "pkg/report.py" in index.indexed_files()


def test_a_failing_build_raises_codeintel_error(fixture_repo, tmp_path):
    failing = tmp_path / "failing_cli.py"
    failing.write_text("import sys\nprint('Error: disk full', file=sys.stderr)\nsys.exit(3)\n")
    index = index_for(fixture_repo, tmp_path, cli=[sys.executable, str(failing)])
    with pytest.raises(CodeIntelError, match="disk full"):
        index.ensure()
