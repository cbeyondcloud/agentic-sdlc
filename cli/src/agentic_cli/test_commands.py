"""The repo's test command must be one its Developer and QA agents may run (DF-359, owner
decision 2026-11-07), checked in preflight, before any spend.

A run refuses every shell command the agent's resolved allow-list does not name, and a deny
rule beats an allow rule; that stays. What preflight adds: the repo's test command(s), read
from its manifests (`detect`, one reader per manifest), are put to the very guard a run
applies (`Guard.for_agent(...).command`), for every step whose agent runs shell commands
(`run_shell`) and writes a dev report or a test report - the Developer's and QA's. Agents
that write anything else are not checked.

Each manifest gives one command with its equivalent spellings (`pytest`, `python -m
pytest`, `uv run pytest`, `poetry run pytest`; `npm test` and `npm run test`; a build
wrapper or the tool itself). One allowed spelling of one detected command is enough: a
polyglot repo fails only when the agent may run none of them. A failure is reported once
per agent, with its steps, every option and the allow-list line(s) to add and where: the
agent type's `shell:` in the org policy (agent-types.yaml), and the agent file's own
`shell:` when it sets one. A command a deny rule covers names the rule: no allow line helps.

The readers read files only (`tomllib`, `json`, a regex; UTF-8 with or without a BOM, any
line ending), so they behave the same on Windows, macOS and Linux; a wrapper is found as
`mvnw`/`mvnw.cmd` or `gradlew`/`gradlew.bat`, and written the way the allow-lists spell it
(`./mvnw test`; the guard reads `mvnw.cmd` as `mvnw`).
"""

import json
import re
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from agentic.definitions import AgentStep, DefinitionSet, LoopStep, PipelineDefinition
from agentic.engine.preflight import PreflightReport, Problem
from agentic.policy import OrgPolicy, ResolvedAgent

#: The handoffs of the agents that run the repo's tests: the Developer's and QA's.
TESTING_KINDS = frozenset({"dev-report", "test-report"})
SHELL_TOOL = "run_shell"
CHECK = "test-command"
DENY_WALLS = frozenset({"denied-command", "framework", "destructive"})
PYTEST_SPELLINGS = ("pytest", "python -m pytest", "uv run pytest", "poetry run pytest")


@dataclass(frozen=True)
class RepoTestCommand:
    command: str  # what an agent types, e.g. "go test ./..."
    allow: str  # the allow-list line that covers it, e.g. "go test"
    source: str  # the manifest it was read from
    alternatives: tuple[str, ...] = ()  # equivalent spellings, each its own allow line

    @property
    def options(self) -> list[tuple[str, str]]:
        """(command, allow line) for the preferred spelling, then each equivalent."""
        return [(self.command, self.allow)] + [(a, a) for a in self.alternatives]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig")  # Windows editors write a BOM
    except (OSError, UnicodeDecodeError):
        return None


def _toml(path: Path) -> dict:
    text = _read(path)
    try:
        return tomllib.loads(text) if text is not None else {}
    except tomllib.TOMLDecodeError:
        return {}


def _one_of(preferred: str, spellings: Iterable[str], allow: str | None = None, *, source: str):
    rest = tuple(s for s in dict.fromkeys(spellings) if s != preferred)
    return RepoTestCommand(preferred, allow or preferred, source, rest)


_PYTEST_REQUIREMENT = re.compile(r"^pytest(?:$|[\s\[<>=!~;,(])", re.IGNORECASE)


def _names_pytest(data: dict) -> bool:
    """pytest among the project's dependencies: optional, dependency groups or Poetry's."""
    requirements: list[object] = []
    project = data.get("project", {})
    requirements += project.get("dependencies", []) if isinstance(project, dict) else []
    for table in (project.get("optional-dependencies", {}), data.get("dependency-groups", {})):
        for group in table.values() if isinstance(table, dict) else ():
            requirements += group if isinstance(group, list) else []
    if any(isinstance(r, str) and _PYTEST_REQUIREMENT.match(r.strip()) for r in requirements):
        return True
    poetry = data.get("tool", {}).get("poetry", {})
    tables = [poetry.get("dev-dependencies", {}), poetry.get("dependencies", {})]
    tables += [g.get("dependencies", {}) for g in poetry.get("group", {}).values()]
    return any(isinstance(t, dict) and "pytest" in map(str.lower, t) for t in tables)


def _pytest_source(root: Path) -> str | None:
    data = _toml(root / "pyproject.toml")
    if "pytest" in data.get("tool", {}) or _names_pytest(data):
        return "pyproject.toml"
    if (root / "pytest.ini").is_file():
        return "pytest.ini"
    for name, section in (("setup.cfg", "tool:pytest"), ("tox.ini", "pytest")):
        if re.search(rf"(?m)^\[{re.escape(section)}\]", _read(root / name) or ""):
            return name
    for name in ("conftest.py", "tests/conftest.py"):
        if (root / name).is_file():
            return name
    tests = root / "tests"
    if tests.is_dir() and (any(tests.glob("test_*.py")) or any(tests.glob("*_test.py"))):
        return "tests/"
    return None


def _python(root: Path) -> RepoTestCommand | None:
    source = _pytest_source(root)
    if source is None:
        return None
    preferred = next(
        (
            run
            for lock, run in (("uv.lock", "uv run pytest"), ("poetry.lock", "poetry run pytest"))
            if (root / lock).is_file()
        ),
        "pytest",
    )
    return _one_of(preferred, PYTEST_SPELLINGS, source=source)


def _node(root: Path) -> RepoTestCommand | None:
    try:
        data = json.loads(_read(root / "package.json") or "{}")
    except json.JSONDecodeError:
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    script = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(script, str) or not script.strip() or "no test specified" in script:
        return None  # none, or the placeholder `npm init` writes
    runner = next(
        (
            name
            for lock, name in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"))
            if (root / lock).is_file()
        ),
        "npm",
    )
    spellings = [f"{runner} test", f"{runner} run test", "npm test", "npm run test"]
    return _one_of(f"{runner} test", spellings, source="package.json")


def _make(root: Path) -> RepoTestCommand | None:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        if re.search(r"(?m)^test\s*:(?!=)", _read(root / name) or ""):
            return RepoTestCommand("make test", "make test", name)
    return None


def _wrapped(root: Path, wrapper: str, suffix: str, tool: str, source: str) -> RepoTestCommand:
    """The build wrapper when the repo has one (`gradlew` or `gradlew.bat`), else the tool;
    either spelling is an option."""
    script = f"./{wrapper} test"
    has = (root / wrapper).is_file() or (root / f"{wrapper}{suffix}").is_file()
    return _one_of(script if has else f"{tool} test", [script, f"{tool} test"], source=source)


def _go(root: Path) -> RepoTestCommand | None:
    if not (root / "go.mod").is_file():
        return None
    return RepoTestCommand("go test ./...", "go test", "go.mod")


def _maven(root: Path) -> RepoTestCommand | None:
    if not (root / "pom.xml").is_file():
        return None
    return _wrapped(root, "mvnw", ".cmd", "mvn", "pom.xml")


GRADLE_FILES = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")


def _gradle(root: Path) -> RepoTestCommand | None:
    source = next((n for n in GRADLE_FILES if (root / n).is_file()), None)
    if source is None:
        return None
    return _wrapped(root, "gradlew", ".bat", "gradle", source)


def _dotnet(root: Path) -> RepoTestCommand | None:
    for pattern in ("*.sln", "*.csproj", "*/*.csproj", "*/*/*.csproj"):
        if found := next(iter(sorted(root.glob(pattern))), None):
            return RepoTestCommand("dotnet test", "dotnet test", found.relative_to(root).as_posix())
    return None


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
    _dotnet,
    _cargo,
)


def detect(repo: Path) -> list[RepoTestCommand]:
    """The test commands the repo's manifests name, each once."""
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
    """One preflight error per Developer or QA agent that may run none of the repo's test
    commands, in any spelling. `guard_for(agent)` is the run's guard (`Guard.for_agent`)."""
    commands = detect(repo)
    if not commands:
        return []
    steps: dict[str, list[str]] = {}  # agent -> its steps, in pipeline order
    for step in _agent_steps(pipeline):
        agent = report.resolved.get(step.agent)
        if agent is not None and _runs_tests(agent):
            steps.setdefault(step.agent, []).append(step.id)
    found: list[Problem] = []
    for name, ids in steps.items():
        agent = report.resolved[name]
        guard = guard_for(agent)
        refused: list[tuple[RepoTestCommand, str, str, list]] = []
        allowed = False
        for test in commands:
            for command, allow in test.options:
                blocks = guard.command(command)  # type: ignore[attr-defined]
                if not blocks:  # one allowed spelling of one test command is enough
                    allowed = True
                    break
                refused.append((test, command, allow, blocks))
            if allowed:
                break
        if not allowed:
            context = (definitions, policy, policy_file, guard_for)
            detail = _detail(name, agent, ids, commands, refused, *context)
            found.append(Problem(ids[0], CHECK, detail, agent.shell))
    return found


def _type_allows(agent, allow: str, policy: OrgPolicy, guard_for) -> bool:
    """Whether the agent's type already allows the spelling (the agent file narrows it)."""
    agent_type = policy.agent_types.get(agent.type)
    if agent_type is None:
        return False
    widest = guard_for(agent.model_copy(update={"shell": list(agent_type.shell)}))
    return not [b for b in widest.command(allow) if b.wall == "not-allowed"]


def _detail(name, agent, ids, commands, refused, definitions, policy, policy_file, guard_for):
    where_steps = f"step {ids[0]}" if len(ids) == 1 else f"steps {', '.join(ids)}"
    listed = ", ".join(f"`{t.command}` (from {t.source})" for t in commands)
    head = (
        f"agent {name} (type {agent.type}; {where_steps}) may run none of the repo's test "
        f"commands: {listed}"
    )
    denied = [(c, b) for _, c, _, b in refused if any(x.wall in DENY_WALLS for x in b)]
    lines = list(dict.fromkeys(a for _, c, a, b in refused if c not in dict(denied)))
    deny_note = "; ".join(
        dict.fromkeys(f"`{c}`: {x.why}" for c, b in denied for x in b if x.wall in DENY_WALLS)
    )
    if not lines:
        return (
            f"{head}. Every spelling is refused by a deny rule ({deny_note}). A deny rule "
            "beats an allow rule, so no allow-list line helps: remove the rule from the "
            "policy that sets it (org deny, the team's teams/<team>.yaml deny, or the agent "
            "file's deny), or run the tests another way"
        )
    in_type = [a for a in lines if _type_allows(agent, a, policy, guard_for)]
    narrows = definitions.agent(name).shell is not None
    where = []
    if narrows and in_type:  # the type allows some already: only the agent file needs one
        lines = in_type
    else:
        where.append(f"`shell:` of type {agent.type} in {policy_file} (org policy)")
    if narrows:
        layer = definitions.agents[name][1]
        source = definitions.agent_files.get(name) or f"the agent file of {name}"
        where.append(f"`shell:` in {source} ({layer} agent file)")
    text = (
        f"{head}. One allowed test command is enough. Add one of these lines: "
        + ", ".join(f'"{a}"' for a in lines)
        + " to "
        + " and to ".join(where)
    )
    if deny_note:
        text += f" (refused by a deny rule, which no allow line lifts: {deny_note})"
    return text


__all__ = ["DETECTORS", "RepoTestCommand", "detect", "problems"]
