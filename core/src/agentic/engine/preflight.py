"""Preflight: every check before any spend, with a report a person can act on
(M1-25, spec §8.2).

Standalone checks (M1): each agent exists and resolves against policy (tier,
model, tools, skills, limits - D35), the handoff kinds it reads and writes exist
in the run's merged catalog (D37), its backend is available, every step receives
the documents it needs, every `when` / `until` compiles and reads kinds that some
earlier step produces, every gate names a role and a real `on_reject` step, and
every action exists. Org checks (RBAC, kill switch, CLI version, budgets,
pipeline integrity) are M2-21.

A warning (an input that may be missing when a step is skipped) is reported but
does not fail preflight.
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Literal

from agentic.definitions import AgentStep, DefinitionSet, LoopStep, PipelineDefinition
from agentic.definitions.pipeline import ActionStep, GateStep
from agentic.engine.expressions import ExpressionError, cel_name, compile_condition
from agentic.handoffs.compat import CONDITIONAL, check_compatibility
from agentic.policy import OrgPolicy, ResolveContext, ResolvedAgent, resolve_agent
from agentic.policy.resolve import kind_violations

AVAILABLE_BACKENDS = frozenset({"native"})  # claude-code is M1-29, codex M2-40


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
) -> PreflightReport:
    report = PreflightReport(pipeline=pipeline.name, problems=[])
    problems = report.problems
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
        for v in kind_violations(agent, definitions.handoffs):
            problems.append(
                Problem(
                    step.id, "kind", f"{v.field}: unknown handoff kind {v.requested!r}", v.allowed
                )
            )
        if agent.backend not in AVAILABLE_BACKENDS:
            problems.append(
                Problem(
                    step.id,
                    "backend",
                    f"backend {agent.backend!r} is not available until M1-29 "
                    "(only native runs in M1)",
                    sorted(AVAILABLE_BACKENDS),
                )
            )

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

    for step in pipeline.steps:
        if isinstance(step, AgentStep | ActionStep):
            if step.when:
                condition(step.id, step.when, produced)
            agent_step(step) if isinstance(step, AgentStep) else action_step(step)
            if isinstance(step, AgentStep) and (kind := produces(step)):
                produced.add(kind)
        elif isinstance(step, LoopStep):
            body_kinds: set[str] = set()
            for inner in step.loop.steps:
                if inner.when:
                    condition(inner.id, inner.when, produced | body_kinds)
                agent_step(inner) if isinstance(inner, AgentStep) else action_step(inner)
                if isinstance(inner, AgentStep) and (kind := produces(inner)):
                    body_kinds.add(kind)
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

    agents = {**{name: defn for name, (defn, _) in definitions.agents.items()}, **report.resolved}
    for v in check_compatibility(pipeline, agents):
        if v.field == "agent":
            continue  # already reported per step
        severity = "warning" if v.field == CONDITIONAL else "error"
        problems.append(Problem(None, "compatibility", v.reason, severity=severity))
    return report


__all__ = ["AVAILABLE_BACKENDS", "PreflightReport", "Problem", "preflight"]
