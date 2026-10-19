"""Child agents under admin limits (M1-17, spec §8.4).

A parent with the `spawn_agent` capability may start children for sub-tasks.
The limits come from the parent's resolved `children` (its agent type's caps,
lowered by its team): how many in total, how many at once, how deep. A child
can only ever narrow:

  * tools    - a subset of the parent's (asking for more is refused);
  * deny     - the parent's deny lists plus the child's own (never fewer);
  * limits   - the lower of what is asked and the parent's `per_child` caps;
  * budget   - a slice of the parent's ledger, closed when the child ends.

A spawn that breaks a limit is not an exception: it returns `stopped:
"refused"` with the reason, and the parent decides what to do. A child that
hits its own limit returns its partial output marked `stopped:` (budget, turns,
time, stuck). A step or run limit hit inside a child reaches the engine.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from pydantic_ai import Tool

from agentic.agents.runner import AgentRunner, AgentTask
from agentic.definitions import Limits
from agentic.policy import ResolvedAgent
from agentic.transport import BudgetError, BudgetSlice, LimitHit

RunnerFactory = Callable[[ResolvedAgent, BudgetSlice, int], AgentRunner]


@dataclass(frozen=True)
class ChildSpec:
    name: str
    instructions: str
    prompt: str
    tools: list[str] | None = None  # None: the parent's tools
    limits: Limits | None = None  # None: the parent's per_child caps
    deny_shell: list[str] = field(default_factory=list)  # added to the parent's
    deny_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChildResult:
    output: str | None
    stopped: Literal["budget", "turns", "time", "stuck", "refused"] | None
    cost_usd: Decimal
    reason: str = ""


class ChildSpawner:
    def __init__(
        self,
        parent: ResolvedAgent,
        parent_budget: BudgetSlice,
        runner_factory: RunnerFactory,
        depth: int = 0,
    ) -> None:
        caps = parent.children
        self.parent, self.parent_budget, self.runner_factory = parent, parent_budget, runner_factory
        self.depth = depth
        self.max_total, self.max_depth = caps.max_total or 0, caps.max_depth or 0
        self.per_child = caps.per_child
        self._spawned = 0
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(caps.max_concurrent or 1)

    async def spawn(self, spec: ChildSpec) -> ChildResult:
        if self.depth + 1 > self.max_depth:
            return _refused(
                f"max_depth {self.max_depth} reached (this agent is at depth {self.depth})"
            )
        tools = list(spec.tools) if spec.tools is not None else list(self.parent.tools)
        if wider := sorted(set(tools) - set(self.parent.tools)):
            return _refused(f"a child cannot have tools its parent lacks: {wider}")
        async with self._lock:
            if self._spawned >= self.max_total:
                return _refused(f"max_total {self.max_total} children already spawned")
            self._spawned += 1
        limits = _narrow(spec.limits, self.per_child)
        try:
            budget = self.parent_budget.open(spec.name, "child", _usd(limits.budget_usd))
        except LimitHit:
            async with self._lock:
                self._spawned -= 1  # nothing ran: the spawn does not count
            return _refused("the parent has no budget left for a child")
        child = self._child(spec, tools, limits)
        async with self._slots:
            runner = self.runner_factory(child, budget, self.depth + 1)
            try:
                outcome = await runner.run(
                    AgentTask(instructions=spec.instructions, inputs={}, prompt=spec.prompt)
                )
            finally:
                cost = budget.total_spent()
                try:
                    budget.close()
                except BudgetError:
                    pass  # calls still in flight after a failure: the slice stays open
        return ChildResult(output=outcome.output, stopped=outcome.stopped, cost_usd=cost)

    def tools(self) -> list[Tool]:
        """`spawn_agent`, if the parent may spawn at all."""
        if "spawn_agent" not in self.parent.tools or self.max_total <= 0:
            return []

        async def spawn_agent(
            name: str, instructions: str, prompt: str, tools: list[str] | None = None
        ) -> str:
            """Start a child agent for a sub-task; returns its result when it ends."""
            result = await self.spawn(
                ChildSpec(name=name, instructions=instructions, prompt=prompt, tools=tools)
            )
            status = "completed" if result.stopped is None else f"stopped: {result.stopped}"
            detail = f" ({result.reason})" if result.reason else ""
            return f"child {name} {status}{detail}\n{result.output or ''}".rstrip()

        return [Tool(spawn_agent)]

    def _child(self, spec: ChildSpec, tools: list[str], limits: Limits) -> ResolvedAgent:
        definition = self.parent.definition.model_copy(
            update={"name": spec.name, "instructions": spec.instructions}
        )
        return self.parent.model_copy(
            update={
                "definition": definition,
                "tools": tools,
                "limits": limits,
                "deny_shell": _union(self.parent.deny_shell, spec.deny_shell),
                "deny_paths": _union(self.parent.deny_paths, spec.deny_paths),
            }
        )


def _narrow(requested: Limits | None, cap: Limits) -> Limits:
    """Field by field, the lower of the request and the cap (None = not set)."""
    values = {}
    for name in ("budget_usd", "max_turns", "max_minutes"):
        given = [v for v in (getattr(requested, name, None), getattr(cap, name)) if v is not None]
        values[name] = min(given) if given else None
    return Limits(**values)


def _usd(amount: float | None) -> Decimal | None:
    """A policy amount (a float in the definitions) as Decimal, through its text form."""
    return None if amount is None else Decimal(str(amount))


def _union(first: list[str], second: list[str]) -> list[str]:
    return list(dict.fromkeys([*first, *second]))


def _refused(reason: str) -> ChildResult:
    return ChildResult(output=None, stopped="refused", cost_usd=Decimal(0), reason=reason)
