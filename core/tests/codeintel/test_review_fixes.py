"""Regression tests for the SP9 final review (Sonnet 5.5): each pins one finding.
C1 the guard's read walls, I1 freshness after a revert, I2/I3 git's real index untouched
and any number of new files, I4 no tool error ends the run, I5 no fsmonitor command,
I6 no network for UNC paths, I7 diff paths confined to the tree."""

import sys
import time
from pathlib import Path

import pytest
from _codeintel_support import git
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from agentic.codeintel import Candidates, CodeIntelError, codeintel_tools, tree_path
from agentic.codeintel.crg import CodeReviewGraph, parse_answer
from agentic.codeintel.index import LocalIndex, available

needs_crg = pytest.mark.skipif(not available(), reason="needs the codeintel extra")


def call(tools, name, **args) -> str:
    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if returns:
            return ModelResponse(parts=[TextPart(str(returns[-1].content))])
        return ModelResponse(parts=[ToolCallPart(name, args)])

    return Agent(FunctionModel(model), tools=tools).run_sync("go").output


class Intel:
    name = "fake"
    supports = frozenset({"dependents", "impact", "tests_for", "overview"})

    def __init__(self, files=("pkg/service.py", ".env", "infra/prod/main.tf"), error=None):
        self.files, self.error = tuple(files), error

    def _answer(self):
        if self.error:
            raise self.error
        return Candidates(self.files, source=self.name)

    def dependents(self, path):
        return self._answer()

    def impact(self, diff):
        return self._answer()

    def tests_for(self, path):
        return self._answer()

    def overview(self):
        if self.error:
            raise self.error
        return "ok"


def deny(target: Path) -> str | None:
    rel = target.as_posix()
    if rel.endswith("/.env") or "/infra/prod/" in rel:
        return f"REFUSED: {target.name} is protected"
    return None


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "service.py").write_text("x = 1\n" * 10_000, encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=abc-secret\n", encoding="utf-8")
    (tmp_path / "infra" / "prod").mkdir(parents=True)
    (tmp_path / "infra" / "prod" / "main.tf").write_text("secret_infra = 1\n", encoding="utf-8")
    return tmp_path


# ── C1: context_pack obeys the guard's read walls ─────────────────────────────


@pytest.mark.parametrize("path", [".env", "infra/prod/main.tf"])
def test_context_pack_refuses_what_the_guard_refuses(tree, path):
    out = call(codeintel_tools(Intel(), tree, read_refusal=deny), "context_pack", path=path)
    assert out.startswith("REFUSED")
    assert "abc-secret" not in out and "secret_infra" not in out


def test_answers_leave_out_paths_the_guard_refuses(tree):
    out = call(
        codeintel_tools(Intel(), tree, read_refusal=deny), "dependents", path="pkg/service.py"
    )
    assert "pkg/service.py" in out and ".env" not in out and "infra/prod" not in out


def test_context_pack_budget_is_capped(tree):
    out = call(
        codeintel_tools(Intel(), tree), "context_pack", path="pkg/service.py", budget_chars=10**9
    )
    assert len(out) < 120_000


# ── I4: no tool error ends the run ────────────────────────────────────────────


@pytest.mark.parametrize("error", [OSError("disk gone"), ValueError("bad json"), RuntimeError("x")])
def test_an_unexpected_error_is_a_refusal(tree, error):
    tools = codeintel_tools(Intel(error=error), tree)
    for name, args in (("dependents", {"path": "pkg/service.py"}), ("overview", {})):
        assert call(tools, name, **args).startswith("REFUSED")


def test_noise_before_the_answer_is_ignored():
    assert parse_answer('WARNING: something\n{"files": ["a.py"]}\n') == {"files": ["a.py"]}
    with pytest.raises(CodeIntelError):
        parse_answer("not json at all")


# ── I6: UNC and device paths never touch the network ──────────────────────────


@pytest.mark.parametrize(
    "path",
    ["\\\\192.0.2.1\\share\\x.py", "//192.0.2.1/share/x.py", "\\\\?\\C:\\x.py", "\\\\.\\COM1"],
)
def test_unc_and_device_paths_are_refused_without_a_lookup(tmp_path, path):
    started = time.monotonic()
    assert tree_path(tmp_path, path) is None
    assert time.monotonic() - started < 1


# ── I1, I2, I3, I5, I7 against the real index ────────────────────────────────


def make_index(repo: Path, tmp_path: Path) -> LocalIndex:
    return LocalIndex(repo, tmp_path / "index", home_dir=tmp_path / "crg-home")


@needs_crg
def test_a_reverted_edit_is_not_served_from_a_stale_index(fixture_repo, tmp_path):  # I1
    index = make_index(fixture_repo, tmp_path)
    index.ensure()
    (fixture_repo / "pkg" / "report.py").write_text("from pkg.api import handle\n", "utf-8")
    index.ensure()
    assert "pkg/report.py" in index.indexed_files()
    git(fixture_repo, "reset", "-q", "--hard", "HEAD")
    git(fixture_repo, "clean", "-q", "-ffd")  # a fresh attempt discards the edit (M1-30)
    assert not index.fresh()
    index.ensure()
    assert "pkg/report.py" not in index.indexed_files()


@needs_crg
def test_a_deleted_then_restored_file_is_indexed_again(fixture_repo, tmp_path):  # I1
    index = make_index(fixture_repo, tmp_path)
    index.ensure()
    (fixture_repo / "pkg" / "api.py").unlink()
    index.ensure()
    git(fixture_repo, "checkout", "--", "pkg/api.py")
    index.ensure()
    assert "pkg/api.py" in index.indexed_files()


@needs_crg
def test_git_index_of_the_tree_is_never_changed(fixture_repo, tmp_path):  # I2
    nested = fixture_repo / "vendor_x"
    nested.mkdir()
    git(nested.parent, "init", "-q", str(nested))
    (nested / "lib.py").write_text("y = 1\n", encoding="utf-8")
    (fixture_repo / "pkg" / "new.py").write_text("from pkg.models import Model\n", "utf-8")
    before = git(fixture_repo, "ls-files", "--stage")
    index = make_index(fixture_repo, tmp_path)
    index.ensure()
    assert git(fixture_repo, "ls-files", "--stage") == before
    assert "pkg/new.py" in index.indexed_files()  # still seen by the index


@needs_crg
def test_many_new_files_are_indexed_without_a_crash(fixture_repo, tmp_path):  # I3
    folder = fixture_repo / ("gen_" + "x" * 40)
    folder.mkdir()
    for i in range(1200):
        (folder / f"module_with_a_rather_long_name_{i:05d}.py").write_text(f"v{i} = {i}\n", "utf-8")
    index = make_index(fixture_repo, tmp_path)
    index.ensure()
    assert any(f.startswith("gen_") for f in index.indexed_files())


@needs_crg
def test_no_fsmonitor_command_runs(fixture_repo, tmp_path):  # I5
    marker = tmp_path / "fsmonitor-ran"
    script = tmp_path / "monitor.sh"
    script.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", encoding="utf-8")
    script.chmod(0o755)
    git(fixture_repo, "config", "core.fsmonitor", script.as_posix())
    (fixture_repo / "pkg" / "new.py").write_text("z = 1\n", encoding="utf-8")
    index = make_index(fixture_repo, tmp_path)
    index.ensure()
    index.fresh()
    assert not marker.exists()


@needs_crg
def test_diff_paths_outside_the_tree_are_ignored(fixture_repo, tmp_path):  # I7
    (fixture_repo.parent / "secret.txt").write_text("s\n", encoding="utf-8")
    intel = CodeReviewGraph(make_index(fixture_repo, tmp_path))
    diff = "--- a/../secret.txt\n+++ b/../secret.txt\n"
    found = intel.impact(diff)
    assert found.files == () and "no file in the tree" in found.note
    absolute = f"--- a/{(fixture_repo.parent / 'secret.txt').as_posix()}\n"
    assert intel.impact(absolute).files == ()


def test_index_errors_from_git_are_codeintel_errors(tmp_path):  # I4
    index = LocalIndex(
        tmp_path / "missing",
        tmp_path / "i",
        home_dir=tmp_path / "h",
        cli=[sys.executable, "-c", "pass"],
    )
    with pytest.raises(CodeIntelError):
        index.ensure()
