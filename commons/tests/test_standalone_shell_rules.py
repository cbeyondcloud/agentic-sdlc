"""The standalone template's shell rules, as the guard applies them to the library agents
(M1-62): the common test runners of Java, JS, Make and Python repos are allowed for the
developer and QA (DF-234), and QA's audit commands cannot modify the environment or
write files (DF-237)."""

import subprocess
from pathlib import Path

import pytest

from agentic.definitions import load_agent
from agentic.policy import ResolveContext, load_policy, resolve_agent
from sdlc_commons.guards import Guard

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "standalone" / "policy"
AGENTS = ROOT / "library" / "agents"
RUNNERS = (
    "mvn test",
    "mvn -q test",
    "./mvnw test",
    "./gradlew test",
    "gradle test",
    "npm test",
    "npm run test",
    "yarn test",
    "pnpm test",
    "make test",
    "coverage run -m pytest",
    "tox",
    "tox -e py312",
    "uv run pytest -q",
)


@pytest.fixture(scope="module")
def target(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("repo")
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def guard_for(name: str, target: Path) -> Guard:
    policy = load_policy(TEMPLATE)
    ctx = ResolveContext(team=None, repo_classification=policy.default_classification)
    resolved, violations = resolve_agent(load_agent(AGENTS / f"{name}.md"), policy, ctx)
    assert violations == [], violations
    return Guard.for_agent(target, "r-1", resolved)


@pytest.mark.parametrize("agent", ["developer", "qa", "qa-baseline"])
@pytest.mark.parametrize("command", RUNNERS)
def test_common_test_runners_are_allowed(agent: str, command: str, target: Path):
    assert guard_for(agent, target).command(command) == [], command


@pytest.mark.parametrize("agent", ["qa", "qa-baseline"])
@pytest.mark.parametrize(
    "command",
    [
        "npm audit fix",
        "npm audit fix --force",
        "npm audit --json fix",
        "pip-audit --fix",
        "pip-audit -r requirements.txt --fix",
        "pip-audit -o report.json",
        "pip-audit --output=report.json",
    ],
)
def test_audit_commands_that_modify_or_write_are_refused(agent: str, command: str, target: Path):
    assert guard_for(agent, target).command(command) != [], command


@pytest.mark.parametrize(
    "command", ["npm audit", "npm audit --json", "pip-audit", "pip-audit -r r.txt"]
)
def test_read_only_audits_stay_allowed(command: str, target: Path):
    assert guard_for("qa", target).command(command) == [], command


def test_a_runner_does_not_open_other_subcommands(target: Path):
    guard = guard_for("developer", target)
    for command in ("./mvnw deploy", "./gradlew publish", "make install", "yarn publish"):
        assert guard.command(command) != [], command
