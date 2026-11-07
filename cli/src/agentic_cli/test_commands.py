"""The repo's test command must be one its Developer and QA agents may run (DF-359, owner
decision 2026-11-07), checked in preflight, before any spend.

A run refuses every shell command the agent's resolved allow-list does not name, and a deny
rule beats an allow rule; that stays. What preflight adds: the repo's test command(s), read
from its manifests (`detect`, one table row per manifest), are put to the very guard a run
applies (`Guard.for_agent(...).command`), for every step whose agent runs shell commands and
writes a dev report or a test report. A command the guard would refuse fails preflight with
the step, the agent, the command and the allow-list line to add, and where to add it: the
agent type's `shell:` in the org policy (agent-types.yaml), and the agent file's own
`shell:` when it sets one. A command a deny rule covers names the rule: no allow line helps.

The table reads files only (`tomllib`, `json`, a regex), so it behaves the same on Windows,
macOS and Linux; a wrapper script is found as `mvnw` or `mvnw.cmd`, and the command is
written the way the allow-lists spell it (`./mvnw test`; the guard reads `mvnw.cmd` as
`mvnw`).
"""

import json
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agentic.definitions import AgentStep, DefinitionSet, LoopStep, PipelineDefinition
from agentic.engine.preflight import PreflightReport, Problem
from agentic.policy import OrgPolicy, ResolvedAgent

#: The handoffs of the agents that run the repo's tests: the Developer's and QA's.
TESTING_KINDS = frozenset({"dev-report", "test-report"})
SHELL_TOOL = "run_shell"
CHECK = "test-command"


@dataclass(frozen=True)
class RepoTestCommand:
    command: str  # what an agent types, e.g. "go test ./..."
    allow: str  # the allow-list line that covers it, e.g. "go test"
    source: str  # the manifest it was read from


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _toml(path: Path) -> dict:
    text = _read(path)
    try:
        return tomllib.loads(text) if text is not None else {}
    except tomllib.TOMLDecodeError:
        return {}


def _python(root: Path) -> RepoTestCommand | None:
    pyproject = root / "pyproject.toml"
    if "pytest" in _toml(pyproject).get("tool", {}):
        source = "pyproject.toml"
    elif (root / "pytest.ini").is_file():
        source = "pytest.ini"
    elif re.search(r"(?m)^\[tool:pytest\]", _read(root / "setup.cfg") or ""):
        source = "setup.cfg"
    else:
        return None
    command = "uv run pytest" if (root / "uv.lock").is_file() else "pytest"
    return RepoTestCommand(command, command, source)


def _node(root: Path) -> RepoTestCommand | None:
    try:
        scripts = json.loads(_read(root / "package.json") or "{}").get("scripts") or {}
    except (json.JSONDecodeError, AttributeError):
        return None
    script = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(script, str) or not script.strip() or "no test specified" in script:
        return None
    runner = next(
        (
            name
            for lock, name in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"))
            if (root / lock).is_file()
        ),
        "npm",
    )
    return RepoTestCommand(f"{runner} test", f"{runner} test", "package.json")


def _make(root: Path) -> RepoTestCommand | None:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        if re.search(r"(?m)^test\s*:(?!=)", _read(root / name) or ""):
            return RepoTestCommand("make test", "make test", name)
    return None


def _wrapper(root: Path, name: str) -> bool:
    return (root / name).is_file() or (root / f"{name}.cmd").is_file()


def _go(root: Path) -> RepoTestCommand | None:
    return (
        RepoTestCommand("go test ./...", "go test", "go.mod")
        if (root / "go.mod").is_file()
        else None
    )


def _maven(root: Path) -> RepoTestCommand | None:
    if not (root / "pom.xml").is_file():
        return None
    command = "./mvnw test" if _wrapper(root, "mvnw") else "mvn test"
    return RepoTestCommand(command, command, "pom.xml")


def _gradle(root: Path) -> RepoTestCommand | None:
    source = next((n for n in ("build.gradle", "build.gradle.kts") if (root / n).is_file()), None)
    if source is None:
        return None
    command = "./gradlew test" if _wrapper(root, "gradlew") else "gradle test"
    return RepoTestCommand(command, command, source)


def _cargo(root: Path) -> RepoTestCommand | None:
    path = root / "Cargo.toml"
    return RepoTestCommand("cargo test", "cargo test", "Cargo.toml") if path.is_file() else None


#: One reader per manifest, in the order the commands are reported.
DETECTORS: tuple[Callable[[Path], RepoTestCommand | None], ...] = (
    _python,
    _node,
    _make,
    _go,
    _maven,
    _gradle,
    _cargo,
)


def detect(repo: Path) -> list[RepoTestCommand]:
    """The test commands the repo's top-level manifests name, each once."""
    found: dict[str, RepoTestCommand] = {}
    for reader in DETECTORS:
        if (command := reader(repo)) is not None:
            found.setdefault(command.command, command)
    return list(found.values())


def _agent_steps(pipeline: PipelineDefinition) -> list[AgentStep]:
    steps: list[AgentStep] = []
    for step in pipeline.steps:
        inner = step.loop.steps if isinstance(step, LoopStep) else [step]
        steps += [s for s in inner if isinstance(s, AgentStep)]
    return steps


def _runs_tests(agent: ResolvedAgent) -> bool:
    return SHELL_TOOL in agent.tools and agent.produces in TESTING_KINDS


def problems(
    report: PreflightReport,
    pipeline: PipelineDefinition,
    definitions: DefinitionSet,
    policy: OrgPolicy,
    *,
    repo: Path,
    policy_file: Path,
    guard_for: Callable[[ResolvedAgent], object],
) -> list[Problem]:
    """A preflight error per (step, test command) the step's agent may not run.
    `guard_for(agent)` is the run's guard for that agent (`Guard.for_agent`)."""
    commands = detect(repo)
    if not commands:
        return []
    found: list[Problem] = []
    guards: dict[str, object] = {}
    for step in _agent_steps(pipeline):
        agent = report.resolved.get(step.agent)
        if agent is None or not _runs_tests(agent):
            continue
        guard = guards.setdefault(step.agent, guard_for(agent))
        for test in commands:
            blocks = guard.command(test.command)  # type: ignore[attr-defined]
            if not blocks:
                continue
            in_type = _type_allows(agent, test, policy, guard_for)
            detail = _detail(step.agent, agent, test, blocks, definitions, policy_file, in_type)
            found.append(Problem(step.id, CHECK, detail, agent.shell))
    return found


def _type_allows(agent, test, policy, guard_for) -> bool:
    """Whether the agent's type already allows the command (the agent file narrows it)."""
    agent_type = policy.agent_types.get(agent.type)
    if agent_type is None:
        return False
    widest = guard_for(agent.model_copy(update={"shell": list(agent_type.shell)}))
    return not [b for b in widest.command(test.command) if b.wall == "not-allowed"]


def _detail(name, agent, test, blocks, definitions, policy_file, in_type: bool) -> str:
    head = (
        f"agent {name} (type {agent.type}) may not run the repo's test command "
        f"`{test.command}` (from {test.source})"
    )
    denied = [b.why for b in blocks if b.wall in ("denied-command", "framework", "destructive")]
    if denied:
        return (
            f"{head}: {'; '.join(denied)}. A deny rule beats an allow rule, so no allow-list "
            "line helps: remove the rule from the policy that sets it (org deny, the team's "
            "teams/<team>.yaml deny, or the agent file's deny), or run the tests another way"
        )
    where = [] if in_type else [f"`shell:` of type {agent.type} in {policy_file} (org policy)"]
    if definitions.agent(name).shell is not None:  # the agent narrows its type: its list too
        layer = definitions.agents[name][1]
        source = definitions.agent_files.get(name) or f"the agent file of {name}"
        where.append(f"`shell:` in {source} ({layer} agent file)")
    where = where or [f"`shell:` of type {agent.type} in {policy_file} (org policy)"]
    return (
        f"{head}: it is not on the agent's shell allow-list. Add the line "
        f'"{test.allow}" to ' + " and to ".join(where)
    )


__all__ = ["DETECTORS", "RepoTestCommand", "detect", "problems"]
