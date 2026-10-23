"""Running one resolved agent on one task (M1-15).

`AgentRunner` is the interface the engine drives; `PydanticAIRunner` runs it on
Pydantic AI, stepping through the agent's graph node by node (`agent.iter`) so
it can keep the latest text and check for a stuck loop after every response.

What a run sees (spec §8.3): the agent's instructions, a notice that wrapped
text is data (M1-42), the repository's AGENTS.md wrapped as data (M1-46), each
consumed handoff wrapped as data (`<handoff kind="...">`, escaped so it cannot
close early), and the step's prompt - never an earlier agent's conversation.

What the model wrote itself (its text, thinking, tool-call arguments, and the
retry prompts that echo them) is secret-masked in the history before each call,
whatever the policy's action: resending it leaks nothing from this machine, and
under `block` one key the model made up would otherwise end the run (review F2).
Everything else in the request is still scanned, and blocked, by the transport.

Limits (spec §8.4), each ending the run with a reason:
  * turns  - `UsageLimits(request_limit=max_turns)`             -> stopped: turns
  * time   - `max_minutes`, measured around the whole run        -> stopped: time
             (the text produced so far is kept as the partial output)
  * budget - `LimitHit` from the metering transport: a child stops (stopped:
             budget, the parent decides); a step or run limit is re-raised for
             the engine, which pauses the run for a human
  * stuck  - the stuck check after each model response           -> stopped: stuck
             (on by default: OpenHands' detector; `detail` names the pattern)

Output contract (M1-20): with a `contract`, the agent is shown the document it
must write, and each answer is checked. Errors go back to the model once
(Pydantic AI's output retry); a second invalid answer ends the run `invalid`,
keeping the last answer and its errors for a human gate. A pause for approval
is never checked.

A pause saves the run's state to `state-<agent>.json` in `state_dir`. The state
is bound to its run and policy version and used once; its digest is in the
outcome so the engine can ledger it, and `resume` needs that ledgered digest -
a state rewritten with a fresh fingerprint is refused (review I3).

Kill switch (M2-19, M2-10): with a `turn_guard`, the runner asks it before each
model request - the boundary between turns. A kill raises `RunKilled` at once. A
pause (or switch state that cannot be confirmed) lets the turn in progress finish,
saves the conversation as above with no pending calls, and returns `paused` with
`held` set; `resume` with no decisions sends the saved request, so nothing is lost
or repeated. A child agent ignores a pause - its parent holds at its own next turn
- but obeys a kill.
"""

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import TypeAdapter
from pydantic_ai import (
    Agent,
    DeferredToolRequests,
    DeferredToolResults,
    ModelRetry,
    Tool,
    ToolDenied,
)
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.instrumented import instrument_model
from pydantic_ai.usage import RunUsage, UsageLimits

from agentic.agents.metering import CostLog
from agentic.agents.models import model_settings
from agentic.agents.stuck import StuckDetector
from agentic.agents.stuck import stuck_check as detector_check
from agentic.killswitch import RunKilled, Verdict
from agentic.policy import ResolvedAgent
from agentic.security import SecretBlocked, SecretScanner
from agentic.security.untrusted import DATA_NOTICE, HANDOFF_TAG, fence
from agentic.telemetry.spans import model_instrumentation
from agentic.transport import BudgetSlice, LimitHit

Stopped = Literal["budget", "turns", "time", "stuck"]
StuckCheck = Callable[[list[ModelMessage]], str | None]
#: Asked before each model request: the kill switch's verdict for the step (M2-19).
TurnGuard = Callable[[], Awaitable[Verdict]]
_USAGE = TypeAdapter(RunUsage)
STUCK_WINDOW = 40  # messages the stuck check looks at (it scans at most 20 events)
MODEL_OUTPUT = "model-output"  # the `where` of a scan event for what the model wrote
OUTPUT_RETRIES = 1  # an invalid answer goes back once with its errors, then the run is invalid
RETRY_MESSAGE = """\
Your handoff document is not valid:
{bullets}
Answer again with the whole document, corrected: a line '---', the YAML fields, \
a line '---', then the Markdown body."""


class OutputContract(Protocol):
    """What the agent must hand over (agentic.handoffs.HandoffContract)."""

    @property
    def instructions(self) -> str: ...

    @property
    def digest(self) -> str: ...  # recorded in a paused state; resume needs the same

    def check(self, text: str) -> tuple[str | None, list[str]]: ...


def retry_message(errors: list[str]) -> str:
    return RETRY_MESSAGE.format(bullets="\n".join(f"- {error}" for error in errors))


class ResumeError(Exception):
    """A paused run that must not be resumed: edited, another agent's, or wrong decisions."""


@dataclass(frozen=True)
class AgentTask:
    instructions: str  # the agent definition's body
    inputs: dict[str, str]  # consumed handoffs, by kind
    prompt: str  # what this step asks for
    knowledge: str = ""  # the repository's AGENTS.md, wrapped as data (M1-46)


@dataclass
class AgentOutcome:
    status: Literal["completed", "stopped", "paused", "invalid"]
    output: str | None
    stopped: Stopped | None
    usage: RunUsage
    costs: CostLog
    pending: list[str] = field(default_factory=list)  # tool calls awaiting a human
    state_path: Path | None = None  # where a paused run was saved
    state_digest: str | None = None  # the saved state's digest: the engine ledgers it
    detail: str | None = None  # why it stopped, in words (the stuck pattern, the limit)
    errors: list[str] = field(default_factory=list)  # why an invalid answer was refused
    held: Verdict | None = None  # a kill-switch pause held the run at a turn (state saved)


@runtime_checkable
class AgentRunner(Protocol):
    async def run(self, task: AgentTask) -> AgentOutcome: ...

    async def resume(
        self, state_path: Path, decisions: dict[str, bool | str], expected_digest: str
    ) -> AgentOutcome: ...


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
        stuck_check: StuckCheck | None = None,  # None: OpenHands' detector, default thresholds
        time_limit_s: float | None = None,
        run_id: str | None = None,
        policy_version: str | None = None,
        contract: OutputContract | None = None,
        scanner: SecretScanner | None = None,  # masks what the model wrote; None = default
        turn_guard: TurnGuard | None = None,  # the kill switch, before each request (M2-19)
    ) -> None:
        self.agent, self.model, self.budget, self.state_dir = agent, model, budget, state_dir
        self.turn_guard = turn_guard
        self.contract = contract
        self.scanner = scanner or SecretScanner()
        self._invalid: tuple[str, list[str]] | None = None  # the last refused answer
        self._refused = 0  # answers refused in this run: past OUTPUT_RETRIES it is invalid
        self.tools, self.is_child = list(tools), is_child
        self.stuck_check = stuck_check or detector_check(StuckDetector())
        self.run_id, self.policy_version = run_id, policy_version
        self.costs = costs if costs is not None else CostLog()
        minutes = agent.limits.max_minutes
        self.time_limit_s = (
            time_limit_s if time_limit_s is not None else (minutes * 60 if minutes else None)
        )

    async def run(self, task: AgentTask) -> AgentOutcome:
        instructions = _instructions(task, self.contract)
        return await self._drive(self._pydantic_agent(instructions), task.prompt, instructions)

    async def resume(
        self, state_path: Path, decisions: dict[str, bool | str], expected_digest: str
    ) -> AgentOutcome:
        """Continue a paused run (perhaps in another process). Everything is checked
        before any model call: the state is in this runner's state folder, matches the
        digest the engine ledgered at the pause, belongs to this run, policy version,
        agent and version, still has every pending tool, and there is exactly one
        decision - True to approve, or a reason to deny - for each pending call. The
        state is then used up, so an approval can never run twice."""
        state_path = Path(state_path)
        try:
            state_path.resolve().relative_to(self.state_dir.resolve())
        except ValueError:
            raise ResumeError(
                f"paused run state is not in this run's state folder: {state_path}"
            ) from None
        state = _load_state(state_path)
        if state["fingerprint"] != expected_digest:
            raise ResumeError(
                f"paused run state does not match the digest in the ledger - it was replaced "
                f"after the pause: {state_path}"
            )
        bound = (state["run_id"], state["policy_version"])
        if bound != (self.run_id, self.policy_version):
            raise ResumeError(
                f"this paused run belongs to run {bound[0]} (policy {bound[1]}), "
                f"not run {self.run_id} (policy {self.policy_version})"
            )
        name, version = self.agent.definition.name, self.agent.definition.version
        if (state["agent"], state["agent_version"]) != (name, version):
            raise ResumeError(
                f"this paused run belongs to agent {state['agent']} version "
                f"{state['agent_version']}, not {name} version {version}"
            )
        expected = self.contract.digest if self.contract is not None else None
        if state.get("contract") != expected:
            raise ResumeError(
                f"this paused run was checked against handoff contract {state.get('contract')}; "
                f"resume it with the same contract, not {expected}"
            )
        have = {tool.name for tool in self.tools}
        if missing := sorted(set(state["pending_tools"].values()) - have):
            raise ResumeError(f"the paused calls need tools this agent no longer has: {missing}")
        pending = set(state["pending"])
        if set(decisions) != pending:
            raise ResumeError(f"decide every pending call and only those: {sorted(pending)}")
        approvals = {}
        for call_id, decision in decisions.items():
            if decision is True:
                approvals[call_id] = True
            elif isinstance(decision, str) and decision.strip():
                approvals[call_id] = ToolDenied(decision.strip())
            else:
                raise ResumeError(
                    f"a decision is True (approve) or a reason to deny, not {decision!r}"
                )
        history = ModelMessagesTypeAdapter.validate_python(state["messages"])
        instructions = state["instructions"]
        _use_up(state_path)
        # a held turn (no pending calls) resumes by sending the request it saved
        deferred = DeferredToolResults(approvals=approvals) if pending else None
        return await self._drive(
            self._pydantic_agent(instructions),
            None,
            instructions,
            message_history=history,
            deferred_tool_results=deferred,
            usage=_USAGE.validate_python(state["usage"]),
        )

    # ── driving one run ───────────────────────────────────────────────────────

    async def _drive(
        self, agent: Agent, prompt: str | None, instructions: str, **run_kwargs
    ) -> AgentOutcome:
        seen: dict = {"text": None, "run": None, "stuck": None, "hold": None}
        self._invalid, self._refused = None, 0
        earlier_hit = self.costs.limit_hit  # a hit from an earlier run was already acted on

        async def go():
            async with agent.iter(
                prompt,
                usage_limits=self._usage_limits(),
                model_settings=model_settings(self.agent),
                **run_kwargs,
            ) as agent_run:
                seen["run"] = agent_run
                async for node in agent_run:
                    if self.turn_guard is not None and Agent.is_model_request_node(node):
                        verdict = await self.turn_guard()  # between turns (M2-19)
                        if verdict.action == "kill":
                            raise RunKilled(verdict)
                        if verdict.action == "pause" and not self.is_child:
                            # the request is not in the history until it is sent
                            held = [*agent_run.all_messages(), node.request]
                            seen["hold"] = (verdict, held)
                            return None
                    if Agent.is_call_tools_node(node):
                        text = "".join(
                            p.content for p in node.model_response.parts if isinstance(p, TextPart)
                        )
                        seen["text"] = text or seen["text"]
                        recent = agent_run.all_messages()[-STUCK_WINDOW:]
                        if stuck := self.stuck_check(recent):
                            seen["stuck"] = stuck
                            return None
                return agent_run.result

        try:
            if self.time_limit_s is None:
                result = await go()
            else:
                result = await asyncio.wait_for(go(), self.time_limit_s)
        except TimeoutError:
            return self._stopped("time", seen)
        except UsageLimitExceeded:  # after a refused answer: its errors say why (DF-37)
            return self._stopped("turns", seen, refused=self._invalid)
        except UnexpectedModelBehavior:
            # invalid only when the answers used up their retries: anything else (a
            # tool's retry limit, an empty response) is the step's real failure (DF-36)
            if self._invalid is None or self._refused <= OUTPUT_RETRIES:
                raise
            text, errors = self._invalid
            return AgentOutcome(
                status="invalid",
                output=text,
                stopped=None,
                usage=_usage(seen),
                costs=self.costs,
                errors=errors,
            )
        except BaseException as error:
            hit = _limit_hit(error)
            if hit is None:
                if blocked := _found(error, SecretBlocked):
                    raise blocked from error  # the SDK wrapped it as a connection error
                raise
            return self._budget(hit, seen)
        if self.costs.limit_hit is not None and self.costs.limit_hit is not earlier_hit:
            return self._budget(self.costs.limit_hit, seen)
        if seen["hold"] is not None:
            return self._hold(*seen["hold"], instructions, seen)
        if result is None:
            return self._stopped("stuck", seen, detail=seen["stuck"])
        if isinstance(result.output, DeferredToolRequests):
            return self._pause(result, instructions, seen)
        return AgentOutcome(
            status="completed",
            output=result.output,
            stopped=None,
            usage=_usage(seen),
            costs=self.costs,
        )

    def _pydantic_agent(self, instructions: str) -> Agent:
        agent = Agent(
            instrument_model(self.model, model_instrumentation()),  # spans, no content (M1-43)
            name=self.agent.definition.name,
            instructions=instructions,
            tools=self.tools,
            output_type=[str, DeferredToolRequests],
            retries={"tools": 1, "output": OUTPUT_RETRIES},
            capabilities=[ProcessHistory(scrubbed_history(self.scanner))],
        )
        contract = self.contract
        if contract is not None:

            @agent.output_validator
            def validate(output: str | DeferredToolRequests) -> str | DeferredToolRequests:
                if not isinstance(output, str):
                    return output  # a pause for approval: nothing to check yet
                document, errors = contract.check(output)
                if errors:
                    self._invalid = (output, errors)
                    self._refused += 1
                    raise ModelRetry(retry_message(errors))
                return document

        return agent

    def _pause(self, result, instructions: str, seen: dict) -> AgentOutcome:
        """Save what a later process needs to continue; the history is sensitive and
        stays in the git-ignored run folder, readable by its owner only (the engine
        ledgers only its digest)."""
        calls = result.output.approvals
        pending = [call.tool_call_id for call in calls]
        tools = {call.tool_call_id: call.tool_name for call in calls}
        path, digest = self._save(result.all_messages(), pending, tools, instructions, seen)
        return AgentOutcome(
            status="paused",
            output=seen["text"],
            stopped=None,
            usage=_usage(seen),
            costs=self.costs,
            pending=pending,
            state_path=path,
            state_digest=digest,
        )

    def _hold(
        self, verdict: Verdict, messages: list, instructions: str, seen: dict
    ) -> AgentOutcome:
        """A kill-switch pause between turns: the conversation is saved like a tool-approval
        pause with nothing pending, so `resume` sends the request it was about to send."""
        path, digest = self._save(messages, [], {}, instructions, seen)
        return AgentOutcome(
            status="paused",
            output=seen["text"],
            stopped=None,
            usage=_usage(seen),
            costs=self.costs,
            state_path=path,
            state_digest=digest,
            detail=verdict.describe(),
            held=verdict,
        )

    def _save(
        self,
        messages: list,
        pending: list[str],
        tools: dict[str, str],
        instructions: str,
        seen: dict,
    ) -> tuple[Path, str]:
        payload = {
            "run_id": self.run_id,
            "policy_version": self.policy_version,
            "agent": self.agent.definition.name,
            "agent_version": self.agent.definition.version,
            "instructions": instructions,
            "messages": json.loads(ModelMessagesTypeAdapter.dump_json(messages)),
            "pending": pending,
            "pending_tools": tools,
            "contract": self.contract.digest if self.contract is not None else None,
            "usage": _USAGE.dump_python(_usage(seen), mode="json"),
        }
        digest = _fingerprint(payload)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / f"state-{self.agent.definition.name}.json"
        text = json.dumps({**payload, "fingerprint": digest}, indent=2, ensure_ascii=False)
        path.write_text(text + "\n", encoding="utf-8", newline="\n")
        if os.name != "nt":  # Windows has no owner-only mode bits; the run folder's ACL applies
            path.chmod(0o600)
        return path, digest

    def _usage_limits(self) -> UsageLimits | None:
        turns = self.agent.limits.max_turns
        return UsageLimits(request_limit=turns) if turns else None

    def _stopped(
        self,
        reason: Stopped,
        seen: dict,
        detail: str | None = None,
        refused: tuple[str, list[str]] | None = None,
    ) -> AgentOutcome:
        """With `refused` (the last answer the contract refused), its errors go with the
        outcome, so a gate can say why no handoff came out."""
        return AgentOutcome(
            status="stopped",
            output=seen["text"],
            stopped=reason,
            usage=_usage(seen),
            costs=self.costs,
            detail=detail,
            errors=list(refused[1]) if refused else [],
        )

    def _budget(self, hit: LimitHit, seen: dict) -> AgentOutcome:
        if self.is_child and hit.scope == "child":
            return self._stopped("budget", seen, detail=str(hit))
        raise hit  # a step or run limit: the engine pauses the run for a human


def scrubbed_history(
    scanner: SecretScanner,
) -> Callable[[list[ModelMessage]], list[ModelMessage]]:
    """A history processor masking secrets in what the model wrote (see the module).
    A message found clean is remembered (and kept alive, so its id stays its own),
    so each is scanned once per run, not on every call."""
    done: dict[int, ModelMessage] = {}

    def process(messages: list[ModelMessage]) -> list[ModelMessage]:
        result = []
        for message in messages:
            if id(message) not in done:
                message = _scrubbed(message, scanner)
                done[id(message)] = message
            result.append(message)
        return result

    return process


def _scrubbed(message: ModelMessage, scanner: SecretScanner) -> ModelMessage:
    parts = [_scrubbed_part(part, scanner) for part in message.parts]
    if all(new is old for new, old in zip(parts, message.parts, strict=True)):
        return message
    return replace(message, parts=parts)


def _scrubbed_part(part, scanner: SecretScanner):
    if isinstance(part, TextPart | ThinkingPart):
        content = scanner.scrub(part.content, MODEL_OUTPUT)
        return part if content is part.content else replace(part, content=content)
    if isinstance(part, ToolCallPart) and part.args:
        args = part.args
        try:
            parsed = json.loads(args) if isinstance(args, str) else args
        except ValueError:
            parsed = args  # not JSON: masked as text
        masked = scanner.scrub(parsed, MODEL_OUTPUT)
        if masked is parsed:
            return part
        decoded = isinstance(args, str) and parsed is not args  # JSON text: keep it JSON text
        return replace(part, args=json.dumps(masked) if decoded else masked)
    if isinstance(part, RetryPromptPart):  # may echo the model's arguments back to it
        content = scanner.scrub(part.content, MODEL_OUTPUT)
        return part if content is part.content else replace(part, content=content)
    return part


def _fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_state(path: Path) -> dict:
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ResumeError(f"paused run state not readable: {path} ({error})") from error
    if not isinstance(state, dict) or "fingerprint" not in state:
        raise ResumeError(f"paused run state has no fingerprint: {path}")
    claimed = state.pop("fingerprint")
    if claimed != _fingerprint(state):
        raise ResumeError(
            f"paused run state does not match its fingerprint - it was edited: {path}"
        )
    return {**state, "fingerprint": claimed}


def _use_up(path: Path) -> None:
    """Rename a resumed state so it cannot be resumed again (kept for the record)."""
    used = path.with_name(f"{path.stem}.used-{time.time_ns()}{path.suffix}")
    try:
        path.rename(used)
    except OSError as error:
        raise ResumeError(f"paused run state could not be used up: {path} ({error})") from error


def _instructions(task: AgentTask, contract: OutputContract | None = None) -> str:
    """The agent's instructions, the data notice, the repository's AGENTS.md (wrapped
    as data), each handoff fenced as data (a `</handoff>` inside cannot close it -
    DF-24), then the output contract."""
    parts = [task.instructions.strip(), DATA_NOTICE]
    if task.knowledge.strip():
        parts.append(task.knowledge.strip())
    for kind, text in task.inputs.items():
        parts.append(fence(text.strip(), HANDOFF_TAG, kind=kind))
    if contract is not None:
        parts.append(contract.instructions)
    return "\n\n".join(parts)


def _usage(seen: dict) -> RunUsage:
    run = seen["run"]
    usage = getattr(run, "usage", None) if run is not None else None
    usage = usage() if callable(usage) else usage
    return usage if isinstance(usage, RunUsage) else RunUsage()


def _limit_hit(error: BaseException) -> LimitHit | None:
    """A LimitHit inside `error`: itself, a member of an exception group, or a cause."""
    return _found(error, LimitHit)


def _found[E: BaseException](error: BaseException, kind: type[E]) -> E | None:
    """An exception of `kind` inside `error`: itself, a group member, or a cause."""
    seen: set[int] = set()
    stack: list[BaseException | None] = [error]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, kind):
            return current
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        stack.extend([current.__cause__, current.__context__])
    return None
