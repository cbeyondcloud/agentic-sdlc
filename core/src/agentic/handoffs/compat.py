"""Checking a pipeline before it runs: does every step receive the documents it needs?

Each agent declares `consumes` (handoff kinds it reads) and `produces` (the kind
it writes). Walking the pipeline in order, every consumed kind must already be
available: produced by an earlier step or supplied as a pipeline input.

Loops are the one exception to "earlier": inside a loop, a step may consume what
a later step in the same loop produces, because it gets the previous iteration's
document (e.g. the Developer reads the Review from the last round).
"""

from collections.abc import Mapping

from agentic.definitions import AgentDefinition, AgentStep, LoopStep, PipelineDefinition
from agentic.policy import Violation


def check_compatibility(
    pipeline: PipelineDefinition, agents: Mapping[str, AgentDefinition]
) -> list[Violation]:
    violations: list[Violation] = []
    available: set[str] = set(pipeline.inputs)

    def check_step(step: AgentStep, can_use: set[str]) -> None:
        agent = agents.get(step.agent)
        if agent is None:
            reason = f"step '{step.id}' uses unknown agent '{step.agent}'"
            violations.append(Violation("agent", step.agent, sorted(agents), reason))
            return
        for kind in agent.consumes:
            if kind not in can_use:
                violations.append(Violation(
                    "consumes", kind, sorted(can_use),
                    f"step '{step.id}' ({agent.name}) needs '{kind}', "
                    "but no earlier step produces it",
                ))  # fmt: skip

    for step in pipeline.steps:
        if isinstance(step, AgentStep):
            check_step(step, available)
            if (agent := agents.get(step.agent)) and agent.produces:
                available.add(agent.produces)
        elif isinstance(step, LoopStep):
            inner = [s for s in step.loop.steps if isinstance(s, AgentStep)]
            inner_agents = [agents[s.agent] for s in inner if s.agent in agents]
            produced_in_loop = {agent.produces for agent in inner_agents if agent.produces}
            for inner_step in inner:
                check_step(inner_step, available | produced_in_loop)
            available |= produced_in_loop
        # gates and actions neither consume nor produce handoffs

    return violations
