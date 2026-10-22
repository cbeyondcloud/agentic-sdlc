"""The shared ACP backend: any agent CLI that speaks the Agent Client Protocol (v1), driven
over stdio through the `agent-client-protocol` SDK (M2-57; spike S7). Optional: needs the
`acp` extra of agentic-core, imported only when a run starts.

Nothing here names an agent: what is agent-specific - command, routing, policy compile,
egress switches, auth, mode - is the step's `AcpProfile`. For each step:

  * the agent runs in the step's tree, with a home of its own (no user config, history
    or credentials), on the ACP SDK's minimal environment plus what the profile sets;
  * under `wrap` (srt, D38) when given: the sandbox, never the agent, is the boundary;
  * the client offers the agent no file or terminal access of ours - it uses its own
    tools, under its own compiled policy, inside the sandbox;
  * every permission request the agent sends is refused (a second gate: what the
    agent's policy did not already allow is not allowed - S7 decision 3);
  * the result is the agent's final message (its text after the turn's last tool call),
    checked against the step's contract with one retry in the same session;
  * each turn is charged at policy's prices (see `_charge`); past the slice, `LimitHit`.
"""

import asyncio
import math
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai.usage import RunUsage

from agentic.agents.metering import CostLog
from agentic.agents.runner import (
    AgentOutcome,
    AgentTask,
    OutputContract,
    ResumeError,
    _instructions,
    retry_message,
)
from agentic.agents.tools import ToolContext
from agentic.backends.acp.profile import (
    AcpProfile,
    ShellRules,
    StepSetup,
    launch_env,
    write_files,
)
from agentic.backends.processes import kill_tree, session_leader
from agentic.policy import ResolvedAgent
from agentic.transport import BudgetSlice, CallCost, CallIdentity, LimitHit, Pricer
from agentic.transport.cost import estimate_usd

if TYPE_CHECKING:
    from agentic.agents.children import ChildSpawner
    from agentic.engine.engine import StepContext

#: The only ACP version we negotiate: v1 is stable; v2 is a draft (S7).
PROTOCOL_VERSION = 1
#: One JSON-RPC line may carry a large tool result or diff.
STDIO_LIMIT = 16 * 1024 * 1024
#: A conservative tokens-from-text estimate, for an agent that reports no usage at all.
CHARS_PER_TOKEN = 4
_STDERR = "agent-stderr.log"
_SHIMS = (".cmd", ".bat")


@dataclass(frozen=True)
class AcpSettings:
    gateway_url: str  # TokenLean's root
    api_key: str = field(repr=False)  # the user's TokenLean key
    home_root: Path  # per-step agent homes live here
    command: tuple[str, ...] | None = None  # the agent's command; None: the profile's


def resolve_command(command: Sequence[str], which=shutil.which) -> list[str]:
    """The agent command with its program found on PATH. A Windows script shim (npm's
    `.cmd`) is refused: it would run through cmd.exe, which re-parses its arguments."""
    found = which(command[0])
    if found is None:
        raise RuntimeError(
            f"the agent command {command[0]!r} was not found on PATH: install the pinned "
            "release, or configure the full path to its executable"
        )
    if Path(found).suffix.lower() in _SHIMS:
        raise RuntimeError(
            f"{found} is a script shim, not an executable: configure the path to the "
            "agent's own (signed) executable instead"
        )
    return [found, *command[1:]]


@dataclass
class _Turn:
    text: str = ""  # the agent's text since its last tool call
    tool_calls: int = 0
    session_cost: float | None = None  # the cumulative session cost it last reported (USD)


class _Client:
    """Our side of the protocol: collects the turn, refuses every permission request,
    and offers no file system or terminal."""

    def __init__(self) -> None:
        self.turn = _Turn()
        self.refused: list[str] = []

    def begin(self) -> None:
        self.turn = _Turn()

    async def session_update(self, session_id: str, update, **kwargs) -> None:
        kind = getattr(update, "session_update", None)
        if kind == "agent_message_chunk":
            text = getattr(update.content, "text", None)
            if isinstance(text, str):
                self.turn.text += text
        elif kind == "tool_call":  # what the agent said before a tool call is not its answer
            self.turn.tool_calls += 1
            self.turn.text = ""
        elif kind == "usage_update":
            cost = update.cost
            if cost is not None and cost.currency.upper() == "USD":
                self.turn.session_cost = cost.amount

    async def request_permission(self, session_id: str, tool_call, options, **kwargs):
        from acp import schema

        self.refused.append(_described(tool_call))
        reject = next((o for o in options if o.kind == "reject_once"), None) or next(
            (o for o in options if o.kind == "reject_always"), None
        )
        if reject is None:  # never an allow option: cancel instead
            outcome = schema.DeniedOutcome(outcome="cancelled")
        else:
            outcome = schema.AllowedOutcome(outcome="selected", option_id=reject.option_id)
        return schema.RequestPermissionResponse(outcome=outcome)

    async def read_text_file(self, *args, **kwargs):
        raise _not_offered("fs/read_text_file")

    async def write_text_file(self, *args, **kwargs):
        raise _not_offered("fs/write_text_file")

    async def create_terminal(self, *args, **kwargs):
        raise _not_offered("terminal/create")

    async def terminal_output(self, *args, **kwargs):
        raise _not_offered("terminal/output")

    async def release_terminal(self, *args, **kwargs):
        raise _not_offered("terminal/release")

    async def wait_for_terminal_exit(self, *args, **kwargs):
        raise _not_offered("terminal/wait_for_exit")

    async def kill_terminal(self, *args, **kwargs):
        raise _not_offered("terminal/kill")

    async def ext_method(self, method: str, params: dict) -> dict:
        raise _not_offered(method)

    async def ext_notification(self, method: str, params: dict) -> None:
        return None


def _not_offered(method: str) -> Exception:
    import acp

    return acp.RequestError.method_not_found(method)


def _described(tool_call) -> str:
    title = getattr(tool_call, "title", None) or getattr(tool_call, "tool_call_id", "a tool")
    raw = getattr(tool_call, "raw_input", None)
    command = raw.get("command") if isinstance(raw, dict) else None
    return f"{title}: {command}" if isinstance(command, str) else str(title)


class AcpRunner:
    """Runs one agent step in one ACP session (an `AgentRunner`). It never pauses for a
    person: every permission request is refused."""

    def __init__(
        self,
        agent: ResolvedAgent,
        profile: AcpProfile,
        *,
        command: Sequence[str],
        env: dict[str, str],
        home: Path,
        cwd: Path,
        budget: BudgetSlice,
        price_for: Pricer,
        contract: OutputContract | None,
        files: dict[str, str] | None = None,
        time_limit_s: float | None = None,
        wrap: Sequence[str] | None = None,
        fallback_estimate_usd: Decimal = Decimal("0.01"),
    ) -> None:
        self.agent, self.profile = agent, profile
        self.command, self.env, self.home, self.cwd = list(command), dict(env), home, cwd
        self.files = dict(files or {})
        self.budget, self.price_for, self.contract = budget, price_for, contract
        minutes = agent.limits.max_minutes
        self.time_limit_s = (
            time_limit_s if time_limit_s is not None else (minutes * 60 if minutes else None)
        )
        self.wrap = list(wrap) if wrap else None
        self.fallback_estimate_usd = fallback_estimate_usd
        self.costs = CostLog()
        self.usage = RunUsage()
        self._client = _Client()
        self._process = None
        self._last_text = ""
        self._cost_seen = Decimal(0)

    async def run(self, task: AgentTask) -> AgentOutcome:
        session = asyncio.ensure_future(self._session(task))
        try:
            done, _ = await asyncio.wait({session}, timeout=self.time_limit_s)
        except asyncio.CancelledError:  # the run was stopped: so is everything it started
            self._kill()
            session.cancel()
            raise
        if not done:  # the time limit: kill the agent and what it started, then close
            self._kill()
            session.cancel()
            try:
                await session
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - it is being stopped
                pass
            detail = f"the step's time limit ({self.time_limit_s}s) was reached"
            return self._outcome("stopped", self._last_text, stopped="time", detail=detail)
        try:
            return session.result()
        except _Stopped as stop:
            return self._outcome("stopped", self._last_text, stopped=stop.why, detail=stop.detail)

    async def resume(self, state_path, decisions, expected_digest) -> AgentOutcome:
        raise ResumeError(f"{self.profile.name} steps never pause for approval")

    def _kill(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            kill_tree(process.pid)

    def _argv(self) -> list[str]:
        if self.wrap and os.name == "nt":
            raise RuntimeError(
                "wrapping an agent CLI in a sandbox needs macOS, Linux or WSL2 (D38)"
            )
        return session_leader([*(self.wrap or []), *resolve_command(self.command)])

    async def _session(self, task: AgentTask) -> AgentOutcome:
        import acp
        from acp import schema

        argv = self._argv()
        shutil.rmtree(self.home, ignore_errors=True)  # a retried step starts clean
        self.home.mkdir(parents=True, exist_ok=True)
        write_files(self.home, self.files)
        with (self.home / _STDERR).open("wb") as stderr:
            async with acp.spawn_agent_process(
                self._client,
                argv[0],
                *argv[1:],
                env=self.env,
                cwd=str(self.cwd),
                transport_kwargs={"stderr": stderr, "limit": STDIO_LIMIT},
            ) as (conn, process):
                self._process = process
                capabilities = schema.ClientCapabilities(
                    fs=schema.FileSystemCapabilities(read_text_file=False, write_text_file=False),
                    terminal=False,
                )
                init = await conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=capabilities,
                    client_info=schema.Implementation(name="agentic", version="1"),
                )
                if init.protocol_version != PROTOCOL_VERSION:
                    raise RuntimeError(
                        f"the {self.profile.name} agent speaks ACP protocol "
                        f"{init.protocol_version}; only {PROTOCOL_VERSION} is supported"
                    )
                if self.profile.auth_method:
                    await conn.authenticate(method_id=self.profile.auth_method)
                session = await conn.new_session(cwd=str(self.cwd), mcp_servers=[])
                sid = session.session_id
                if self.profile.mode:
                    await conn.set_session_mode(session_id=sid, mode_id=self.profile.mode)
                first = [
                    acp.text_block(_instructions(task, self.contract)),
                    acp.text_block(task.prompt),
                ]
                text = await self._turn(conn, sid, first)
                if self.contract is None:
                    return self._outcome("completed", text)
                accepted, errors = self.contract.check(text)
                if errors:  # one retry, in the same session (OUTPUT_RETRIES = 1)
                    text = await self._turn(conn, sid, [acp.text_block(retry_message(errors))])
                    accepted, errors = self.contract.check(text)
                if errors:
                    return self._outcome("invalid", text, errors=errors)
                return self._outcome("completed", accepted if accepted is not None else text)

    async def _turn(self, conn, session_id: str, blocks: list) -> str:
        self._client.begin()
        response = await conn.prompt(session_id=session_id, prompt=blocks)
        turn = self._client.turn
        text = turn.text.strip()
        if text:
            self._last_text = text
        sent = sum(len(getattr(block, "text", "") or "") for block in blocks)
        self._charge(response.usage, turn, sent_chars=sent, text=text)
        stop = response.stop_reason
        if stop == "max_turn_requests":
            raise _Stopped("turns", "the agent's turn limit was reached")
        if stop in ("refusal", "cancelled"):
            raise RuntimeError(f"the {self.profile.name} agent ended the turn: {stop}")
        return text

    def _charge(self, usage, turn: _Turn, *, sent_chars: int, text: str) -> None:
        """Charge one turn at policy's price, from the best figure the agent gives:

        1. the session cost it reports, when the profile priced its model at our prices
           (every model call of the turn, summed by the agent): the difference since
           the last turn;
        2. else its reported token usage for the turn;
        3. else an estimate from the text sent and received (CHARS_PER_TOKEN).

        With no price for the model, `fallback_estimate_usd` per model call (1 + the
        turn's tool calls). The call is recorded before `LimitHit` is raised."""
        model, calls = self.agent.model, 1 + turn.tool_calls
        read = int(getattr(usage, "cached_read_tokens", None) or 0)
        write = int(getattr(usage, "cached_write_tokens", None) or 0)
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0) + read + write
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0) + int(
            getattr(usage, "thought_tokens", None) or 0
        )
        self.usage.requests += calls
        self.usage.input_tokens += tokens_in
        self.usage.output_tokens += tokens_out
        price = self.price_for(model)
        name = self.profile.name
        if price is None:
            cost = CallCost(
                "unavailable",
                None,
                tokens_in or None,
                tokens_out or None,
                read or None,
                write or None,
                model,
                None,
                None,
                reason="no price for this model",
            )
            estimate = self.fallback_estimate_usd * calls
        else:
            if self.profile.session_cost and turn.session_cost is not None:
                total = Decimal(str(turn.session_cost))
                estimate = max(total - self._cost_seen, Decimal(0))
                self._cost_seen = max(self._cost_seen, total)
                reason = f"{name}: session cost at policy prices"
            elif tokens_in + tokens_out > 0:
                estimate = estimate_usd(price, tokens_in, tokens_out, read, write)
                reason = f"{name}: usage at policy prices"
            else:
                tokens_in = math.ceil(sent_chars / CHARS_PER_TOKEN)
                tokens_out = math.ceil(len(text) / CHARS_PER_TOKEN)
                estimate = estimate_usd(price, tokens_in, tokens_out)
                reason = f"{name} reported no usage: estimated client-side from text length"
            cost = CallCost(
                "estimated",
                estimate,
                tokens_in,
                tokens_out,
                read,
                write,
                model,
                None,
                None,
                reason=reason,
            )
        if estimate <= 0:
            return
        try:
            charged = self.budget.charge(cost, estimate)
        except LimitHit:
            self.costs.add(cost, estimate)
            raise
        self.costs.add(cost, charged)

    def _outcome(self, status, text, *, stopped=None, detail=None, errors=None) -> AgentOutcome:
        if detail is None and self._client.refused:
            detail = "refused when the agent asked: " + "; ".join(self._client.refused)
        return AgentOutcome(
            status=status,
            output=text,
            stopped=stopped,
            usage=self.usage,
            costs=self.costs,
            detail=detail,
            errors=list(errors or []),
        )


class _Stopped(Exception):
    def __init__(self, why: str, detail: str) -> None:
        super().__init__(detail)
        self.why, self.detail = why, detail


class AcpBackend:
    """Builds a locked-down ACP runner for each step, from one agent's profile."""

    def __init__(
        self,
        profile: AcpProfile,
        settings: AcpSettings,
        *,
        identity_for: Callable[["StepContext"], CallIdentity],
        prices: Pricer,
        # our shell policy for the step: the CLI passes the guard's (framework floor
        # included, `Guard.shell_rules`); default, the agent's resolved lists
        rules_for: Callable[["StepContext"], ShellRules] | None = None,
        wrap_for: Callable[["StepContext"], Sequence[str] | None] = lambda step: None,
        fallback_estimate_usd: Decimal = Decimal("0.01"),  # per call of an unpriced model
        time_limit_s: float | None = None,  # None: the agent's max_minutes
    ) -> None:
        self.profile, self.settings = profile, settings
        self.name = profile.name
        self.identity_for, self.prices = identity_for, prices
        self.rules_for, self.wrap_for = rules_for, wrap_for
        self.fallback_estimate_usd, self.time_limit_s = fallback_estimate_usd, time_limit_s

    def runner(
        self,
        step: "StepContext",
        *,
        tool_context: ToolContext | None,
        state_dir: Path,
        spawner: "ChildSpawner | None",
        is_child: bool = False,
    ) -> AcpRunner:
        agent = step.agent
        cwd = tool_context.tree if tool_context is not None else state_dir
        home = (
            self.settings.home_root
            / step.run_id
            / f"{step.step}-{step.iteration}"
            / agent.definition.name
        )
        rules = self.rules_for(step) if self.rules_for else ShellRules.from_agent(agent)
        setup = StepSetup(
            agent=agent,
            gateway_url=self.settings.gateway_url,
            api_key=self.settings.api_key,
            headers=self.identity_for(step).headers(),
            rules=rules,
            price=self.prices(agent.model),
            home=home,
            tree=cwd,
        )
        launch = self.profile.configure(setup)
        return AcpRunner(
            agent,
            self.profile,
            command=self.settings.command or self.profile.command,
            env=launch_env(self.profile, launch, home),
            home=home,
            cwd=cwd,
            budget=step.budget,
            price_for=self.prices,
            contract=step.contract,
            files=dict(launch.files),
            time_limit_s=self.time_limit_s,
            wrap=self.wrap_for(step),
            fallback_estimate_usd=self.fallback_estimate_usd,
        )
