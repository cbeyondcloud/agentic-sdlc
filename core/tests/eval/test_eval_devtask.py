"""Dev tasks execute (M1-59, DF-241): a task's fail-to-pass tests must fail on its
workspace and pass once its reference change is laid over it; an agent's run is scored
by the same test execution."""

import asyncio
import os
from pathlib import Path

import pytest

from agentic.eval.corpus import load_corpus
from agentic.eval.devtask import check_dev_task, overlay, run_tests
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
