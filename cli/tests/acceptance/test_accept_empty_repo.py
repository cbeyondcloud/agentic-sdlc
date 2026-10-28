"""Acceptance 4 (spec §1.3, M1-70): the same pipeline on a brand-new, empty repository,
from `git init` + `agentic init --repo --standalone`.

What runs for real: `git init` (no commit, no file) with an origin; `agentic init --repo
--standalone` on the unborn branch, its pull request opened through the adapter; the
developer's `init --standalone`; `feature-delivery` from the CLI with every gate, the
Developer creating the project from nothing in the run's tree (package, tests, test
configuration), QA running the new tests; push and pull request. Stubbed as in M1-68:
models (cassette), GitHub API, init's index build, merging init's PR, and the OS sandbox
on Windows native.
"""

from _acceptance_support import (
    Scenario,
    approvals,
    assert_published,
    git,
    handoff,
    shell_results,
    tool,
    trailers_by_step,
)

from agentic_cli.exit_codes import ExitCode

SCENARIO = "m1-70"
REQUEST = "Start a tiny greeting library: greet(name) says hello"
PYTEST = "python -m pytest -q"
PYPROJECT = """[project]
name = "greeter"
version = "0.1.0"
requires-python = ">=3.10"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-p no:cacheprovider"
"""
CODE = '''"""Greetings."""


def greet(name: str) -> str:
    """'Hello, Ada!' for 'Ada'; a blank name is refused."""
    if not name.strip():
        raise ValueError("name must not be blank")
    return f"Hello, {name.strip()}!"
'''
TESTS = """import pytest

from greeter.core import greet


def test_greet_says_hello():
    assert greet("Ada") == "Hello, Ada!"


def test_a_blank_name_is_refused():
    with pytest.raises(ValueError):
        greet("  ")
"""
ITEMS = [
    {
        "id": "REQ-1",
        "statement": "greet(name) returns a greeting for the name.",
        "acceptance_criteria": [
            "WHEN greet is called with Ada THE system SHALL return Hello, Ada!"
        ],
    },
    {
        "id": "REQ-2",
        "statement": "A blank name is refused.",
        "acceptance_criteria": ["WHEN the name is blank THE system SHALL raise ValueError."],
    },
]


def report(summary: str, passed: int, criteria: tuple[str, ...]) -> str:
    return handoff(
        "test-report",
        {"Verdict": "Pass."},
        summary=summary,
        verdict="pass",
        tests={"passed": passed, "failed": 0, "skipped": 0},
        failures=[],
        baseline_failures=[],
        acceptance_results=[
            {"criterion_id": c, "passed": True, "evidence": "tests/test_core.py"} for c in criteria
        ],
        dependency_scan={"blocked_licences": [], "vulnerabilities": []},
    )


SCRIPT = {
    "requirements": [
        tool("list_dir", path="."),
        handoff(
            "requirements",
            {"Assumptions": "A new Python package; pytest for its tests."},
            summary="A greeting library with one function.",
            open_questions=[],
            size="trivial",
            items=ITEMS,
        ),
    ],
    "baseline": [
        tool("run_shell", command=PYTEST),
        report("Baseline: the new repository has no tests yet.", 0, ()),
    ],
    "design": [
        handoff(
            "design",
            {"Decision outcome": "### D1: package greeter, pytest configured in pyproject"},
            summary="Create the greeter package, its tests and pyproject.toml.",
            decisions=[
                {
                    "id": "D1",
                    "choice": "A greeter package with greet() in greeter/core.py.",
                    "rationale": "The smallest layout that installs and tests cleanly.",
                }
            ],
            impact={
                "modules": ["greeter/core.py"],
                "interfaces": ["greet(name)"],
                "tests": ["tests/test_core.py"],
                "confidence": "candidate",
            },
            risks=[],
        ),
    ],
    "develop": [
        tool("write_file", path="pyproject.toml", text=PYPROJECT),
        tool("write_file", path="greeter/__init__.py", text='"""A tiny greeting library."""\n'),
        tool("write_file", path="greeter/core.py", text=CODE),
        tool("write_file", path="tests/test_core.py", text=TESTS),
        tool("run_shell", command=PYTEST),
        handoff(
            "dev-report",
            {"What changed": "New package, tests and pytest configuration."},
            summary="Created greeter with greet() and two tests.",
            commits=[],
            files_changed=["pyproject.toml", "greeter/core.py", "tests/test_core.py"],
            tests_run={"passed": 2, "failed": 0, "skipped": 0},
            deviations_from_design=[],
        ),
    ],
    "review": [
        handoff(
            "review",
            {"Verdict": "Nothing open."},
            summary="Clean: small and tested.",
            findings=[],
            score=9.0,
            prepass_results=[],
        ),
    ],
    "qa": [
        tool("run_shell", command=PYTEST),
        report("Both tests pass; REQ-1 and REQ-2 hold.", 2, ("REQ-1", "REQ-2")),
    ],
}


def test_the_pipeline_on_a_brand_new_empty_repo(scenario: Scenario):
    repo = scenario.empty_repo()
    assert git(repo, "status", "--porcelain") == ""  # nothing at all: no file, no commit
    setup = scenario.init_standalone(repo, new_repo=True)
    assert sorted(setup["files"]) == sorted(
        git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    )
    main_head = git(repo, "rev-parse", "HEAD")
    scenario.models(SCRIPT)

    plan = scenario.ok("--json", "run", "feature-delivery", "--dry-run").json()
    assert plan["preflight"]["passed"], plan["preflight"]
    run = scenario.ok(
        "--json", "run", "feature-delivery", "--input", f"request={REQUEST}", code=ExitCode.PAUSED
    ).json()
    run_id = run["run_id"]
    for gate in ("requirements-approval", "design-approval"):
        assert run["pending"]["id"] == gate
        run = scenario.ok("--json", "approve", run_id, code=ExitCode.PAUSED).json()
    assert run["pending"]["id"] == "review-approval"
    done = scenario.ok("--json", "approve", run_id).json()
    assert done["status"] == "completed", done
    assert scenario.finish_models() == []

    assert [h["step"] for h in done["history"]] == [
        "requirements",
        "baseline",
        "design",
        "develop",
        "review",
        "qa",
        "publish",
    ]
    assert [a["verdict"] for a in approvals(repo, run_id)] == ["approve"] * 3
    assert shell_results(scenario, run_id, "baseline-1") == ["exit 5 - no tests ran"]
    assert shell_results(scenario, run_id, "qa-1") == ["exit 0 - 2 passed"]

    branch = f"sdlc/{run_id}"
    assert list(trailers_by_step(scenario.bare, branch)) == ["develop #1"]
    files = git(scenario.bare, "ls-tree", "-r", "--name-only", branch).splitlines()
    for created in (
        "pyproject.toml",
        "greeter/__init__.py",
        "greeter/core.py",
        "tests/test_core.py",
    ):
        assert created in files
    assert git(scenario.bare, "show", f"{branch}:greeter/core.py") + "\n" == CODE
    assert_published(scenario, repo, run_id, main_head, title=REQUEST)
