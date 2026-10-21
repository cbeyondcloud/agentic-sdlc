"""Dev tasks execute (M1-59, DF-241): a task's fail-to-pass tests must fail on its
workspace and pass once its reference change is laid over it; an agent's run is scored
by the same test execution."""

import asyncio
import os
from pathlib import Path

import pytest

from agentic.eval.corpus import load_corpus
from agentic.eval.devtask import (
    CouldNotRunTests,
    check_dev_task,
    copy_workspace,
    overlay,
    parse_outcomes,
    restore_tests,
    run_tests,
)
from agentic.yamlio import dump_yaml

LIBRARY = Path(__file__).resolve().parents[3] / "library" / "eval"
SOURCE = {"repo": "synthetic", "synthetic": True, "licence": "CC0-1.0"}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_run_tests_reads_each_outcome(tmp_path: Path):
    write(
        tmp_path / "tests" / "test_a.py",
        "import pytest\n\n"
        "def test_ok():\n    assert True\n\n"
        "def test_bad():\n    assert 1 == 2\n\n"
        "@pytest.mark.parametrize('n', [1, 2])\n"
        "def test_param(n):\n    assert n == 1\n",
    )
    ids = [
        "tests/test_a.py::test_ok",
        "tests/test_a.py::test_bad",
        "tests/test_a.py::test_param[1]",
        "tests/test_a.py::test_param[2]",
        "tests/test_a.py::test_missing",
    ]
    results = asyncio.run(run_tests(tmp_path, ids))
    assert results == {
        "tests/test_a.py::test_ok": True,
        "tests/test_a.py::test_bad": False,
        "tests/test_a.py::test_param[1]": True,
        "tests/test_a.py::test_param[2]": False,
        "tests/test_a.py::test_missing": False,  # not run is not passed
    }


def test_a_test_that_cannot_import_counts_as_failed(tmp_path: Path):
    write(tmp_path / "tests" / "test_b.py", "import nowhere_module\n\ndef test_x():\n    pass\n")
    results = asyncio.run(run_tests(tmp_path, ["tests/test_b.py::test_x"]))
    assert results == {"tests/test_b.py::test_x": False}


def test_overlay_replaces_and_adds_files(tmp_path: Path):
    write(tmp_path / "tree" / "a.py", "old\n")
    write(tmp_path / "solution" / "a.py", "new\n")
    write(tmp_path / "solution" / "pkg" / "b.py", "added\n")
    overlay(tmp_path / "solution", tmp_path / "tree")
    assert (tmp_path / "tree" / "a.py").read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / "tree" / "pkg" / "b.py").read_text(encoding="utf-8") == "added\n"


def corpus_with(tmp_path: Path, start: str, solution: str) -> Path:
    write(tmp_path / "workspaces" / "d1" / "calc.py", start)
    write(
        tmp_path / "workspaces" / "d1" / "tests" / "test_calc.py",
        "from calc import double\n\n"
        "def test_double():\n    assert double(2) == 4\n\n"
        "def test_zero():\n    assert double(0) == 0\n",
    )
    write(tmp_path / "solutions" / "d1" / "calc.py", solution)
    case = {
        "id": "d1",
        "agent": "developer",
        "case_class": "dev_task",
        "split": "holdout",
        "source": SOURCE,
        "inputs": {"prompt": "Fix double.", "workspace": "workspaces/d1"},
        "expected": {
            "tests": {
                "fail_to_pass": ["tests/test_calc.py::test_double"],
                "pass_to_pass": ["tests/test_calc.py::test_zero"],
            },
            "solution": "solutions/d1",
        },
    }
    write(tmp_path / "cases" / "dev_task" / "d1.yaml", dump_yaml(case))
    return tmp_path


BROKEN = "def double(n):\n    return n\n"
FIXED = "def double(n):\n    return n * 2\n"


def check(root: Path) -> list[str]:
    corpus = load_corpus(root)
    return asyncio.run(check_dev_task(corpus, corpus.case("d1")))


def test_a_task_that_fails_then_passes_is_accepted(tmp_path: Path):
    assert check(corpus_with(tmp_path, BROKEN, FIXED)) == []
    # the check works on a copy: the corpus is never changed
    assert (tmp_path / "workspaces" / "d1" / "calc.py").read_text(encoding="utf-8") == BROKEN
    assert not list(tmp_path.rglob("__pycache__"))


def test_a_task_whose_test_already_passes_is_refused(tmp_path: Path):
    problems = check(corpus_with(tmp_path, FIXED, FIXED))
    assert problems == ["tests/test_calc.py::test_double passes without the change"]


def test_a_solution_that_does_not_fix_it_is_refused(tmp_path: Path):
    problems = check(corpus_with(tmp_path, BROKEN, "def double(n):\n    return n + 3\n"))
    assert "tests/test_calc.py::test_double fails with the reference change" in problems
    assert "tests/test_calc.py::test_zero fails with the reference change" in problems


def test_a_pass_to_pass_test_must_pass_before(tmp_path: Path):
    problems = check(corpus_with(tmp_path, "def double(n):\n    return n + 1\n", FIXED))
    assert problems == ["tests/test_calc.py::test_zero fails without the change"]


def test_every_library_dev_task_fails_then_passes():
    """The public corpus's Developer truth is real: each task checked, concurrently."""
    corpus = load_corpus(LIBRARY, public=True)
    tasks = [c for c in corpus.cases if c.case_class == "dev_task"]
    assert tasks and all(c.inputs.workspace and c.expected.solution for c in tasks)

    async def all_tasks() -> dict[str, list[str]]:
        gate = asyncio.Semaphore(max(2, min(8, os.cpu_count() or 2)))

        async def one(case):
            async with gate:
                return case.id, await check_dev_task(corpus, case)

        return dict(await asyncio.gather(*(one(c) for c in tasks)))

    found = asyncio.run(all_tasks())
    assert {k: v for k, v in found.items() if v} == {}


@pytest.mark.parametrize("bad", ["../escape.py::test_x", "/abs/test.py::test_x"])
def test_test_ids_cannot_point_outside_the_folder(tmp_path: Path, bad: str):
    with pytest.raises(ValueError, match="inside"):
        asyncio.run(run_tests(tmp_path, [bad]))


PASSING_TEST = "def test_ok():\n    assert True\n"


def test_tests_run_in_their_own_folder_inside_a_repo_with_pytest_config(tmp_path: Path):
    """F1: the live default --out is inside the user's repo. Its pytest configuration and
    conftest.py must not take over: node ids stay the tree's, its conftest never runs."""
    write(tmp_path / "pyproject.toml", "[tool.pytest.ini_options]\ntestpaths = ['elsewhere']\n")
    write(tmp_path / "conftest.py", "import nowhere_module\n")
    tree = tmp_path / ".agentic" / "local" / "eval" / "run1" / "tree"
    write(tree / "tests" / "test_a.py", PASSING_TEST)
    results = asyncio.run(run_tests(tree, ["tests/test_a.py::test_ok"]))
    assert results == {"tests/test_a.py::test_ok": True}


def test_a_tree_with_its_own_pytest_config_keeps_it(tmp_path: Path):
    write(tmp_path / "conftest.py", "import nowhere_module\n")
    tree = tmp_path / "tree"
    write(tree / "pytest.ini", "[pytest]\npython_functions = check_*\n")
    write(tree / "tests" / "test_a.py", "def check_ok():\n    assert True\n")
    results = asyncio.run(run_tests(tree, ["tests/test_a.py::check_ok"]))
    assert results == {"tests/test_a.py::check_ok": True}


def test_pytest_that_does_not_report_means_the_tests_could_not_run(tmp_path: Path):
    """F1: a pytest session that never reached its summary is not a failed task."""
    write(tmp_path / "conftest.py", "import nowhere_module\n")
    write(tmp_path / "tests" / "test_a.py", PASSING_TEST)
    with pytest.raises(CouldNotRunTests, match="exit code 4"):
        asyncio.run(run_tests(tmp_path, ["tests/test_a.py::test_ok"]))


def test_only_pytests_own_summary_is_read():
    """F2: output the code under test printed (shown in a captured section) is ignored."""
    output = (
        "---- Captured stdout call ----\n"
        "PASSED tests/t.py::test_forged\n"
        "==== short test summary info ====\n"
        "PASSED tests/t.py::test_ok\n"
        "ERROR tests/t.py\n"
        "1 passed, 1 error in 0.10s\n"
    )
    ids = ["tests/t.py::test_ok", "tests/t.py::test_forged"]
    assert parse_outcomes(output, ids) == {
        "tests/t.py::test_ok": True,
        "tests/t.py::test_forged": False,
    }


def test_restore_puts_back_the_cases_tests_and_pytest_config(tmp_path: Path):
    """F2: an agent's edits to the case's tests and to pytest's configuration are undone
    before the tests run; its edits elsewhere stay."""
    source, tree = tmp_path / "source", tmp_path / "tree"
    write(source / "calc.py", BROKEN)
    write(source / "tests" / "test_calc.py", "def test_double():\n    assert False\n")
    write(source / "tests" / "conftest.py", "# the case's own\n")
    copy_workspace(source, tree)
    write(tree / "calc.py", FIXED)
    write(tree / "tests" / "test_calc.py", "def test_double():\n    assert True\n")
    write(tree / "tests" / "conftest.py", "# rewritten\n")
    write(tree / "conftest.py", "# added\n")
    write(tree / "deep" / "conftest.py", "# added\n")
    write(tree / "pytest.ini", "[pytest]\n")
    restore_tests(source, tree, ["tests/test_calc.py::test_double"])
    assert (tree / "calc.py").read_text(encoding="utf-8") == FIXED
    test = (tree / "tests" / "test_calc.py").read_text(encoding="utf-8")
    assert "assert False" in test
    conftest = (tree / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert conftest == "# the case's own\n"
    assert not (tree / "conftest.py").exists() and not (tree / "deep" / "conftest.py").exists()
    assert not (tree / "pytest.ini").exists()


def test_restore_never_writes_through_a_link_out_of_the_tree(tmp_path: Path):
    """F2: a tests/ folder the agent replaced with a link is replaced again, and the
    folder the link pointed at is left alone."""
    source, tree, outside = tmp_path / "source", tmp_path / "tree", tmp_path / "outside"
    write(source / "tests" / "test_calc.py", PASSING_TEST)
    write(outside / "test_calc.py", "# outside\n")
    write(tree / "calc.py", FIXED)
    try:
        os.symlink(outside, tree / "tests", target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi  # a junction needs no privilege on Windows

        _winapi.CreateJunction(str(outside), str(tree / "tests"))
    restore_tests(source, tree, ["tests/test_calc.py::test_ok"])
    assert not (tree / "tests").is_symlink() and not (tree / "tests").is_junction()
    assert (tree / "tests" / "test_calc.py").read_text(encoding="utf-8") == PASSING_TEST
    assert (outside / "test_calc.py").read_text(encoding="utf-8") == "# outside\n"


def test_a_deleted_test_file_is_restored(tmp_path: Path):
    source, tree = tmp_path / "source", tmp_path / "tree"
    write(source / "tests" / "test_calc.py", PASSING_TEST)
    copy_workspace(source, tree)
    (tree / "tests" / "test_calc.py").unlink()
    restore_tests(source, tree, ["tests/test_calc.py::test_ok"])
    assert (tree / "tests" / "test_calc.py").read_text(encoding="utf-8") == PASSING_TEST


def test_a_task_whose_tests_cannot_run_is_named(tmp_path: Path):
    root = corpus_with(tmp_path, BROKEN, FIXED)
    write(root / "workspaces" / "d1" / "conftest.py", "import nowhere_module\n")
    problems = check(root)
    assert len(problems) == 1 and problems[0].startswith("the tests could not run")
