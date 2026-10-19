"""Checking a pipeline before it runs: does every step receive the documents it needs?

Each agent declares `consumes` (handoff kinds it reads) and `produces` (the kind
it writes). Walking the pipeline in order, every consumed kind must already be
available: produced by an earlier step or supplied as a pipeline input.

Loops are the one exception to "earlier": inside a loop, a step may consume what
a later step in the same loop produces, because it gets the previous iteration's
document (e.g. the Developer reads the Review from the last round). On the first
iteration that document does not exist yet; that is the intended feedback
pattern, so it is not reported.

A kind produced only by a step with a `when` may be missing at run time, when
that step is skipped: a consumer of it gets a warning (`field="consumes-if-run"`),
not an error (DF-05).

Pass *resolved* agents (agentic.policy.ResolvedAgent): an agent file may leave
consumes/produces unset and inherit them from its agent type (D35), and only the
resolved agent knows the inherited values. A raw definition's unset lists count
as empty.
"""

from collections.abc import Mapping

from agentic.definitions import AgentDefinition, AgentStep, LoopStep, PipelineDefinition
from agentic.policy import ResolvedAgent, Violation

#: The Violation field of a warning: the input may be missing when a step is skipped.
CONDITIONAL = "consumes-if-run"


def check_compatibility(
    pipeline: PipelineDefinition, agents: Mapping[str, ResolvedAgent | AgentDefinition]
) -> list[Violation]:
    violations: list[Violation] = []
    available: set[str] = set(pipeline.inputs)
    conditional: dict[str, str] = {}  # kind -> the step with a `when` that produces it

    def check_step(step: AgentStep, can_use: set[str], maybe: dict[str, str]) -> None:
        agent = agents.get(step.agent)
        if agent is None:
            reason = f"step '{step.id}' uses unknown agent '{step.agent}'"
            violations.append(Violation("agent", step.agent, sorted(agents), reason))
            return
        for kind in agent.consumes or []:
            if kind in can_use:
                continue
            if kind in maybe:
                reason = (
                    f"step '{step.id}' ({step.agent}) reads '{kind}', which may be missing "
                    f"when '{maybe[kind]}' is skipped (its `when` is false)"
                )
                violations.append(Violation(CONDITIONAL, kind, sorted(can_use), reason))
                continue
            reason = (
                f"step '{step.id}' ({step.agent}) needs '{kind}', but no earlier step produces it"
            )
            violations.append(Violation("consumes", kind, sorted(can_use), reason))

    def produce(step: AgentStep, into: set[str], maybe: dict[str, str]) -> None:
        agent = agents.get(step.agent)
        if agent is None or not agent.produces:
            return
        if step.when:
            if agent.produces not in into:
                maybe[agent.produces] = step.id
        else:
            into.add(agent.produces)
            maybe.pop(agent.produces, None)

    for step in pipeline.steps:
        if isinstance(step, AgentStep):
            check_step(step, available, conditional)
            produce(step, available, conditional)
        elif isinstance(step, LoopStep):
            inner = [s for s in step.loop.steps if isinstance(s, AgentStep)]
            in_loop: set[str] = set()
            in_loop_maybe: dict[str, str] = {}
            for inner_step in inner:
                produce(inner_step, in_loop, in_loop_maybe)
            for inner_step in inner:
                check_step(inner_step, available | in_loop, {**conditional, **in_loop_maybe})
            available |= in_loop
            for kind, producer in in_loop_maybe.items():
                if kind not in available:
                    conditional[kind] = producer
        # gates and actions neither consume nor produce handoffs

    return violations
