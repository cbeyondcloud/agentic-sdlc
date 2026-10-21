"""Running a dev task's tests (M1-59, M1-63; DF-241).

A dev task's truth is its tests: `fail_to_pass` must pass after the change and
`pass_to_pass` must keep passing. Both the corpus check and the scoring of an agent's
run use `run_tests`, which runs pytest on a folder and reads each requested test's
outcome from pytest's own `-rA` summary (node ids as pytest prints them, `/`-separated
on every OS). A test that did not run - not collected, an import error, skipped - did
not pass.

`check_dev_task` proves a task's truth before anyone measures with it: on a copy of the
workspace the fail-to-pass tests fail and the pass-to-pass tests pass; with the
reference change (`expected.solution`) laid over the copy, every test passes.

Commands run through `run_process` (own process group, killed on timeout, the safe
environment) or, for an agent's tree, through the step's `Sandbox`, since the code
under test is then the agent's. Plugins are not auto-loaded, so the outcome does not
depend on what else is installed in the environment.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from agentic.agents.tools.context import Sandbox
from agentic.eval.cases import EvalCase
from agentic.sandbox.process import run_process, safe_env

TEST_TIMEOUT_S = 300.0
_OUTCOME = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS) (.+?)(?: - .*)?$")
_PASSING = ("PASSED", "XPASS")
_SCRATCH = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")


def test_files(test_ids: Sequence[str]) -> list[str]:
    """The files holding `test_ids`, in order (each checked to stay inside the folder)."""
    files = []
    for test in test_ids:
        path = PurePosixPath(test.split("::", 1)[0])
        if path.is_absolute() or ".." in path.parts or "\\" in test or ":" in str(path)[:2]:
            raise ValueError(f"a test id must name a file inside the folder, not {test!r}")
        files.append(path.as_posix())
    return list(dict.fromkeys(files))


def pytest_argv(files: Sequence[str]) -> list[str]:
    """The command that runs the test files `files`. Whole files, not node ids: one id
    pytest cannot find would abort the session and fail every other test with it."""
    flags = ["-q", "-rA", "--no-header", "-p", "no:cacheprovider", "-o", "addopts="]
    return [sys.executable, "-m", "pytest", *flags, *files]


def parse_outcomes(output: str, test_ids: Sequence[str]) -> dict[str, bool]:
    """Each requested test id -> passed, from pytest's short summary."""
    passed: dict[str, bool] = {}
    for line in output.splitlines():
        if match := _OUTCOME.match(line.strip()):
            outcome, node = match.groups()
            passed[node] = passed.get(node, True) and outcome in _PASSING
    return {test: passed.get(test, False) for test in test_ids}


async def run_tests(
    folder: Path,
    test_ids: Sequence[str],
    *,
    timeout_s: float = TEST_TIMEOUT_S,
    sandbox: Sandbox | None = None,
) -> dict[str, bool]:
    """Run `test_ids` with pytest in `folder`: test id -> passed. A timeout fails them all;
    a test whose file is gone did not pass."""
    files = [f for f in test_files(test_ids) if (Path(folder) / f).is_file()]
    if not files:
        return dict.fromkeys(test_ids, False)
    argv = pytest_argv(files)
    if sandbox is not None:
        result = await sandbox.run(command_line(argv), Path(folder), timeout_s)
    else:
        env = {**safe_env(), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PYTEST_ADDOPTS", None)
        result = await run_process(argv, Path(folder), timeout_s, env=env)
    if result.timed_out:
        return dict.fromkeys(test_ids, False)
    return parse_outcomes(result.stdout, test_ids)


def command_line(argv: Sequence[str]) -> str:
    """`argv` as one command string for the platform's shell (a sandbox takes a string)."""
    return subprocess.list2cmdline(list(argv)) if os.name == "nt" else shlex.join(argv)


def overlay(source: Path, target: Path) -> None:
    """Copy every file of `source` over `target` (replacing or adding, never deleting)."""
    shutil.copytree(source, target, dirs_exist_ok=True, ignore=_SCRATCH)


def copy_workspace(workspace: Path, target: Path) -> Path:
    """A fresh copy of a workspace (caches left out) for one run or check."""
    shutil.copytree(workspace, target, ignore=_SCRATCH)
    return target


async def check_dev_task(corpus, case: EvalCase) -> list[str]:
    """What is wrong with a dev task's truth (empty: its tests fail without the reference
    change and pass with it). `corpus` is the loaded `Corpus` holding the case."""
    tests = case.expected.tests
    if case.inputs.workspace is None or case.expected.solution is None or tests is None:
        return ["a checked dev task needs a workspace, a solution and tests"]
    ids = [*tests.fail_to_pass, *tests.pass_to_pass]
    with tempfile.TemporaryDirectory(prefix="agentic-devtask-", ignore_cleanup_errors=True) as tmp:
        tree = copy_workspace(corpus.path(case.inputs.workspace), Path(tmp) / "tree")
        before = await run_tests(tree, ids)
        problems = [f"{t} passes without the change" for t in tests.fail_to_pass if before[t]]
        problems += [f"{t} fails without the change" for t in tests.pass_to_pass if not before[t]]
        overlay(corpus.path(case.expected.solution), tree)
        after = await run_tests(tree, ids)
        problems += [f"{t} fails with the reference change" for t in ids if not after[t]]
    return problems


__all__ = [
    "check_dev_task",
    "command_line",
    "copy_workspace",
    "overlay",
    "parse_outcomes",
    "pytest_argv",
    "run_tests",
]
