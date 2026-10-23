"""Preflight: every check before any spend, with a report a person can act on
(M1-25, spec §8.2).

Standalone checks (M1): each agent exists and resolves against policy (tier,
model, tools, skills, limits - D35), the handoff kinds it reads and writes exist
in the run's merged catalog (D37), its backend is available, every step receives
the documents it needs, every `when` / `until` compiles and reads kinds that some
earlier step produces, every gate names a role and a real `on_reject` step, and
every action exists, and the repo's classification is one the policy defines.
The org checks only the control plane can answer (RBAC, kill switch, CLI version,
concurrency, budget, pipeline integrity; M2-21) are added by the CLI
(`agentic_cli.org_preflight`) as `org-<check>` problems.

A warning (an input that may be missing when a step is skipped) is reported but
does not fail preflight.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from agentic.backends import available, backend_issues
from agentic.definitions import AgentStep, DefinitionSet, LoopStep, PipelineDefinition
from agentic.definitions.pipeline import ActionStep, GateStep, step_names
from agentic.definitions.versions import VersionChoice, deprecation_state
from agentic.engine.expressions import ExpressionError, cel_name, compile_condition
from agentic.handoffs.compat import CONDITIONAL, check_compatibility
from agentic.handoffs.kinds import HandoffBase
from agentic.policy import OrgPolicy, ResolveContext, ResolvedAgent, resolve_agent
from agentic.policy.resolve import kind_violations
from agentic.skills.loader import MAX_PRELOADED

if TYPE_CHECKING:
    from agentic.sandbox.readiness import SandboxDecision

#: The word the standalone policy template puts in every tier's model (DF-225).
PLACEHOLDER_MODEL = "change-me"


class SkillAvailability(Protocol):
    """The installed skills a run would load (`agentic.skills.InstalledSkills`)."""

    def unavailable(self, name: str) -> str | None: ...

    def preloads_all(self, agent: ResolvedAgent) -> bool: ...


def skill_problems(agent: ResolvedAgent, skills: SkillAvailability) -> list[str]:
    """Why the agent cannot get every skill it was resolved with (DF-136, DF-144): a skill
    not catalogued or not installed as pinned, or more skills to inline than fit."""
    problems, usable = [], []
    for name in agent.skills:
        if why := skills.unavailable(name):
            problems.append(f"{name}: {why}")
        else:
            usable.append(name)
    inline_all = "activate_skill" not in agent.tools or skills.preloads_all(agent)
    if inline_all and len(usable) > MAX_PRELOADED:
        problems.append(
            f"{len(usable)} skills would be pre-loaded, but at most {MAX_PRELOADED} fit; "
            "give the agent activate_skill or fewer skills"
        )
    return problems


@dataclass(frozen=True)
class Problem:
    step: str | None  # None: the pipeline as a whole
    check: str
    detail: str
    policy_value: object = None
    severity: Literal["error", "warning"] = "error"


@dataclass
class PreflightReport:
    pipeline: str
    problems: list[Problem]
    resolved: dict[str, ResolvedAgent] = field(default_factory=dict)
    steps: int = 0
    gates: int = 0

    @property
    def passed(self) -> bool:
        return not any(p.severity == "error" for p in self.problems)

    def text(self) -> str:
        warnings = [p for p in self.problems if p.severity == "warning"]
        if self.passed:
            head = f"preflight passed: {self.steps} steps, {self.gates} gates"
            if warnings:
                head += f" ({len(warnings)} warning{'s' if len(warnings) > 1 else ''})"
        else:
            errors = len(self.problems) - len(warnings)
            head = f"preflight failed: {errors} problem{'s' if errors > 1 else ''}"
        return "\n".join([head, *(_line(p) for p in self.problems)])


def _line(problem: Problem) -> str:
    where = problem.step or "pipeline"
    check = f"{problem.check} (warning)" if problem.severity == "warning" else problem.check
    policy = f" (policy: {problem.policy_value})" if problem.policy_value is not None else ""
    return f"{where}: {check}: {problem.detail}{policy}"


def preflight(
    pipeline: PipelineDefinition,
    definitions: DefinitionSet,
    policy: OrgPolicy,
    context: ResolveContext,
    actions: Collection[str] = (),
    sandbox: "SandboxDecision | None" = None,
    available_backends: Collection[str] | None = None,
    skills: SkillAvailability | None = None,
    honoured: Mapping[str, Collection[str]] | None = None,
    skill_versions: Mapping[str, str] | None = None,
) -> PreflightReport:
    """Every check before any spend. `honoured`: per backend, the capability settings the
    caller's wiring makes good (`backend_issues`). `skill_versions`: each catalogued
    skill's version, for the deprecation check (M2-15)."""
    report = PreflightReport(pipeline=pipeline.name, problems=[])
    problems = report.problems
    required = policy.required_steps.get(pipeline.name, [])
    present = {name for step in pipeline.steps for name in step_names(step)}
    for name in required:  # M2-23: whatever dropped it (an overlay, a team pipeline)
        if name not in present:
            problems.append(
                Problem(
                    None,
                    "required-step",
                    f"org policy requires {name!r} in {pipeline.name} (org.yaml "
                    "required_steps), but the pipeline has no step, gate or loop of that name",
                    list(required),
                )
            )
    _version_problem(problems, None, definitions.versions.get(("pipeline", pipeline.name)))
    today = definitions.rules.today if definitions.rules is not None else datetime.now(UTC).date()
    versions_checked: set[str] = set()

    def versions(step: AgentStep, agent: ResolvedAgent) -> None:
        """The agent's version and its skills' versions: deprecated warns, past the
        date refuses (M2-15); once per agent."""
        if step.agent in versions_checked:
            return
        versions_checked.add(step.agent)
        _version_problem(problems, step.id, definitions.versions.get(("agent", step.agent)))
        for skill in agent.skills if skill_versions is not None else ():
            version = skill_versions.get(skill)
            gone, blocked = (
                deprecation_state("skill", skill, version, policy.deprecated, today)
                if version is not None
                else (None, False)
            )
            if gone is not None:
                when = "refused since" if blocked else "refused from"
                note = f" - {gone.note}" if gone.note else ""
                problems.append(
                    Problem(
                        step.id,
                        "version",
                        f"skill {skill}@{version}: deprecated, {when} "
                        f"{gone.blocked_from.isoformat()}{note}",
                        severity="error" if blocked else "warning",
                    )
                )

    backends = available() if available_backends is None else available_backends
    if sandbox is not None and sandbox.refusal:  # the regime needs an OS sandbox (D38)
        problems.append(Problem(None, "sandbox", sandbox.refusal))
    elif sandbox is not None and sandbox.warning:
        problems.append(Problem(None, "sandbox", sandbox.warning, severity="warning"))
    classification = context.repo_classification or policy.default_classification
    if classification not in policy.classifications:  # once for the repo (M2-21)
        problems.append(
            Problem(
                None,
                "classification",
                f"the repo's classification {classification!r} is not defined in org policy "
                "(classification in .agentic/config.yaml)",
                sorted(policy.classifications),
            )
        )
    produced: set[str] = set(pipeline.inputs)
    earlier_agent_steps: list[str] = []

    def agent_step(step: AgentStep) -> None:
        report.steps += 1
        earlier_agent_steps.append(step.id)
        if step.agent not in definitions.agents:
            problems.append(Problem(step.id, "agent", f"no agent named {step.agent!r}"))
            return
        agent, violations = resolve_agent(definitions.agent(step.agent), policy, context)
        for v in violations:
            problems.append(Problem(step.id, "policy", f"{v.field}: {v.reason}", v.allowed))
        if agent is None:
            return
        report.resolved[step.agent] = agent
        versions(step, agent)
        if PLACEHOLDER_MODEL in agent.model:  # the template's tiers were never edited
            problems.append(
                Problem(
                    step.id,
                    "model",
                    f"tier {agent.tier!r} still names the placeholder model {agent.model!r}: "
                    "set a real model id for it in tiers.yaml in your policy folder",
                    agent.model,
                )
            )
        for v in kind_violations(agent, definitions.handoffs):
            problems.append(
                Problem(
                    step.id, "kind", f"{v.field}: unknown handoff kind {v.requested!r}", v.allowed
                )
            )
        for issue in backend_issues(agent, policy, available=backends, honoured=honoured):
            severity = "error" if issue.action == "block" else "warning"
            problems.append(
                Problem(step.id, "backend", issue.detail, agent.backend, severity=severity)
            )
        if skills is not None:
            for detail in skill_problems(agent, skills):
                problems.append(Problem(step.id, "skills", detail))

    def condition(owner: str, text: str, can_read: set[str]) -> None:
        try:
            compiled = compile_condition(text)
        except ExpressionError as error:
            problems.append(Problem(owner, "condition", str(error)))
            return
        names = {cel_name(kind): kind for kind in can_read}
        for name in sorted(compiled.kinds - set(names)):
            kind = (
                name.replace("_", "-")
                if name.replace("_", "-") in definitions.handoffs.kinds()
                else name
            )
            problems.append(
                Problem(
                    owner, "condition", f"{text!r} reads {kind!r}, which no earlier step produces"
                )
            )

    def produces(step: AgentStep) -> str | None:
        agent = report.resolved.get(step.agent)
        return agent.produces if agent is not None else None

    def action_step(step: ActionStep) -> None:
        report.steps += 1
        earlier_agent_steps.append(step.id)
        if step.action not in actions:
            problems.append(
                Problem(step.id, "action", f"unknown action {step.action!r}", sorted(actions))
            )

    made: list[str] = []  # every kind an agent step produces, in pipeline order
    outside: list[tuple[int, AgentStep, str]] = []  # (position in `made`, step, kind)

    def judging(step: AgentStep, kind: str) -> None:
        """A step outside any loop whose handoff can report a failure (a review, a test
        report) and that no later step replaces (a baseline test report is replaced by
        QA's): nothing can send a failure back, so the run pauses at its end (DF-265)."""
        schema = definitions.handoffs.schemas.get(kind)
        if schema is None or getattr(schema.model, "failure", None) is HandoffBase.failure:
            return
        problems.append(
            Problem(
                step.id,
                "judging-step",
                f"its {kind} can report a failure, but the step is outside any loop: a "
                f"failing {kind} cannot be routed back, so the run pauses failing-handoff "
                "at its end (put the step in a loop whose `until` reads it)",
                severity="warning",
            )
        )

    for step in pipeline.steps:
        if isinstance(step, AgentStep | ActionStep):
            if step.when:
                condition(step.id, step.when, produced)
            agent_step(step) if isinstance(step, AgentStep) else action_step(step)
            if isinstance(step, AgentStep) and (kind := produces(step)):
                produced.add(kind)
                outside.append((len(made), step, kind))
                made.append(kind)
        elif isinstance(step, LoopStep):
            body_kinds: set[str] = set()
            for inner in step.loop.steps:
                if inner.when:
                    condition(inner.id, inner.when, produced | body_kinds)
                agent_step(inner) if isinstance(inner, AgentStep) else action_step(inner)
                if isinstance(inner, AgentStep) and (kind := produces(inner)):
                    body_kinds.add(kind)
                    made.append(kind)
            condition(step.loop.id, step.loop.until, produced | body_kinds)
            produced |= body_kinds
        elif isinstance(step, GateStep):
            report.gates += 1
            if not step.approver_role.strip():
                problems.append(Problem(step.gate, "gate", "approver_role is empty"))
            if step.on_reject and step.on_reject not in earlier_agent_steps:
                problems.append(
                    Problem(
                        step.gate,
                        "gate",
                        f"on_reject names {step.on_reject!r}, which is not a step before the gate",
                        list(earlier_agent_steps),
                    )
                )

    for position, step, kind in outside:
        if kind not in made[position + 1 :]:  # its handoff is the run's last word on `kind`
            judging(step, kind)

    agents = {**{name: defn for name, (defn, _) in definitions.agents.items()}, **report.resolved}
    for v in check_compatibility(pipeline, agents):
        if v.field == "agent":
            continue  # already reported per step
        severity = "warning" if v.field == CONDITIONAL else "error"
        problems.append(Problem(None, "compatibility", v.reason, severity=severity))
    return report


def _version_problem(
    problems: list[Problem], step: str | None, choice: VersionChoice | None
) -> None:
    """A deprecated version: a warning before its date, an error from it (M2-15)."""
    if choice is not None and choice.deprecation is not None:
        severity: Literal["error", "warning"] = "error" if choice.blocked else "warning"
        problems.append(Problem(step, "version", choice.describe(), severity=severity))


__all__ = ["PreflightReport", "Problem", "SkillAvailability", "preflight", "skill_problems"]
