"""Running a dev task's tests (M1-59, M1-63; DF-241).

A dev task's truth is its tests: `fail_to_pass` must pass after the change and
`pass_to_pass` must keep passing. Both the corpus check and the scoring of an agent's
run use `run_tests`, which runs pytest on a folder and reads each requested test's
outcome from pytest's own `-rA` summary (node ids as pytest prints them, `/`-separated
on every OS). A test that did not run - not collected, an import error, skipped - did
not pass. pytest's rootdir, configuration and conftest.py search stop at the folder, so a
tree inside the user's repository is measured on its own; a session that never reached
its summary raises `CouldNotRunTests` rather than failing every test.

An agent's tree is scored by `run_case_tests`: the case's test files, conftest.py files
and pytest configuration are first restored from the pristine workspace (an agent that
rewrites a failing test does not pass it), and a session that does not report on the
tree is retried on a pristine copy to tell the agent's fault from the harness's.

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
_SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
_FINAL_LINE = re.compile(r"^=*\s*(?:no tests ran|\d+ \w+(?:, \d+ \w+)*) in \d+(?:\.\d+)?s\b")
_REPORTED = (0, 1, 2)  # pytest ran: all passed, some failed, or a collection error
_SCRATCH = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")
#: pytest's configuration files in the order it looks for them, with the section that
#: makes each one count; a workspace's own (at its root) is used, never a parent's.
PYTEST_CONFIGS = {
    "pytest.ini": "",
    ".pytest.ini": "",
    "pyproject.toml": "[tool.pytest",
    "tox.ini": "[pytest]",
    "setup.cfg": "[tool:pytest]",
}


class CouldNotRunTests(RuntimeError):
    """pytest did not report on the tests: the session stopped before its summary (a
    usage or internal error, a conftest that cannot load, a process that exited)."""


def test_files(test_ids: Sequence[str]) -> list[str]:
    """The files holding `test_ids`, in order (each checked to stay inside the folder)."""
    files = []
    for test in test_ids:
        path = PurePosixPath(test.split("::", 1)[0])
        if path.is_absolute() or ".." in path.parts or "\\" in test or ":" in str(path)[:2]:
            raise ValueError(f"a test id must name a file inside the folder, not {test!r}")
        files.append(path.as_posix())
    return list(dict.fromkeys(files))


def pytest_config(folder: Path) -> str:
    """The pytest configuration file of the workspace at `folder`, or the null device:
    pytest must never pick up the configuration of a folder above (the live default
    output folder is inside the user's own repository)."""
    for name, section in PYTEST_CONFIGS.items():
        path = Path(folder) / name
        if path.is_file() and section in path.read_text(encoding="utf-8", errors="replace"):
            return str(path)
    return os.devnull


def pytest_argv(files: Sequence[str], folder: Path) -> list[str]:
    """The command that runs the test files `files` of the workspace at `folder`. Whole
    files, not node ids: one id pytest cannot find would abort the session and fail
    every other test with it. The rootdir, the configuration and the conftest.py search
    all stop at `folder`, so node ids are the workspace's and no parent's code runs."""
    flags = ["-q", "-rA", "--no-header", "-p", "no:cacheprovider", "-o", "addopts="]
    here = str(folder)
    isolated = ["--rootdir", here, "--confcutdir", here, "-c", pytest_config(folder)]
    return [sys.executable, "-m", "pytest", *flags, *isolated, *files]


def parse_outcomes(output: str, test_ids: Sequence[str]) -> dict[str, bool]:
    """Each requested test id -> passed, from pytest's short summary. Only the lines
    after the last summary header count: output the code under test printed is shown
    earlier, in captured sections, and a forged line there must not read as a pass."""
    lines = [line.strip() for line in output.splitlines()]
    headers = [i for i, line in enumerate(lines) if _SUMMARY_HEADER.match(line)]
    passed: dict[str, bool] = {}
    for line in lines[headers[-1] + 1 :] if headers else lines:
        if match := _OUTCOME.match(line):
            outcome, node = match.groups()
            passed[node] = passed.get(node, True) and outcome in _PASSING
    return {test: passed.get(test, False) for test in test_ids}


def reported(output: str) -> bool:
    """Whether pytest reached its final summary line ('1 failed, 2 passed in 0.10s')."""
    return any(_FINAL_LINE.match(line.strip()) for line in output.splitlines())


async def run_tests(
    folder: Path,
    test_ids: Sequence[str],
    *,
    timeout_s: float = TEST_TIMEOUT_S,
    sandbox: Sandbox | None = None,
) -> dict[str, bool]:
    """Run `test_ids` with pytest in `folder`: test id -> passed. A timeout fails them all;
    a test whose file is gone did not pass. Raises `CouldNotRunTests` when pytest did
    not report (see `run_case_tests` for telling the harness's fault from the code's)."""
    folder = Path(folder)
    files = [f for f in test_files(test_ids) if (folder / f).is_file()]
    if not files:
        return dict.fromkeys(test_ids, False)
    argv = pytest_argv(files, folder)
    if sandbox is not None:
        result = await sandbox.run(command_line(argv), folder, timeout_s)
    else:
        env = {**safe_env(), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PYTEST_ADDOPTS", None)
        result = await run_process(argv, folder, timeout_s, env=env)
    if result.timed_out:
        return dict.fromkeys(test_ids, False)
    if result.exit_code not in _REPORTED or not reported(result.stdout):
        said = (result.stderr or result.stdout).strip().splitlines()
        reason = said[0][:300] if said else "no output"
        raise CouldNotRunTests(f"pytest stopped with exit code {result.exit_code}: {reason}")
    return parse_outcomes(result.stdout, test_ids)


def restore_tests(source: Path, tree: Path, test_ids: Sequence[str]) -> None:
    """Put the case's test files, every conftest.py and the root pytest configuration of
    `tree` back as the pristine workspace `source` has them, and remove any the agent
    added: the case's tests are the truth an agent's run is measured by, so its edits to
    them (or a conftest.py that passes everything) must not count."""
    source, tree = Path(source), Path(tree)
    wanted = {*test_files(test_ids), *PYTEST_CONFIGS}
    wanted |= _conftests(source)
    for rel in _conftests(tree) - wanted:
        _remove(tree / rel)
    for rel in sorted(wanted):
        target = tree / rel
        _unlink_folders(tree, PurePosixPath(rel).parent)  # never write through a link
        _remove(target)
        if (source / rel).is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / rel, target)


async def run_case_tests(
    source: Path,
    tree: Path,
    test_ids: Sequence[str],
    *,
    timeout_s: float = TEST_TIMEOUT_S,
    sandbox: Sandbox | None = None,
) -> dict[str, bool]:
    """Score an agent's `tree` by the case's tests: restore them from the pristine
    workspace `source`, then run them. When pytest does not report on the tree, the same
    tests run on a fresh copy of `source` beside it: if they report there, the agent's
    change stopped the session and every test failed; if not, the harness cannot run the
    tests here and `CouldNotRunTests` is raised (the run stays unmeasured)."""
    restore_tests(source, tree, test_ids)
    try:
        return await run_tests(tree, test_ids, timeout_s=timeout_s, sandbox=sandbox)
    except CouldNotRunTests:
        with tempfile.TemporaryDirectory(
            prefix="pristine-", dir=Path(tree).parent, ignore_cleanup_errors=True
        ) as tmp:
            pristine = copy_workspace(source, Path(tmp) / "tree")
            await run_tests(pristine, test_ids, timeout_s=timeout_s, sandbox=sandbox)
        return dict.fromkeys(test_ids, False)


def _conftests(folder: Path) -> set[str]:
    """Every conftest.py under `folder`, '/'-separated relative paths (links not followed)."""
    found = set()
    for top, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".pytest_cache")]
        dirs[:] = [d for d in dirs if not _is_link(Path(top) / d)]  # junctions too
        if "conftest.py" in files:
            found.add((Path(top) / "conftest.py").relative_to(folder).as_posix())
    return found


def _unlink_folders(tree: Path, folder: PurePosixPath) -> None:
    """Remove any link (symlink or junction) on the way from `tree` to `folder`: the link
    itself, never what it points at, so restored files land inside the tree."""
    here = tree
    for part in folder.parts:
        here = here / part
        if _is_link(here):
            _remove(here)
            return


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _remove(path: Path) -> None:
    if path.is_junction():
        os.rmdir(path)  # the junction only, not its target's files
    elif path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


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
        try:
            before = await run_tests(tree, ids)
            overlay(corpus.path(case.expected.solution), tree)
            after = await run_tests(tree, ids)
        except CouldNotRunTests as error:
            return [f"the tests could not run: {error}"]
    problems = [f"{t} passes without the change" for t in tests.fail_to_pass if before[t]]
    problems += [f"{t} fails without the change" for t in tests.pass_to_pass if not before[t]]
    problems += [f"{t} fails with the reference change" for t in ids if not after[t]]
    return problems


__all__ = [
    "PYTEST_CONFIGS",
    "CouldNotRunTests",
    "check_dev_task",
    "command_line",
    "copy_workspace",
    "overlay",
    "parse_outcomes",
    "pytest_argv",
    "pytest_config",
    "reported",
    "restore_tests",
    "run_case_tests",
    "run_tests",
]
