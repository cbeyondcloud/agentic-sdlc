"""The claude-code backend: Claude Code driven through the Claude Agent SDK, locked down
(M1-29, spike S2). Optional: needs the `claude-code` extra of agentic-core.

What S2 proved, applied to every run:
  * every model call goes to TokenLean (`ANTHROPIC_BASE_URL`) with the user's proxy
    key and our identity headers (`ANTHROPIC_CUSTOM_HEADERS`), on the resolved model -
    the small/fast model is pinned to it too;
  * nothing else leaves the machine: Claude Code's telemetry, error reporting,
    auto-update and other non-essential traffic are switched off;
  * no user or project settings, hooks, plugins or CLAUDE.md load (`--bare` and an
    explicit empty `setting_sources`), and each step has its own config folder;
  * the model is offered only the tools mapped from the agent's capabilities
    (`tools=`; `allowed_tools` only auto-approves), and the guard's permission rules
    apply in don't-ask mode - anything not allowed is refused, never asked about.

This module imports `claude_agent_sdk` only when a run starts, so agentic-core works
without the extra.
"""

import asyncio
import copy
import json
import logging
import os
import shlex
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
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
    no_answer,
    retry_message,
)
from agentic.agents.tools import ToolContext
from agentic.backends.capabilities import CAPABILITIES
from agentic.backends.processes import ProcessTree, session_leader
from agentic.backends.turns import TurnCheck, TurnGuard
from agentic.killswitch import RunKilled
from agentic.policy import ResolvedAgent
from agentic.sandbox.process import SECRET_NAME
from agentic.transport import BudgetSlice, CallCost, CallIdentity, LimitHit, Pricer
from agentic.transport.cost import estimate_usd

if TYPE_CHECKING:
    from agentic.agents.children import ChildSpawner
    from agentic.engine.engine import StepContext

_log = logging.getLogger("agentic.backends.claude_code")
#: How long the runner looks for the Claude Code process the SDK starts (DF-481): it reads
#: the SDK's private `_transport._process`, so a renamed attribute must end in a warning,
#: not a poll for the whole session.
ADOPT_DEADLINE_S = 30.0

#: Switches that stop every request Claude Code makes besides model calls (S2 Q3).
QUIET = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
}


#: Never inherited from the user's environment (review I1): another provider's switch
#: or credential would route Claude Code around TokenLean or leak to the agent.
_BLANKED_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "GOOGLE_",
    "GCLOUD_",
    "CLOUDSDK_",
    "VERTEX_",
    "BEDROCK_",
    "OPENAI_",
    "GEMINI_",
    "CLAUDE_CODE_USE_",
)
_BLANKED = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS"}
#: The token counts of one model call's usage, as Claude Code streams them.
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _blanked(name: str) -> bool:
    upper = name.upper()
    return (
        upper.startswith(_BLANKED_PREFIXES) or upper in _BLANKED or bool(SECRET_NAME.search(upper))
    )


@dataclass(frozen=True)
class ClaudeCodeSettings:
    gateway_url: str  # TokenLean's root
    api_key: str = field(repr=False)  # the user's TokenLean key
    config_root: Path  # per-step CLAUDE_CONFIG_DIR folders live here
    cli_path: Path | None = None  # None: the SDK's bundled (Anthropic-signed) binary


def lock_down(
    step: "StepContext",
    *,
    settings: ClaudeCodeSettings,
    identity: CallIdentity,
    permissions: dict,
    cwd: Path,
    edit_tree: bool,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """The `ClaudeAgentOptions` keyword arguments for one step: every lock-down setting."""
    agent = step.agent
    mapped = CAPABILITIES["claude-code"].tools
    tools = list(dict.fromkeys(name for cap in agent.tools for name in mapped.get(cap, ())))
    rules = copy.deepcopy(permissions)
    if edit_tree:  # a redirect or file edit inside the tree is a write (S2 Q6)
        allow = rules.setdefault("permissions", {}).setdefault("allow", [])
        allow.append("Edit(./**)")
    config_dir = (
        settings.config_root / step.run_id / f"{step.step}-{step.iteration}" / agent.definition.name
    )
    env = {
        "ANTHROPIC_BASE_URL": settings.gateway_url,
        "ANTHROPIC_API_KEY": settings.api_key,
        "ANTHROPIC_CUSTOM_HEADERS": "\n".join(f"{k}: {v}" for k, v in identity.headers().items()),
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": agent.model,
        "ANTHROPIC_SMALL_FAST_MODEL": agent.model,
        "CLAUDE_CONFIG_DIR": str(config_dir),
        **QUIET,
    }
    for name in environ if environ is not None else os.environ:
        if name not in env and _blanked(name):
            env[name] = ""  # the SDK merges the user's environment under ours (review I1)
    options: dict = {
        "cwd": str(cwd),
        "model": agent.model,
        "env": env,
        "setting_sources": [],
        "extra_args": {"bare": None, "no-session-persistence": None},
        "tools": tools,
        # No `allowed_tools`: a bare tool name there approves EVERY use of the tool
        # (review C1). The guard's rules in `settings` are the only approvals.
        "permission_mode": "dontAsk",
        "settings": json.dumps(rules),
    }
    if agent.limits.max_turns is not None:
        options["max_turns"] = agent.limits.max_turns
    if agent.effort is not None:
        options["effort"] = agent.effort
    available = step.budget.available()
    if available is not None:
        options["max_budget_usd"] = float(available)
    if settings.cli_path is not None:
        options["cli_path"] = str(settings.cli_path)
    return options


class ClaudeCodeRunner:
    """Runs one agent step in one Claude Code session (an `AgentRunner`).

    The answer is checked against the step's contract; errors go back once, in the
    same session, then the run ends `invalid` - as on the native backend. After every
    answer the step is charged from Claude Code's reported token usage at policy's
    prices (`estimated`): never from Claude Code's own cost figure or TokenLean's
    header, so the budget means the same on every backend (S2 Q8). A charge past the
    slice raises `LimitHit` for the engine. Claude Code never pauses for a person:
    don't-ask mode refuses whatever the rules do not allow. The kill switch is asked
    before each prompt and at each model call Claude Code streams (`turn_guard`, decision
    89): a kill stops the session at once; a pause holds the run at the next step.
    """

    def __init__(
        self,
        agent: ResolvedAgent,
        *,
        options: dict,
        budget: BudgetSlice,
        price_for: Pricer,
        contract: OutputContract | None,
        time_limit_s: float | None,
        wrap: Sequence[str] | None = None,
        fallback_estimate_usd: Decimal = Decimal("0.01"),
        turn_guard: TurnGuard | None = None,
    ) -> None:
        self.agent, self.options, self.budget = agent, dict(options), budget
        self.price_for, self.contract = price_for, contract
        minutes = agent.limits.max_minutes
        self.time_limit_s = (
            time_limit_s if time_limit_s is not None else (minutes * 60 if minutes else None)
        )
        self.wrap = list(wrap) if wrap else None
        self.fallback_estimate_usd = fallback_estimate_usd
        self.costs = CostLog()
        self.usage = RunUsage()
        self._last_text = ""
        self._client = None
        self._tree = ProcessTree()
        self.turn_check = TurnCheck(turn_guard)

    async def run(self, task: AgentTask) -> AgentOutcome:
        session = asyncio.ensure_future(self._session(task))
        try:
            done, _ = await asyncio.wait({session}, timeout=self.time_limit_s)
        except asyncio.CancelledError:  # the run was stopped: so is everything it started
            self._kill_tree()
            session.cancel()
            raise
        if not done:  # the time limit: kill Claude Code and what it started, then close
            self._kill_tree()
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

    def _kill_tree(self) -> None:
        """Kill the Claude Code process and every process it started (review I4): it
        starts each shell command in a process group of its own, so the shared helper
        kills every descendant, the group and (Windows) the job."""
        self._adopt()
        self._tree.kill()

    def _adopt(self) -> bool:
        """Contain the Claude Code process once the SDK has started it."""
        if self._tree.pid is not None:
            return True
        process = getattr(getattr(self._client, "_transport", None), "_process", None)
        if getattr(process, "pid", None) is None:
            return False
        self._tree.adopt(process)
        return True

    async def _adopt_when_started(self) -> None:
        """The SDK starts Claude Code inside `connect`, before its handshake: adopt it
        at once, so even a stop during the handshake kills it (DF-451). Gives up after
        ADOPT_DEADLINE_S with a warning (DF-481)."""
        deadline = asyncio.get_running_loop().time() + ADOPT_DEADLINE_S
        while not self._adopt():
            if asyncio.get_running_loop().time() >= deadline:
                _log.warning(
                    "the Claude Code process was not found within %.0f s, so it runs outside "
                    "the step's process scope (has claude-agent-sdk changed?); a stop may "
                    "leave its commands running",
                    ADOPT_DEADLINE_S,
                )
                return
            await asyncio.sleep(0.01)

    async def resume(self, state_path, decisions, expected_digest) -> AgentOutcome:
        raise ResumeError("claude-code steps never pause for approval (don't-ask mode)")

    async def _session(self, task: AgentTask) -> AgentOutcome:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        options = dict(self.options)
        options["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": _instructions(task, self.contract),
        }
        if os.name != "nt":  # a session leader, so the time limit can kill its group
            options["cli_path"] = str(_launcher(self.wrap or [], options))
        elif self.wrap:
            raise RuntimeError("wrapping Claude Code in a sandbox needs macOS, Linux or WSL2 (D38)")
        client = self._client = ClaudeSDKClient(options=ClaudeAgentOptions(**options))
        self._tree = ProcessTree()
        watcher = asyncio.ensure_future(self._adopt_when_started())
        try:
            async with client:
                try:
                    return await self._converse(client, task)
                except RunKilled:  # stopped now, not after a graceful shutdown
                    self._kill_tree()
                    raise
                finally:  # its shell commands, before the SDK shuts Claude Code down
                    if not self.wrap or sys.platform == "darwin":  # (Linux srt: the PID
                        self._tree.kill_below()  # namespace ends them with the sandbox)
        finally:  # nothing Claude Code started outlives the session, however it ends
            watcher.cancel()
            self._tree.close()

    async def _converse(self, client, task: AgentTask) -> AgentOutcome:
        text = await self._turn(client, task.prompt)
        if self.contract is None:  # silence is no answer (DF-433)
            errors = no_answer(text)
            return self._outcome("invalid" if errors else "completed", text, errors=errors)
        accepted, errors = self.contract.check(text)
        if errors:  # one retry, in the same session (OUTPUT_RETRIES = 1)
            text = await self._turn(client, retry_message(errors))
            accepted, errors = self.contract.check(text)
        if errors:
            return self._outcome("invalid", text, errors=errors)
        return self._outcome("completed", accepted if accepted is not None else text)

    async def _turn(self, client, prompt: str) -> str:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        await self.turn_check()  # a kill before the prompt starts any model call
        await client.query(prompt)
        text = ""
        # per model call (its message id), as streamed: one message per content block, each
        # with its call's usage - so the largest counts win, never their sum (DF-77)
        streamed: dict[str, dict[str, int]] = {}
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                said = "".join(b.text for b in message.content if isinstance(b, TextBlock))
                if said:
                    text = self._last_text = said
                key = message.message_id or f"#{len(streamed)}"
                new_call = key not in streamed
                call = streamed.setdefault(key, {})
                for name in _USAGE_KEYS:
                    value = int((message.usage or {}).get(name) or 0)
                    call[name] = max(call.get(name, 0), value)
                if new_call:  # a turn boundary: what the model asked for has not run yet
                    await self._turn_boundary(streamed)
            elif isinstance(message, ResultMessage):
                usage = message.usage or {}
                reported = int(usage.get("input_tokens") or 0) + int(
                    usage.get("output_tokens") or 0
                )
                # A run Claude Code stopped itself reports no usage: charge what was streamed.
                seen = {key: sum(c.get(key, 0) for c in streamed.values()) for key in _USAGE_KEYS}
                self._charge(usage if reported else seen, calls=max(len(streamed), 1))
                if message.result:
                    text = self._last_text = message.result
                self._ended(message)
        return text

    async def _turn_boundary(self, streamed: dict[str, dict[str, int]]) -> None:
        """Ask the kill switch; on a kill, charge what was streamed so far (Claude Code
        reports a turn's usage only at its end) and raise `RunKilled` - the stop wins over
        a limit the charge crosses."""
        try:
            await self.turn_check()
        except RunKilled:
            seen = {key: sum(c.get(key, 0) for c in streamed.values()) for key in _USAGE_KEYS}
            try:
                self._charge(seen, calls=max(len(streamed), 1))
            except LimitHit:
                pass
            raise

    def _ended(self, result) -> None:
        """How Claude Code ended the turn: anything but success stops the step."""
        kind = result.subtype
        if kind == "error_max_budget_usd":  # its own estimate crossed the slice: pause
            raise LimitHit(
                self.budget.scope,
                self.budget.name,
                self.budget.limit_usd,
                self.budget.total_spent(),
            )
        if kind == "error_max_turns":
            raise _Stopped("turns", "the agent's turn limit was reached")
        if result.is_error:
            raise RuntimeError(f"Claude Code ended the step with {kind}: {result.result or ''}")

    def _charge(self, usage: dict, calls: int = 1) -> None:
        """Charge one answer's usage to the step at policy's price (LimitHit past the slice).
        Unpriced, it is `fallback_estimate_usd` for each of its `calls` model calls."""
        fresh = int(usage.get("input_tokens") or 0)
        read = int(usage.get("cache_read_input_tokens") or 0)
        write = int(usage.get("cache_creation_input_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or 0)
        tokens_in = fresh + read + write  # Claude Code counts cached tokens separately
        self.usage.requests += 1
        self.usage.input_tokens += tokens_in
        self.usage.output_tokens += tokens_out
        if tokens_in + tokens_out == 0:
            return
        price = self.price_for(self.agent.model)
        if price is None:
            cost = CallCost(
                "unavailable",
                None,
                tokens_in,
                tokens_out,
                read,
                write,
                self.agent.model,
                None,
                None,
                reason="no price for this model",
            )
            estimate = self.fallback_estimate_usd * calls
        else:
            estimate = estimate_usd(price, tokens_in, tokens_out, read, write)
            cost = CallCost(
                "estimated",
                estimate,
                tokens_in,
                tokens_out,
                read,
                write,
                self.agent.model,
                None,
                None,
                reason="claude-code usage priced by policy",
            )
        if estimate <= 0:
            return
        # The call already happened: record it, then stop past the slice (review I3).
        try:
            charged = self.budget.charge(cost, estimate)
        except LimitHit:
            self.costs.add(cost, estimate)
            raise
        self.costs.add(cost, charged)

    def _outcome(self, status, text, *, stopped=None, detail=None, errors=None) -> AgentOutcome:
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


class ClaudeCodeBackend:
    """Builds a locked-down Claude Code runner for each step."""

    name = "claude-code"

    def __init__(
        self,
        settings: ClaudeCodeSettings,
        *,
        identity_for: Callable[["StepContext"], CallIdentity],
        prices: Pricer,
        permissions_for: Callable[["StepContext"], dict],
        wrap_for: Callable[["StepContext"], Sequence[str] | None] = lambda step: None,
        fallback_estimate_usd: Decimal = Decimal("0.01"),  # per call of an unpriced model
    ) -> None:
        self.settings, self.identity_for, self.prices = settings, identity_for, prices
        self.permissions_for, self.wrap_for = permissions_for, wrap_for
        self.fallback_estimate_usd = fallback_estimate_usd

    def runner(
        self,
        step: "StepContext",
        *,
        tool_context: ToolContext | None,
        state_dir: Path,
        spawner: "ChildSpawner | None",
        is_child: bool = False,
    ) -> ClaudeCodeRunner:
        cwd = tool_context.tree if tool_context is not None else state_dir
        options = lock_down(
            step,
            settings=self.settings,
            identity=self.identity_for(step),
            permissions=self.permissions_for(step),
            cwd=cwd,
            edit_tree="edit_files" in step.agent.tools,
        )
        return ClaudeCodeRunner(
            step.agent,
            options=options,
            budget=step.budget,
            price_for=self.prices,
            contract=step.contract,
            time_limit_s=None,
            wrap=self.wrap_for(step),
            fallback_estimate_usd=self.fallback_estimate_usd,
            turn_guard=step.turn_guard,
        )


def _launcher(wrap: Sequence[str], options: dict) -> Path:
    """An executable script that starts Claude Code as a session leader (so the time
    limit can kill its whole process group, review I4), under `wrap` (srt, D38) when
    given. POSIX only."""
    claude = options.get("cli_path") or str(bundled_cli())
    folder = Path(options["env"]["CLAUDE_CONFIG_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "claude-launcher"
    parts = session_leader([*wrap, claude])
    command = " ".join(shlex.quote(part) for part in parts)
    script.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def bundled_cli() -> Path:
    """The Claude Code binary bundled in the pinned claude-agent-sdk wheel."""
    import claude_agent_sdk

    name = "claude.exe" if os.name == "nt" else "claude"
    return Path(claude_agent_sdk.__file__).parent / "_bundled" / name
