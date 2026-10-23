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
import logging
import math
import os
import shutil
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from pydantic_ai.usage import RunUsage

from agentic.agents.metering import CostLog
from agentic.agents.runner import (
    AgentOutcome,
    AgentTask,
    OutputContract,
    ResumeError,
    _instructions,
    no_answer,
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
from agentic.backends.processes import ProcessTree, session_leader
from agentic.backends.turns import TurnCheck, TurnGuard
from agentic.killswitch import RunKilled, Verdict
from agentic.policy import ResolvedAgent
from agentic.sandbox.process import SECRET_NAME
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
#: Removing a step home: tries, and the pause before each retry (grows each time).
REMOVE_TRIES = 3
REMOVE_PAUSE_S = 0.2
_log = logging.getLogger("agentic.backends.acp")
_STDERR = "agent-stderr.log"
_SHIMS = (".cmd", ".bat")


@dataclass(frozen=True)
class AcpSettings:
    gateway_url: str  # TokenLean's root
    api_key: str = field(repr=False)  # the user's TokenLean key
    home_root: Path  # per-step agent homes live here
    command: tuple[str, ...] | None = None  # the agent's command; None: the profile's
    # variables org policy lets through (`sandbox.pass_env`, DF-541)
    pass_env: tuple[str, ...] = ()


def resolve_command(
    command: Sequence[str],
    which=shutil.which,
    *,
    path: str | None = None,
    tree: Path | None = None,
) -> list[str]:
    """The agent command with its program as an absolute path: a full path as given, else
    found in the absolute folders on PATH (`path`; default the environment's) - never in
    the current folder, which Windows' own lookup searches first, nor in a relative PATH
    entry, nor inside the step's tree: a checkout carrying the agent's name would run on
    the host, unsandboxed. A Windows script shim (npm's `.cmd`) is refused: it would run
    through cmd.exe, which re-parses its arguments."""
    program = command[0]
    if os.path.isabs(program):
        found = which(program)  # a folder given: no search, so no current folder either
    elif os.path.dirname(program):
        raise RuntimeError(
            f"the agent command {program!r} is a relative path: configure the full path "
            "to its executable, or its name alone to find it on PATH"
        )
    else:
        entries = (os.environ.get("PATH", "") if path is None else path).split(os.pathsep)
        cwd = os.path.normcase(os.path.abspath(os.getcwd()))
        found = None
        for entry in entries:
            if not entry or not os.path.isabs(entry):  # `.`, `bin`, `` mean the current folder
                continue
            if os.path.normcase(os.path.abspath(entry)) == cwd:
                continue
            found = which(os.path.join(entry, program))
            if found is not None:
                break
    if found is None:
        raise RuntimeError(
            f"the agent command {program!r} was not found on PATH (its absolute folders "
            "only): install the pinned release, or configure the full path to its executable"
        )
    if tree is not None and Path(found).resolve().is_relative_to(Path(tree).resolve()):
        raise RuntimeError(
            f"{found} is inside the step's tree: the agent's executable must be installed "
            "outside the repository"
        )
    if Path(found).suffix.lower() in _SHIMS:
        raise RuntimeError(
            f"{found} is a script shim, not an executable: configure the path to the "
            "agent's own (signed) executable instead"
        )
    return [found, *command[1:]]


def _free_port(host: str) -> int:
    """A port free on `host` now. Inside srt the agent has a network namespace of its own,
    so nothing else can take it; unwrapped the window is a moment."""
    with socket.socket() as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


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
        # awaited at each tool call the agent reports: a turn boundary (the kill switch)
        self.on_tool_call: Callable[[], Awaitable[None]] | None = None

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
            if self.on_tool_call is not None:
                await self.on_tool_call()
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
    """What was refused, for the step's record: the kind of tool and the command's program
    only - its arguments can carry a credential (the guard never quotes a command either),
    and an agent's tool title often is the whole command."""
    kind = getattr(tool_call, "kind", None) or "tool"
    raw = getattr(tool_call, "raw_input", None)
    command = raw.get("command") if isinstance(raw, dict) else None
    words = command.split() if isinstance(command, str) else []
    return f"{kind}: {words[0]} ..." if words else str(kind)


class AcpRunner:
    """Runs one agent step in one ACP session (an `AgentRunner`). It never pauses for a
    person: every permission request is refused. The kill switch is asked before each
    prompt and at each tool call the agent reports (`turn_guard`, decision 89): a kill
    stops the session at once; a pause holds the run at the next step."""

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
        turn_guard: TurnGuard | None = None,
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
        self._tree = ProcessTree()
        self._last_text = ""
        self._cost_seen = Decimal(0)
        self.turn_check = TurnCheck(turn_guard)
        self._client.on_tool_call = self._tool_boundary
        self._session_task: asyncio.Future | None = None
        self._killed: Verdict | None = None  # a kill seen at a tool call

    async def run(self, task: AgentTask) -> AgentOutcome:
        session = self._session_task = asyncio.ensure_future(self._session(task))
        try:
            done, _ = await asyncio.wait({session}, timeout=self.time_limit_s)
        except asyncio.CancelledError:  # the run was stopped: so is everything it started
            self._kill()
            session.cancel()
            raise
        if self._killed is not None:  # the kill switch at a tool call cancelled the session
            self._kill()
            try:
                await session
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - it was cancelled
                pass
            raise RunKilled(self._killed)
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
        """Kill the agent and every process it started (the shared helper)."""
        self._tree.kill()

    async def _tool_boundary(self) -> None:
        """At a tool call the agent reported: ask the kill switch. A kill stops the agent
        and cancels the session (a notification handler cannot raise into the runner);
        `run` then raises `RunKilled`."""
        try:
            await self.turn_check()
        except RunKilled as killed:
            self._killed = killed.verdict
            self._kill()
            if self._session_task is not None:
                self._session_task.cancel()

    def _launch(self) -> tuple[list[str], dict[str, str]]:
        """The argv and environment the agent starts with: under `wrap` when given, as a
        session leader on POSIX, its own server (if any) on a free port called directly."""
        if self.wrap and os.name == "nt":
            raise RuntimeError(
                "wrapping an agent CLI in a sandbox needs macOS, Linux or WSL2 (D38)"
            )
        command, env = resolve_command(self.command, tree=self.cwd), dict(self.env)
        if self.profile.own_server is not None:
            flag, host = self.profile.own_server
            direct = f"{host}:{_free_port(host)}"
            command += [flag, direct.rsplit(":", 1)[1]]
            # both spellings, merged: Linux and macOS users often set only `no_proxy` (DF-449)
            said = f"{env.get('NO_PROXY', '')},{env.get('no_proxy', '')}"
            given = [h.strip() for h in said.split(",") if h.strip()]
            env["NO_PROXY"] = env["no_proxy"] = ",".join(dict.fromkeys([*given, direct]))
            if self.wrap:  # inside, the sandbox's proxy takes everything else
                command = ["env", f"NO_PROXY={direct}", f"no_proxy={direct}", *command]
        return session_leader([*(self.wrap or []), *command]), env

    async def _session(self, task: AgentTask) -> AgentOutcome:
        import acp
        from acp import schema

        refused = self.profile.refuse_tree(self.cwd) if self.profile.refuse_tree else []
        if refused:
            raise RuntimeError(
                f"the {self.profile.name} backend refuses this tree: the agent would run "
                f"code from it whatever its config says - {'; '.join(refused)}. Remove "
                "those files from the branch, or run the step on another backend"
            )
        argv, env = self._launch()
        shutil.rmtree(self.home, ignore_errors=True)  # a retried step starts clean
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            write_files(self.home, self.files)
            with (self.home / _STDERR).open("wb") as stderr:
                async with acp.spawn_agent_process(
                    self._client,
                    argv[0],
                    *argv[1:],
                    env=env,
                    cwd=str(self.cwd),
                    transport_kwargs={"stderr": stderr, "limit": STDIO_LIMIT},
                ) as (conn, process):
                    self._process = process
                    self._tree = ProcessTree().adopt(process)
                    try:
                        return await self._converse(conn, task, acp, schema)
                    finally:  # nothing it started outlives the step, however it ends
                        self._tree.close()
        finally:  # its state - the transcript included - goes with the step
            try:
                self._remove_secrets()  # first, so a locked transcript cannot keep them
            except RuntimeError:
                pass  # the step has ended already: what it raised stands
            await self._remove_home()

    async def _remove_home(self) -> None:
        """Delete the step home, retrying (Windows holds a file open for a moment after
        its process exits); one that stays is named in a warning, never silently left
        (DF-448). The step's result stands either way."""
        for attempt in range(REMOVE_TRIES):
            try:
                shutil.rmtree(self.home)
                return
            except FileNotFoundError:
                return
            except OSError as error:
                failure = error
            if attempt + 1 < REMOVE_TRIES:
                await asyncio.sleep(REMOVE_PAUSE_S * (attempt + 1))
        _log.warning(
            "the %s agent's step home %s could not be removed (%s): delete it by hand - it "
            "holds the agent's transcript",
            self.profile.name,
            self.home,
            failure.strerror or type(failure).__name__,
        )

    async def _converse(self, conn, task: AgentTask, acp, schema) -> AgentOutcome:
        try:
            sid = await self._open(conn, schema)
        except (ConnectionError, acp.RequestError) as error:
            raise RuntimeError(
                f"the {self.profile.name} agent did not start a session ({error}); "
                f"its stderr ends: {self._stderr_tail()}"
            ) from error
        first = [acp.text_block(_instructions(task, self.contract)), acp.text_block(task.prompt)]
        text = await self._turn(conn, sid, first)
        if self.contract is None:  # silence is no answer (DF-433)
            errors = no_answer(text)
            return self._outcome("invalid" if errors else "completed", text, errors=errors)
        accepted, errors = self.contract.check(text)
        if errors:  # one retry, in the same session (OUTPUT_RETRIES = 1)
            text = await self._turn(conn, sid, [acp.text_block(retry_message(errors))])
            accepted, errors = self.contract.check(text)
        if errors:
            return self._outcome("invalid", text, errors=errors)
        return self._outcome("completed", accepted if accepted is not None else text)

    async def _open(self, conn, schema) -> str:
        """Initialize (ACP v1 only), authenticate and open a session; its id."""
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
                f"the {self.profile.name} agent speaks ACP protocol {init.protocol_version}; "
                f"only {PROTOCOL_VERSION} is supported"
            )
        if self.profile.auth_method:
            await conn.authenticate(method_id=self.profile.auth_method)
        session = await conn.new_session(cwd=str(self.cwd), mcp_servers=[])
        self._remove_secrets()  # read while it loaded its config: its commands find nothing
        if self.profile.mode:
            await conn.set_session_mode(session_id=session.session_id, mode_id=self.profile.mode)
        return session.session_id

    def _remove_secrets(self) -> None:
        """Delete the profile's secret files from the step home. One that cannot be
        deleted (a lock on Windows) stops the step: the agent's shell could read it."""
        for name in self.profile.secret_files:
            try:
                self.home.joinpath(*PurePosixPath(name).parts).unlink(missing_ok=True)
            except OSError as error:
                raise RuntimeError(
                    f"the {self.profile.name} agent's secret file {name!r} could not be "
                    f"removed from its home ({error.strerror or error})"
                ) from None

    def _stderr_tail(self, limit: int = 2000) -> str:
        """The end of the agent's stderr, with every secret the step gave it masked: the
        profile's files (a key) and secret-named variables' values (DF-448)."""
        try:
            text = (self.home / _STDERR).read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return "(no stderr)"
        secrets = [*self.files.values()]
        secrets += [value for name, value in self.env.items() if SECRET_NAME.search(name)]
        for secret in sorted({s.strip() for s in secrets if len(s.strip()) >= 4}, key=len)[::-1]:
            text = text.replace(secret, "***")
        return text[-limit:].strip() or "(empty)"

    async def _turn(self, conn, session_id: str, blocks: list) -> str:
        await self.turn_check()  # a kill before the prompt starts any model call
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
        3. else a floor estimated from the text (CHARS_PER_TOKEN): the prompt once per
           model call, the answer once.

        A turn charged by 2 or 3 counts towards the next reported session cost, so it is
        never charged twice. With no price for the model, `fallback_estimate_usd` per
        model call (1 + the turn's tool calls). The call is recorded before `LimitHit`."""
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
            else:
                if tokens_in + tokens_out > 0:
                    estimate = estimate_usd(price, tokens_in, tokens_out, read, write)
                    reason = f"{name}: usage at policy prices"
                else:  # a floor: every model call re-sends at least the prompt
                    tokens_in = math.ceil(sent_chars / CHARS_PER_TOKEN) * calls
                    tokens_out = math.ceil(len(text) / CHARS_PER_TOKEN)
                    estimate = estimate_usd(price, tokens_in, tokens_out)
                    reason = f"{name} reported no usage: estimated client-side from text length"
                if self.profile.session_cost:  # a later session cost already includes it
                    self._cost_seen += estimate
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
        # our shell policy for the step: the CLI passes the guard's, framework floor
        # included (`ShellRules(**guard.shell_rules())`); default, the agent's lists
        rules_for: Callable[["StepContext"], ShellRules] | None = None,
        # the srt command prefix for the step (the CLI's `StepWiring.process_wrap_for`)
        wrap_for: Callable[["StepContext"], Sequence[str] | None] | None = None,
        fallback_estimate_usd: Decimal = Decimal("0.01"),  # per call of an unpriced model
        time_limit_s: float | None = None,  # None: the agent's max_minutes
        # no fail-open defaults (DF-445): without rules_for or wrap_for the agent would run
        # with a shell under only its own lists, unsandboxed - tests must ask for that
        unsandboxed: bool = False,
    ) -> None:
        missing = [n for n, f in (("rules_for", rules_for), ("wrap_for", wrap_for)) if f is None]
        if missing and not unsandboxed:
            raise ValueError(
                f"an ACP backend needs {' and '.join(missing)}: the framework's shell rules "
                "and the srt sandbox wrap (pass unsandboxed=True only in tests)"
            )
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
            env=launch_env(self.profile, launch, home, pass_env=self.settings.pass_env),
            home=home,
            cwd=cwd,
            budget=step.budget,
            price_for=self.prices,
            contract=step.contract,
            files=dict(launch.files),
            time_limit_s=self.time_limit_s,
            wrap=self.wrap_for(step) if self.wrap_for is not None else None,
            fallback_estimate_usd=self.fallback_estimate_usd,
            turn_guard=step.turn_guard,
        )
