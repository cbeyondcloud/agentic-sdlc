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
import os
import shlex
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
from agentic.backends.capabilities import CAPABILITIES
from agentic.policy import ResolvedAgent
from agentic.transport import BudgetSlice, CallCost, CallIdentity, LimitHit, Pricer
from agentic.transport.cost import estimate_usd

if TYPE_CHECKING:
    from agentic.agents.children import ChildSpawner
    from agentic.engine.engine import StepContext

#: Switches that stop every request Claude Code makes besides model calls (S2 Q3).
QUIET = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
}


@dataclass(frozen=True)
class ClaudeCodeSettings:
    gateway_url: str  # TokenLean's root
    api_key: str  # the user's TokenLean key
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
    options: dict = {
        "cwd": str(cwd),
        "model": agent.model,
        "env": env,
        "setting_sources": [],
        "extra_args": {"bare": None, "no-session-persistence": None},
        "tools": tools,
        "allowed_tools": list(tools),
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
    don't-ask mode refuses whatever the rules do not allow.
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

    async def run(self, task: AgentTask) -> AgentOutcome:
        try:
            return await asyncio.wait_for(self._session(task), self.time_limit_s)
        except _Stopped as stop:
            return self._outcome("stopped", self._last_text, stopped=stop.why, detail=stop.detail)
        except TimeoutError:  # the session is closed: the Claude Code process ends
            return self._outcome(
                "stopped",
                self._last_text,
                stopped="time",
                detail=f"the step's time limit ({self.time_limit_s}s) was reached",
            )

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
        if self.wrap:
            options["cli_path"] = str(_wrapper(self.wrap, options))
        async with ClaudeSDKClient(options=ClaudeAgentOptions(**options)) as client:
            text = await self._turn(client, task.prompt)
            if self.contract is None:
                return self._outcome("completed", text)
            accepted, errors = self.contract.check(text)
            if errors:  # one retry, in the same session (OUTPUT_RETRIES = 1)
                text = await self._turn(client, retry_message(errors))
                accepted, errors = self.contract.check(text)
            if errors:
                return self._outcome("invalid", text, errors=errors)
            return self._outcome("completed", accepted if accepted is not None else text)

    async def _turn(self, client, prompt: str) -> str:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        await client.query(prompt)
        text = ""
        seen = {"input_tokens": 0, "output_tokens": 0}  # per model call, as streamed
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                said = "".join(b.text for b in message.content if isinstance(b, TextBlock))
                if said:
                    text = self._last_text = said
                for key in seen:
                    seen[key] += int((message.usage or {}).get(key) or 0)
            elif isinstance(message, ResultMessage):
                usage = message.usage or {}
                reported = int(usage.get("input_tokens") or 0) + int(
                    usage.get("output_tokens") or 0
                )
                # A run Claude Code stopped itself reports no usage: charge what was streamed.
                self._charge(usage if reported else seen)
                if message.result:
                    text = self._last_text = message.result
                self._ended(message)
        return text

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

    def _charge(self, usage: dict) -> None:
        """Charge one answer's usage to the step at policy's price (LimitHit past the slice)."""
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
            estimate = self.fallback_estimate_usd
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
        reservation = self.budget.reserve(estimate)  # LimitHit if it does not fit
        try:
            charged = reservation.settle(cost)
        except Exception:
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
    ) -> None:
        self.settings, self.identity_for, self.prices = settings, identity_for, prices
        self.permissions_for, self.wrap_for = permissions_for, wrap_for

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
        )


def _wrapper(wrap: Sequence[str], options: dict) -> Path:
    """An executable script that runs Claude Code under `wrap` (srt, D38). POSIX only."""
    if os.name == "nt":
        raise RuntimeError("wrapping Claude Code in a sandbox needs macOS, Linux or WSL2 (D38)")
    claude = options.get("cli_path") or str(bundled_cli())
    folder = Path(options["env"]["CLAUDE_CONFIG_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "claude-in-sandbox"
    command = " ".join(shlex.quote(part) for part in [*wrap, claude])
    script.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def bundled_cli() -> Path:
    """The Claude Code binary bundled in the pinned claude-agent-sdk wheel."""
    import claude_agent_sdk

    name = "claude.exe" if os.name == "nt" else "claude"
    return Path(claude_agent_sdk.__file__).parent / "_bundled" / name
