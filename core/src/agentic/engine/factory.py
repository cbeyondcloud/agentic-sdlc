"""The production step-runner factory: what the engine runs each agent step on.

`StepRunners` sends each step to the backend its resolved agent names (`native`, or
an agent-CLI backend; M1-28) with:
  * the step's tool context (worktree, guard, sandbox - M1-30), or none;
  * a `spawn_agent` spawner when the backend can run child agents; the children
    are built by this same factory (DF-28);
  * the step's state folder.

`PydanticStepRunners` is the native-only form kept for existing callers.
"""

from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path

from pydantic_ai.models import Model

from agentic.agents.children import ChildSpawner
from agentic.agents.models import GatewaySettings
from agentic.agents.runner import AgentRunner
from agentic.agents.tools import ToolContext
from agentic.backends.base import Backend
from agentic.backends.capabilities import CAPABILITIES
from agentic.backends.native import NativeBackend
from agentic.engine.engine import EngineError, StepContext
from agentic.policy import ResolvedAgent
from agentic.transport import BudgetSlice, CallIdentity, Pricer


class StepRunners:
    def __init__(
        self,
        backends: Mapping[str, Backend],
        *,
        tools_for: Callable[[StepContext], ToolContext | None],
        state_dir_for: Callable[[StepContext], Path],
    ) -> None:
        self.backends = dict(backends)
        self.tools_for, self.state_dir_for = tools_for, state_dir_for

    def __call__(self, step: StepContext) -> AgentRunner:
        spawner = self.root_spawner(step.agent, step) if self._has_children(step.agent) else None
        return self._runner(step, spawner, self.state_dir_for(step))

    def root_spawner(self, agent: ResolvedAgent, step: StepContext) -> ChildSpawner:
        """The step's own spawner: the root of its child tree (caps are per tree)."""
        return ChildSpawner(
            agent,
            step.budget,
            lambda child, budget, spawner: self.child_runner(child, budget, spawner, step),
        )

    def child_runner(
        self, child: ResolvedAgent, budget: BudgetSlice, spawner: ChildSpawner, parent: StepContext
    ) -> AgentRunner:
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

    def _backend(self, agent: ResolvedAgent) -> Backend:
        backend = self.backends.get(agent.backend)
        if backend is None:
            raise EngineError(
                f"no backend {agent.backend!r} is configured (configured: {sorted(self.backends)})"
            )
        return backend

    def _has_children(self, agent: ResolvedAgent) -> bool:
        caps = CAPABILITIES.get(agent.backend)
        return caps is not None and caps.children

    def _runner(
        self, step: StepContext, spawner: ChildSpawner | None, state_dir: Path, **kwargs
    ) -> AgentRunner:
        return self._backend(step.agent).runner(
            step,
            tool_context=self.tools_for(step),
            state_dir=state_dir,
            spawner=spawner,
            **kwargs,
        )


class PydanticStepRunners(StepRunners):
    """Native-only step runners (the form SP6 introduced)."""

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
        native = NativeBackend(
            identity_for=identity_for,
            gateway=gateway,
            prices=prices,
            fallback_estimate_usd=fallback_estimate_usd,
            model_for=model_for,
            policy_version=policy_version,
        )
        super().__init__({"native": native}, tools_for=tools_for, state_dir_for=state_dir_for)


__all__ = ["PydanticStepRunners", "StepRunners"]
