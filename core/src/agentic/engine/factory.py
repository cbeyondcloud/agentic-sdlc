"""The production step-runner factory: what the engine runs each agent step on.

For each step it builds a `PydanticAIRunner` with:
  * the agent's metered model (every call through TokenLean, charged to the
    step's budget slice) - or `model_for`, for tests;
  * the agent's tools (`build_tools`) when the step has a tool context
    (worktree, guard, sandbox - M1-30), plus `spawn_agent` from a ChildSpawner
    whose children are built by this same factory (DF-28);
  * the step's output contract (children get none: their output goes back to
    the parent, not into the pipeline);
  * the run id, so a paused run's state is bound to it.
"""

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from pydantic_ai.models import Model

from agentic.agents.children import ChildSpawner
from agentic.agents.metering import CostLog
from agentic.agents.models import GatewaySettings, metered_model
from agentic.agents.runner import PydanticAIRunner
from agentic.agents.tools import ToolContext, build_tools
from agentic.engine.engine import StepContext
from agentic.policy import ResolvedAgent
from agentic.transport import BudgetSlice, CallIdentity, Pricer


class PydanticStepRunners:
    def __init__(
        self,
        *,
        identity_for: Callable[[StepContext], CallIdentity],
        gateway: GatewaySettings,
        prices: Pricer,
        fallback_estimate_usd: Decimal,
        tools_for: Callable[[StepContext], ToolContext | None],
        state_dir_for: Callable[[StepContext], Path],
        model_for: Callable[[StepContext], Model] | None = None,
        policy_version: str | None = None,
    ) -> None:
        self.identity_for, self.gateway, self.prices = identity_for, gateway, prices
        self.fallback_estimate_usd = fallback_estimate_usd
        self.tools_for, self.state_dir_for, self.model_for = tools_for, state_dir_for, model_for
        self.policy_version = policy_version

    def __call__(self, step: StepContext) -> PydanticAIRunner:
        return self._runner(step, self.root_spawner(step.agent, step), self.state_dir_for(step))

    def root_spawner(self, agent: ResolvedAgent, step: StepContext) -> ChildSpawner:
        """The step's own spawner: the root of its child tree (caps are per tree)."""
        return ChildSpawner(
            agent,
            step.budget,
            lambda child, budget, spawner: self.child_runner(child, budget, spawner, step),
        )

    def child_runner(
        self, child: ResolvedAgent, budget: BudgetSlice, spawner: ChildSpawner, parent: StepContext
    ) -> PydanticAIRunner:
        """A spawned child's runner: no contract, its own state folder, its own spawner."""
        context = StepContext(
            run_id=parent.run_id,
            step=parent.step,
            iteration=parent.iteration,
            agent=child,
            budget=budget,
            contract=None,
        )
        state_dir = self.state_dir_for(parent) / "children" / child.definition.name
        return self._runner(context, spawner, state_dir, is_child=True)

    def _runner(
        self, step: StepContext, spawner: ChildSpawner, state_dir: Path, *, is_child: bool = False
    ) -> PydanticAIRunner:
        costs = CostLog()
        if self.model_for is not None:
            model = self.model_for(step)
        else:
            model, costs = metered_model(
                step.agent,
                self.identity_for(step),
                self.gateway,
                budget=step.budget,
                price_for=self.prices,
                fallback_estimate_usd=self.fallback_estimate_usd,
                log=costs,
            )
        tool_context = self.tools_for(step)
        tools = build_tools(step.agent, tool_context) if tool_context is not None else []
        tools += spawner.tools()
        return PydanticAIRunner(
            step.agent,
            model,
            budget=step.budget,
            state_dir=state_dir,
            tools=tools,
            costs=costs,
            is_child=is_child,
            contract=step.contract,
            run_id=step.run_id,
            policy_version=self.policy_version,
        )


__all__ = ["PydanticStepRunners"]
