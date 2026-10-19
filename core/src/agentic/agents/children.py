"""Child agents under admin limits (M1-17, spec §8.4).

A parent with the `spawn_agent` capability may start children for sub-tasks.
The limits come from the parent's resolved `children` (its agent type's caps,
lowered by its team): how many in total, how many at once, how deep. A child
can only ever narrow:

  * tools    - a subset of the parent's (asking for more is refused);
  * deny     - the parent's deny lists plus the child's own (never fewer);
  * limits   - the lower of what is asked and the parent's `per_child` caps;
  * budget   - a slice of the parent's ledger, closed when the child ends.

The count, concurrency and depth caps are the ROOT agent's and hold for the
whole tree: every spawner in it shares one counter and one set of slots, and the
runner factory is handed the child's own spawner (never a bare depth), so a
factory cannot reset the limits. Only the root waits for a free slot; a nested
spawn that finds none is refused - waiting there could deadlock, as its parent
holds a slot until the child ends.

A spawn that breaks a limit is not an exception: it returns `stopped:
"refused"` with the reason, and the parent decides what to do. A child that
hits its own limit returns its partial output marked `stopped:` (budget, turns,
time, stuck). A child cannot wait for a human: one that pauses for approval
returns `stopped: "paused"`. A step or run limit hit inside a child reaches the
engine.
"""

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from pydantic_ai import Tool

from agentic.agents.runner import AgentRunner, AgentTask
from agentic.definitions import Limits
from agentic.policy import ResolvedAgent
from agentic.transport import BudgetError, BudgetSlice, LimitHit

# (child, its budget slice, its own spawner - for its spawn_agent tool) -> runner
RunnerFactory = Callable[[ResolvedAgent, BudgetSlice, "ChildSpawner"], AgentRunner]
CHILD_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


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
    stopped: Literal["budget", "turns", "time", "stuck", "paused", "refused"] | None
    cost_usd: Decimal
    reason: str = ""


class _Tree:
    """What every spawner in one agent tree shares: the root's caps and counters."""

    def __init__(self, root: ResolvedAgent) -> None:
        caps = root.children
        self.max_total, self.max_depth = caps.max_total or 0, caps.max_depth or 0
        self.max_concurrent = caps.max_concurrent or 1
        self.spawned = 0
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(self.max_concurrent)


class ChildSpawner:
    def __init__(
        self,
        parent: ResolvedAgent,
        parent_budget: BudgetSlice,
        runner_factory: RunnerFactory,
        *,
        _tree: _Tree | None = None,
        _depth: int = 0,
    ) -> None:
        self.parent, self.parent_budget, self.runner_factory = parent, parent_budget, runner_factory
        self.tree = _tree or _Tree(parent)
        self.depth = _depth
        self.max_total, self.max_depth = self.tree.max_total, self.tree.max_depth
        self.per_child = parent.children.per_child

    def for_child(self, child: ResolvedAgent, budget: BudgetSlice) -> "ChildSpawner":
        """The spawner a child uses for its own children: same tree, one level deeper."""
        return ChildSpawner(
            child, budget, self.runner_factory, _tree=self.tree, _depth=self.depth + 1
        )

    async def spawn(self, spec: ChildSpec) -> ChildResult:
        if not CHILD_NAME.fullmatch(spec.name):
            return _refused(
                "a child's name is 1-40 lowercase letters, digits and dashes, "
                f"starting with a letter or digit: {spec.name[:60]!r}"
            )
        if self.depth + 1 > self.max_depth:
            return _refused(
                f"max_depth {self.max_depth} reached (this agent is at depth {self.depth})"
            )
        tools = list(spec.tools) if spec.tools is not None else list(self.parent.tools)
        if wider := sorted(set(tools) - set(self.parent.tools)):
            return _refused(f"a child cannot have tools its parent lacks: {wider}")
        tree = self.tree
        async with tree.lock:
            if tree.spawned >= tree.max_total:
                return _refused(f"max_total {tree.max_total} agents already spawned in this tree")
            tree.spawned += 1
        if self.depth > 0 and tree.slots.locked():  # nested: never wait (see above)
            await self._give_back()
            return _refused(f"max_concurrent {tree.max_concurrent} agents already running")
        async with tree.slots:
            return await self._run(spec, tools)

    async def _run(self, spec: ChildSpec, tools: list[str]) -> ChildResult:
        limits = _narrow(spec.limits, self.per_child)
        try:
            budget = self.parent_budget.open(spec.name, "child", _usd(limits.budget_usd))
        except LimitHit:
            await self._give_back()  # nothing ran: the spawn does not count
            return _refused("the parent has no budget left for a child")
        started = False
        try:
            child = self._child(spec, tools, limits)
            runner = self.runner_factory(child, budget, self.for_child(child, budget))
            started = True
            outcome = await runner.run(
                AgentTask(instructions=spec.instructions, inputs={}, prompt=spec.prompt)
            )
        finally:
            cost = budget.total_spent()
            try:
                budget.close()
            except BudgetError:
                pass  # calls still in flight after a failure: the slice stays open
            if not started:
                await self._give_back()  # the child was never built: the spawn does not count
        if outcome.status == "paused":
            return ChildResult(
                output=outcome.output,
                stopped="paused",
                cost_usd=cost,
                reason=f"the child asked for approval of {len(outcome.pending)} tool call(s); "
                "a child cannot wait for a human",
            )
        reason = outcome.detail or ""
        return ChildResult(
            output=outcome.output, stopped=outcome.stopped, cost_usd=cost, reason=reason
        )

    async def _give_back(self) -> None:
        async with self.tree.lock:
            self.tree.spawned -= 1

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
