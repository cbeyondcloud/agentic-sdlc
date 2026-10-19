"""Running one resolved agent on one task (M1-15).

`AgentRunner` is the interface the engine drives; `PydanticAIRunner` runs it on
Pydantic AI, stepping through the agent's graph node by node (`agent.iter`) so
it can keep the latest text and check for a stuck loop after every response.

What a run sees (spec §8.3): the agent's instructions, each consumed handoff
wrapped as data (`<handoff kind="...">`), and the step's prompt - never an
earlier agent's conversation.

Limits (spec §8.4), each ending the run with a reason:
  * turns  - `UsageLimits(request_limit=max_turns)`             -> stopped: turns
  * time   - `max_minutes`, measured around the whole run        -> stopped: time
             (the text produced so far is kept as the partial output)
  * budget - `LimitHit` from the metering transport: a child stops (stopped:
             budget, the parent decides); a step or run limit is re-raised for
             the engine, which pauses the run for a human
  * stuck  - the stuck check after each model response           -> stopped: stuck
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic_ai import Agent, Tool
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, TextPart
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits

from agentic.agents.metering import CostLog
from agentic.agents.models import model_settings
from agentic.policy import ResolvedAgent
from agentic.transport import BudgetSlice, LimitHit

Stopped = Literal["budget", "turns", "time", "stuck"]
StuckCheck = Callable[[list[ModelMessage]], str | None]


@dataclass(frozen=True)
class AgentTask:
    instructions: str  # the agent definition's body
    inputs: dict[str, str]  # consumed handoffs, by kind
    prompt: str  # what this step asks for


@dataclass
class AgentOutcome:
    status: Literal["completed", "stopped", "paused"]
    output: str | None
    stopped: Stopped | None
    usage: RunUsage
    costs: CostLog
    pending: list[str] = field(default_factory=list)  # tool calls awaiting a human
    state_path: Path | None = None  # where a paused run was saved


@runtime_checkable
class AgentRunner(Protocol):
    async def run(self, task: AgentTask) -> AgentOutcome: ...

    async def resume(self, state_path: Path, decisions: dict[str, bool | str]) -> AgentOutcome: ...


class PydanticAIRunner:
    def __init__(
        self,
        agent: ResolvedAgent,
        model: Model,
        *,
        budget: BudgetSlice,
        state_dir: Path,
        tools: Sequence[Tool] = (),
        costs: CostLog | None = None,
        is_child: bool = False,
        stuck_check: StuckCheck | None = None,
        time_limit_s: float | None = None,
    ) -> None:
        self.agent, self.model, self.budget, self.state_dir = agent, model, budget, state_dir
        self.tools, self.is_child, self.stuck_check = list(tools), is_child, stuck_check
        self.costs = costs if costs is not None else CostLog()
        minutes = agent.limits.max_minutes
        self.time_limit_s = (
            time_limit_s if time_limit_s is not None else (minutes * 60 if minutes else None)
        )

    async def run(self, task: AgentTask) -> AgentOutcome:
        return await self._drive(self._pydantic_agent(task), task.prompt)

    async def resume(self, state_path: Path, decisions: dict[str, bool | str]) -> AgentOutcome:
        raise NotImplementedError("pause and resume arrive with deferred tools (M1-15 part 4)")

    # ── driving one run ───────────────────────────────────────────────────────

    async def _drive(self, agent: Agent, prompt: str | None, **run_kwargs) -> AgentOutcome:
        seen: dict = {"text": None, "run": None}

        async def go():
            async with agent.iter(
                prompt,
                usage_limits=self._usage_limits(),
                model_settings=model_settings(self.agent),
                **run_kwargs,
            ) as agent_run:
                seen["run"] = agent_run
                async for node in agent_run:
                    if Agent.is_call_tools_node(node):
                        text = "".join(
                            p.content for p in node.model_response.parts if isinstance(p, TextPart)
                        )
                        seen["text"] = text or seen["text"]
                        if self.stuck_check and self.stuck_check(agent_run.all_messages()):
                            return None
                return agent_run.result

        try:
            if self.time_limit_s is None:
                result = await go()
            else:
                result = await asyncio.wait_for(go(), self.time_limit_s)
        except TimeoutError:
            return self._stopped("time", seen)
        except UsageLimitExceeded:
            return self._stopped("turns", seen)
        except BaseException as error:
            hit = _limit_hit(error)
            if hit is None:
                raise
            return self._budget(hit, seen)
        if self.costs.limit_hit is not None:
            return self._budget(self.costs.limit_hit, seen)
        if result is None:
            return self._stopped("stuck", seen)
        return AgentOutcome(
            status="completed",
            output=result.output,
            stopped=None,
            usage=_usage(seen),
            costs=self.costs,
        )

    def _pydantic_agent(self, task: AgentTask) -> Agent:
        return Agent(
            self.model, instructions=_instructions(task), tools=self.tools, output_type=str
        )

    def _usage_limits(self) -> UsageLimits | None:
        turns = self.agent.limits.max_turns
        return UsageLimits(request_limit=turns) if turns else None

    def _stopped(self, reason: Stopped, seen: dict) -> AgentOutcome:
        return AgentOutcome(
            status="stopped",
            output=seen["text"],
            stopped=reason,
            usage=_usage(seen),
            costs=self.costs,
        )

    def _budget(self, hit: LimitHit, seen: dict) -> AgentOutcome:
        if self.is_child and hit.scope == "child":
            return self._stopped("budget", seen)
        raise hit  # a step or run limit: the engine pauses the run for a human


def _instructions(task: AgentTask) -> str:
    parts = [task.instructions.strip()]
    for kind, text in task.inputs.items():
        parts.append(f'<handoff kind="{kind}">\n{text.strip()}\n</handoff>')
    return "\n\n".join(parts)


def _usage(seen: dict) -> RunUsage:
    run = seen["run"]
    usage = getattr(run, "usage", None) if run is not None else None
    usage = usage() if callable(usage) else usage
    return usage if isinstance(usage, RunUsage) else RunUsage()


def _limit_hit(error: BaseException) -> LimitHit | None:
    """A LimitHit inside `error`: itself, a member of an exception group, or a cause."""
    seen: set[int] = set()
    stack = [error]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, LimitHit):
            return current
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        stack.extend([current.__cause__, current.__context__])
    return None
