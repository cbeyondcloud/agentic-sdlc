"""The codex backend: the pinned `codex exec` CLI, locked down (M2-40, spike S3, D42).
Optional: needs the `codex` extra of agentic-core (TOML writing), and the Codex CLI at
CODEX_VERSION on PATH - on macOS or Linux, and on Windows inside WSL2 (D38). Native
Windows is refused: the npm win32 package bundles an unsigned `rg.exe`.

What S3 proved, applied to every run:
  * every model call goes to TokenLean's Responses route (`wire_api = "responses"`) with
    the user's proxy key (`env_key`) and our identity headers plus `traceparent`
    (`env_http_headers`), on the resolved model;
  * nothing else leaves the machine: plugins, apps, analytics, feedback, OTel, update
    checks, history and web search are off;
  * no user, project or repo config loads: each step has its own `CODEX_HOME`, the lock-
    down keys are passed again as `-c` overrides (session flags beat a project layer),
    and the worktree is marked `trust_level = "untrusted"` - the only switch that kept a
    hostile repo's `.codex/` (instructions, MCP servers, rules) out; `AGENTS.md` is off
    (`project_doc_max_bytes = 0`), and so are repo and user skills
    (`skills.include_instructions = false`);
  * Codex's own sandbox is off (`danger-full-access`): srt wraps the whole process and
    is the boundary (no nested sandboxes; Codex's bwrap breaks in CI containers). A run
    without a sandbox wrap is refused unless the settings explicitly allow it (tests);
  * the shell policy is compiled to execpolicy rules (`codex_rules`), and the model is
    described by our catalog (`codex_catalog`, DF-115);
  * the answer is shaped by `--output-schema` (a strict `{"document": ...}` envelope);
  * the step is charged from `turn.completed.usage` at policy prices (`estimated`).
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
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
from agentic.backends.claude_code import _blanked
from agentic.backends.codex_catalog import model_catalog
from agentic.backends.codex_hooks import CodexHook, hook_table
from agentic.backends.codex_rules import ShellRules, compile_rules
from agentic.backends.processes import ProcessTree, spawn_group
from agentic.policy import ResolvedAgent
from agentic.sandbox.process import KILL_WAIT_S, safe_env
from agentic.security.untrusted import HANDOFF_TAG, fence
from agentic.transport import BudgetSlice, CallCost, CallIdentity, LimitHit, Pricer
from agentic.transport.cost import estimate_usd

if TYPE_CHECKING:
    from agentic.agents.children import ChildSpawner
    from agentic.engine.engine import StepContext

#: The pinned Codex CLI release (rust-v0.160.1, 2026-10-05, Apache-2.0; spike S3).
_log = logging.getLogger("agentic.backends.codex")
CODEX_VERSION = "0.160.1"
#: Our provider's id in Codex's config, and the variable holding the gateway key.
PROVIDER = "agentic-gateway"
KEY_ENV = "AGENTIC_GATEWAY_KEY"
#: The answer envelope `--output-schema` enforces when the step has an output contract.
DOCUMENT_SCHEMA = {
    "type": "object",
    "properties": {"document": {"type": "string"}},
    "required": ["document"],
    "additionalProperties": False,
}
DOCUMENT_NOTICE = (
    "Your final answer is JSON with one field, `document`, whose value is the whole "
    "document described above, as one string."
)
_VERSION = re.compile(r"(\d+\.\d+\.\d+)")


class CodexError(RuntimeError):
    """Codex cannot run this step as configured (missing, wrong version, no sandbox)."""


@dataclass(frozen=True)
class CodexSettings:
    gateway_url: str  # TokenLean's root (or its /v1)
    api_key: str = field(repr=False)  # the user's TokenLean key
    home_root: Path  # per-step CODEX_HOME folders live here
    cli: tuple[str, ...] = ("codex",)  # the pinned CLI, on PATH
    retries: int = 1  # request and stream retries (Codex retries a cut stream whole)
    idle_timeout_ms: int = 300_000
    # No unsandboxed codex (decision 22): only tests may turn this off
    require_sandbox: bool = True
    # model names or globs -> catalog fields to tune (context window, priority)
    catalog_overrides: Mapping[str, Mapping[str, object]] = field(default_factory=dict)


@dataclass
class CodexLaunch:
    """Everything one step's `codex exec` is started with. `write` puts the files in
    the step's CODEX_HOME; the developer instructions are added then (they depend on
    the task), in the config file only - see `lock_down`."""

    home: Path
    argv: list[str]
    env: dict[str, str]
    config: dict
    provider: str
    rules: str
    catalog: dict
    schema: dict | None
    last_message: Path

    def write(self, instructions: str) -> None:
        import tomli_w

        (self.home / "rules").mkdir(parents=True, exist_ok=True)
        config = {**self.config, "developer_instructions": instructions}
        _write(self.home / "config.toml", tomli_w.dumps(config))
        _write(self.home / "rules" / "agentic.rules", self.rules)
        _write(self.home / "models.json", json.dumps(self.catalog, indent=2) + "\n")
        if self.schema is not None:
            _write(self.home / "schema.json", json.dumps(self.schema, indent=2) + "\n")
        self.last_message.unlink(missing_ok=True)


def lock_down(
    step: "StepContext",
    *,
    settings: CodexSettings,
    identity: CallIdentity,
    rules: ShellRules,
    cwd: Path,
    srt_boundary: bool,
    hook: CodexHook | None = None,
    environ: Mapping[str, str] | None = None,
) -> CodexLaunch:
    """The command, environment and CODEX_HOME files for one step: every lock-down
    setting. `srt_boundary` says srt wraps the process (allow rules are emitted only
    then); `hook` is the guard hook (DF-116). Paths are resolved: Codex matches the
    trust entry against the real worktree path and the hook's trust key against the
    real config path (macOS temp folders are symlinks)."""
    agent = step.agent
    tree = Path(cwd).resolve().as_posix()
    folder = f"{step.step}-{step.iteration}"
    home = (settings.home_root / step.run_id / folder / agent.definition.name).resolve()
    headers = {**identity.headers(), "traceparent": _traceparent(identity.run_id)}
    header_env = {name: _env_name(name) for name in headers}
    base = settings.gateway_url.rstrip("/")
    provider = {
        "name": "agentic gateway",
        "base_url": base if base.endswith("/v1") else f"{base}/v1",
        "wire_api": "responses",
        "env_key": KEY_ENV,
        "request_max_retries": settings.retries,
        "stream_max_retries": settings.retries,
        "stream_idle_timeout_ms": settings.idle_timeout_ms,
        "env_http_headers": header_env,
    }
    config: dict = {
        "model": agent.model,
        "model_provider": PROVIDER,
        "approval_policy": "never",
        "sandbox_mode": "danger-full-access",
        "check_for_update_on_startup": False,
        "project_doc_max_bytes": 0,
        "web_search": "disabled",
        "model_catalog_json": (home / "models.json").as_posix(),
        "history": {"persistence": "none"},
        # repo (.agents/, .codex/) and user skills would be listed to the model as
        # instructions, untrusted worktree or not (review I2); org skills come with M2-47
        "skills": {"include_instructions": False},
        "features": {
            "plugins": False,
            "apps": False,
            "remote_models": False,
            "memories": False,
            "multi_agent": False,
            "goals": False,
        },
        "analytics": {"enabled": False},
        "feedback": {"enabled": False},
        "otel": {"exporter": "none", "metrics_exporter": "none", "trace_exporter": "none"},
        "shell_environment_policy": {"inherit": "core"},
        "projects": {tree: {"trust_level": "untrusted"}},
        "model_providers": {PROVIDER: provider},
    }
    if agent.effort is not None:
        config["model_reasoning_effort"] = agent.effort
    env = {
        name: value
        for name, value in safe_env(environ).items()
        if not (_blanked(name) or name.upper().startswith("CODEX_"))
    }
    env.update({header_env[name]: value for name, value in headers.items()})
    env.update({"CODEX_HOME": str(home), KEY_ENV: settings.api_key})
    schema = DOCUMENT_SCHEMA if step.contract is not None else None
    last_message = home / "last-message.txt"
    argv = [*settings.cli, "exec", "--json", "--ephemeral", "--strict-config"]
    argv += ["--skip-git-repo-check", "-C", tree, "-m", agent.model]
    if schema is not None:
        argv += ["--output-schema", (home / "schema.json").as_posix()]
    argv += ["-o", last_message.as_posix(), *overrides(config), "-"]
    if hook is not None:  # in the file only: its trust key names the config file's path
        config["hooks"] = hook_table(hook, home / "config.toml")
    catalog = model_catalog(
        agent.model, tools=agent.tools, effort=agent.effort, overrides=settings.catalog_overrides
    )
    return CodexLaunch(
        home=home,
        argv=argv,
        env=env,
        config=config,
        provider=PROVIDER,
        rules=compile_rules(rules, srt_boundary=srt_boundary),
        catalog=catalog,
        schema=schema,
        last_message=last_message,
    )


def overrides(config: Mapping[str, object]) -> list[str]:
    """`config` as `-c key=value` session overrides. A table of bare keys becomes dotted
    keys; a table with any other key (a path: `projects`) becomes one inline table, as do
    header tables. Codex splits a `-c` key on every dot, even inside quotes, so a path in
    a dotted key (`projects."/x/.sdlc-worktrees/..."`) stops it starting (review C1)."""
    out: list[str] = []

    def emit(prefix: str, value: object) -> None:
        if (
            isinstance(value, Mapping)
            and all(_BARE_KEY.fullmatch(key) for key in value)
            and not prefix.endswith("env_http_headers")
        ):
            for key, inner in value.items():
                emit(f"{prefix}.{key}" if prefix else key, inner)
            return
        out.extend(["-c", f"{prefix}={_toml_value(value)}"])

    emit("", config)
    return out


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _toml_value(value: object) -> str:
    """One TOML value, tables written inline (`{ "k" = v, ... }`)."""
    import tomli_w

    if isinstance(value, Mapping):
        pairs = (f"{_toml_key(key)} = {_toml_value(inner)}" for key, inner in value.items())
        return "{ " + ", ".join(pairs) + " }"
    return tomli_w.dumps({"v": value}).strip().removeprefix("v = ")


def _toml_key(key: object) -> str:
    """One TOML key, quoted and escaped as TOML needs (`tomli_w` writes it)."""
    import tomli_w

    return tomli_w.dumps({str(key): 0}).strip().removesuffix(" = 0")


def check_version(cli: Sequence[str], *, run: Callable[[list[str]], str] | None = None) -> str:
    """The CLI's version; CodexError unless it is CODEX_VERSION (the contract tests
    pin that version: another one may change flags, config keys or events)."""
    run = run or _version_output
    try:
        output = run([*cli, "--version"])
    except (FileNotFoundError, PermissionError, subprocess.SubprocessError) as error:
        raise CodexError(
            f"the Codex CLI was not found or did not start ({' '.join(cli)}): install "
            f"version {CODEX_VERSION} on PATH (on Windows, inside WSL2)"
        ) from error
    found = _VERSION.search(output or "")
    version = found.group(1) if found else (output or "").strip() or "unknown"
    if version != CODEX_VERSION:
        raise CodexError(
            f"the Codex CLI is version {version}; this framework is tested with "
            f"{CODEX_VERSION} only - install that version"
        )
    return version


def _version_output(argv: list[str]) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    return done.stdout or done.stderr


def _env_name(header: str) -> str:
    """The variable carrying one header's value: `X-Agentic-Run-Id` ->
    `AGENTIC_HEADER_RUN_ID`, `traceparent` -> `AGENTIC_HEADER_TRACEPARENT`."""
    name = header.upper().removeprefix("X-AGENTIC-").replace("-", "_")
    return f"AGENTIC_HEADER_{name}"


def _traceparent(run_id: str) -> str:
    """A W3C traceparent in the run's trace (the transport's rule: the trace id is the
    run id's hash), so TokenLean's rows join the run's spans."""
    trace_id = hashlib.sha256(run_id.encode()).hexdigest()[:32]
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def native_windows() -> bool:
    return os.name == "nt"


# ── reading `codex exec --json` ───────────────────────────────────────────────

#: Item types that are a tool call: each one's result goes back to the model.
TOOL_ITEMS = frozenset({"command_execution", "file_change", "mcp_tool_call", "web_search"})
MAX_STDERR = 20_000


class EventReader:
    """What one `codex exec --json` turn said, from its JSONL events (S3 Q7/Q11): the
    last agent message, the turn's usage, how many tool calls ran, whether the turn
    started (model calls were made), completed or failed, and the last error (an `error`
    event alone may be transient: Codex reports a dropped stream it then retries)."""

    def __init__(self) -> None:
        self.text = ""
        self.usage: dict | None = None
        self.tool_calls = 0
        self.started = False
        self.completed = False
        self.failed = False
        self.error: str | None = None
        self._tools: set[str] = set()

    @property
    def ended_badly(self) -> bool:
        return self.failed or (self.error is not None and not self.completed)

    def feed(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if kind == "item.completed" and item.get("type") == "agent_message":
            self.text = str(item.get("text") or "")
        elif kind == "item.completed" and item.get("type") in TOOL_ITEMS:
            key = str(item.get("id") or f"#{len(self._tools)}")
            if key not in self._tools:
                self._tools.add(key)
                self.tool_calls += 1
        elif kind == "turn.started":
            self.started = True
        elif kind == "turn.completed":
            self.completed = True
            if isinstance(event.get("usage"), dict):
                self.usage = event["usage"]
        elif kind == "turn.failed":
            self.failed = True
            error = event.get("error")
            self.error = str(error.get("message") if isinstance(error, dict) else error)
        elif kind == "error":
            self.error = str(event.get("message") or "error")


def unwrap_document(text: str) -> str:
    """The `document` of the `--output-schema` envelope; any other text as written (the
    output contract then says what is wrong with it)."""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if isinstance(data, dict) and isinstance(data.get("document"), str):
        return data["document"]
    return text


# ── running a step ────────────────────────────────────────────────────────────


class CodexRunner:
    """Runs one agent step as `codex exec` (an `AgentRunner`).

    The answer is checked against the step's contract. Its errors go back once, in a
    new `codex exec` that carries the first answer as data, then the run ends
    `invalid`, as on the other backends. (`--ephemeral` keeps no session, so there is
    nothing to resume.) After each exec the step is charged from Codex's
    `turn.completed.usage` at policy's prices (`estimated`), never from TokenLean's
    header, which Codex ignores anyway (S3 Q11); an exec killed or failed before that
    event is charged the fallback estimate per model call. A charge past the slice
    raises `LimitHit` for the engine.

    Limits:
      * time  - the step's limit, around both execs; the whole process tree is killed;
      * turns - each tool result costs one more model call, so a tool call past
                `max_turns` stops the step.

    Codex never pauses for a person: `approval_policy = never` refuses prompt rules.
    """

    def __init__(
        self,
        agent: ResolvedAgent,
        *,
        launch: CodexLaunch,
        budget: BudgetSlice,
        price_for: Pricer,
        contract: OutputContract | None,
        time_limit_s: float | None,
        wrap: Sequence[str] | None = None,
        require_sandbox: bool = True,
        fallback_estimate_usd: Decimal = Decimal("0.01"),
    ) -> None:
        self.agent, self.launch, self.budget = agent, launch, budget
        self.price_for, self.contract = price_for, contract
        minutes = agent.limits.max_minutes
        self.time_limit_s = (
            time_limit_s if time_limit_s is not None else (minutes * 60 if minutes else None)
        )
        self.wrap = list(wrap) if wrap else None
        self.require_sandbox = require_sandbox
        self.fallback_estimate_usd = fallback_estimate_usd
        self.costs = CostLog()
        self.usage = RunUsage()
        self._last_text = ""
        self._process: asyncio.subprocess.Process | None = None
        self._tree: ProcessTree | None = None
        self._pending: EventReader | None = None  # the running exec's, until charged
        self._turns = 0

    async def run(self, task: AgentTask) -> AgentOutcome:
        if self.require_sandbox and not self.wrap:
            raise CodexError(
                "codex steps run only inside the srt sandbox, and none was set up for this "
                "step: enable the srt sandbox (agentic init says what to install) to use "
                "codex steps"
            )
        session = asyncio.ensure_future(self._session(task))
        try:
            done, _ = await asyncio.wait({session}, timeout=self.time_limit_s)
        except asyncio.CancelledError:  # the run was stopped: so is everything it started
            self._kill_tree()
            session.cancel()
            try:  # the spend is recorded; the stop wins over a limit it crossed
                self._account_cut("codex was stopped before turn.completed")
            except LimitHit:
                pass
            raise
        if not done:
            self._kill_tree()
            session.cancel()
            try:
                await session
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - it is being stopped
                pass
            await self._reap()
            self._account_cut("codex was killed at the time limit before turn.completed")
            detail = f"the step's time limit ({self.time_limit_s}s) was reached"
            return self._outcome("stopped", self._last_text, stopped="time", detail=detail)
        try:
            return session.result()
        except _Stopped as stop:
            return self._outcome("stopped", self._last_text, stopped=stop.why, detail=stop.detail)

    async def resume(self, state_path, decisions, expected_digest) -> AgentOutcome:
        raise ResumeError("codex steps never pause for approval (approval_policy = never)")

    async def _session(self, task: AgentTask) -> AgentOutcome:
        instructions = _instructions(task, self.contract)
        if self.launch.schema is not None:
            instructions += "\n\n" + DOCUMENT_NOTICE
        text = await self._exec(task.prompt, instructions)
        if self.contract is None:  # silence is no answer (DF-433)
            errors = no_answer(text)
            return self._outcome("invalid" if errors else "completed", text, errors=errors)
        accepted, errors = self.contract.check(text)
        if errors:  # one retry (OUTPUT_RETRIES = 1), in a new exec carrying the answer
            text = await self._exec(_retry_prompt(task.prompt, text, errors), instructions)
            accepted, errors = self.contract.check(text)
        if errors:
            return self._outcome("invalid", text, errors=errors)
        return self._outcome("completed", accepted if accepted is not None else text)

    async def _exec(self, prompt: str, instructions: str) -> str:
        """One `codex exec`. CODEX_HOME's files are written first, every time: a shell
        command in an earlier exec may have changed them (they sit in a writable place)."""
        self.launch.write(instructions)
        argv = [*(self.wrap or []), *self.launch.argv]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.launch.home,
                env=self.launch.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                limit=16 * 1024 * 1024,  # one JSONL event can carry a large tool output
                **spawn_group(),
            )
        except OSError as error:
            raise CodexError(f"the Codex CLI did not start: {error}") from error
        self._process = process
        tree = self._tree = ProcessTree().adopt(process)
        stderr = asyncio.ensure_future(_tail(process.stderr))
        reader = self._pending = EventReader()
        try:
            try:
                process.stdin.write(prompt.encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Codex exited before reading: its exit code and stderr say why
            while line := await process.stdout.readline():
                reader.feed(line.decode("utf-8", errors="replace"))
                if reader.text:
                    self._last_text = unwrap_document(reader.text)
                max_turns = self.agent.limits.max_turns
                if max_turns is not None and self._turns + reader.tool_calls >= max_turns:
                    self._kill_tree()
                    await process.wait()
                    self._account_cut("codex was killed at the turn limit before turn.completed")
                    raise _Stopped("turns", "the agent's turn limit was reached")
            code = await process.wait()
        finally:  # whatever went wrong while reading, nothing is left running
            if process.returncode is None:
                self._kill_tree()
                await self._reap()
            tree.close()  # what its commands left behind goes with the exec
        errors = await stderr
        failed = code != 0 or reader.ended_badly
        if failed:
            self._account_cut("codex ended with an error before turn.completed")
        else:
            self._account(reader)
        text = reader.text or _read(self.launch.last_message)
        if failed:
            last = errors.strip().splitlines()[-1:]
            detail = reader.error or (last[0] if last else f"exit code {code}")
            raise RuntimeError(f"Codex ended the step with an error: {detail}")
        self._last_text = unwrap_document(text)
        return self._last_text

    def _account(self, reader: EventReader, *, cut: str | None = None) -> None:
        """Count this exec's tool calls and charge its model calls, once. Codex reports
        usage only in `turn.completed`: an exec cut short after its turn started (`cut`
        says how) is charged `fallback_estimate_usd` per model call - the calls
        happened, and a step that always hits a limit must not cost nothing (review I3)."""
        if self._pending is reader:
            self._pending = None
        self._turns += reader.tool_calls
        calls = reader.tool_calls + 1
        if reader.usage is not None:
            self._charge(reader.usage, calls=calls)
        elif cut is not None and reader.started:
            self._charge_unreported(calls, cut)

    def _account_cut(self, why: str) -> None:
        """Charge the running exec, if it is not charged yet, as cut short (`why`)."""
        if self._pending is not None:
            self._account(self._pending, cut=why)

    def _charge_unreported(self, calls: int, why: str) -> None:
        """Charge model calls whose usage Codex never reported: the fallback per call."""
        model = self.agent.model
        cost = CallCost("unavailable", None, None, None, None, None, model, None, None, reason=why)
        self.usage.requests += calls
        self._book(cost, self.fallback_estimate_usd * calls)

    def _charge(self, usage: dict, calls: int) -> None:
        """Charge one turn's usage at policy's price (LimitHit past the slice). Responses
        counts include cached input in `input_tokens` and reasoning in `output_tokens`.
        Unpriced, it is `fallback_estimate_usd` for each of the turn's model calls."""
        tokens_in = int(usage.get("input_tokens") or 0)
        read = int(usage.get("cached_input_tokens") or 0)
        write = int(usage.get("cache_write_input_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or 0)
        self.usage.requests += calls
        self.usage.input_tokens += tokens_in
        self.usage.output_tokens += tokens_out
        if tokens_in + tokens_out == 0:
            return
        model = self.agent.model
        price = self.price_for(model)
        if price is None:
            cost = CallCost(
                "unavailable",
                None,
                tokens_in,
                tokens_out,
                read,
                write,
                model,
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
                model,
                None,
                None,
                reason="codex turn usage priced by policy",
            )
        self._book(cost, estimate)

    def _book(self, cost: CallCost, estimate: Decimal) -> None:
        """Charge the budget and log the call (LimitHit past the slice, after both)."""
        if estimate <= 0:
            return
        try:  # the calls already happened: record them, then stop past the slice
            charged = self.budget.charge(cost, estimate)
        except LimitHit:
            self.costs.add(cost, estimate)
            raise
        self.costs.add(cost, charged)

    async def _reap(self) -> None:
        """Wait (briefly) for a killed Codex to exit, so its pipes close in this loop."""
        if self._process is not None:
            try:
                await asyncio.wait_for(self._process.wait(), KILL_WAIT_S)
            except TimeoutError:
                pass

    def _kill_tree(self) -> None:
        """Kill Codex and every process it started (its shell commands may start
        sessions of their own, so the group alone is not enough: the shared helper)."""
        if self._tree is not None:
            self._tree.kill()

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


def _retry_prompt(prompt: str, answer: str, errors: list[str]) -> str:
    """The second exec's prompt: the task again, the refused answer as data, the errors."""
    shown = fence(answer, HANDOFF_TAG, kind="your-previous-answer")
    return f"{prompt}\n\nYour previous answer was refused:\n{shown}\n\n{retry_message(errors)}"


async def _tail(stream: asyncio.StreamReader) -> str:
    """The stream read to its end; only the last MAX_STDERR bytes are kept."""
    kept = bytearray()
    while chunk := await stream.read(65_536):
        kept = (kept + chunk)[-MAX_STDERR:]
    return kept.decode("utf-8", errors="replace")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


class CodexBackend:
    """Builds a locked-down codex runner for each step."""

    name = "codex"

    def __init__(
        self,
        settings: CodexSettings,
        *,
        identity_for: Callable[["StepContext"], CallIdentity],
        prices: Pricer,
        rules_for: Callable[["StepContext"], ShellRules] = lambda step: ShellRules.of(step.agent),
        wrap_for: Callable[["StepContext"], Sequence[str] | None] = lambda step: None,
        # the guard hook (DF-116), given a step and its tree; None: execpolicy alone
        hook_for: Callable[["StepContext", Path], CodexHook | None] = lambda step, tree: None,
        version: Callable[[Sequence[str]], str] = check_version,
        fallback_estimate_usd: Decimal = Decimal("0.01"),  # per call of an unpriced model
    ) -> None:
        self.settings, self.identity_for, self.prices = settings, identity_for, prices
        self.rules_for, self.wrap_for, self.version = rules_for, wrap_for, version
        self.hook_for = hook_for
        self.fallback_estimate_usd = fallback_estimate_usd
        self._checked = False

    def runner(
        self,
        step: "StepContext",
        *,
        tool_context: ToolContext | None,
        state_dir: Path,
        spawner: "ChildSpawner | None",
        is_child: bool = False,
    ) -> CodexRunner:
        if native_windows():
            raise CodexError(
                "the codex backend runs on macOS and Linux; on Windows run agentic inside "
                "WSL2 (agentic init says how) - the native Windows packages are not supported"
            )
        if not self._checked:
            self.version(self.settings.cli)
            self._checked = True
        wrap = self.wrap_for(step)
        cwd = tool_context.tree if tool_context is not None else state_dir
        hook = self.hook_for(step, cwd)
        if hook is None:  # DF-431: the deny rules alone miss reordered or wrapped commands
            _log.warning(
                "codex step %s runs without the guard hook: its deny rules match only commands "
                "that start with the listed words; the srt sandbox is the boundary",
                step.step,
            )
        launch = lock_down(
            step,
            settings=self.settings,
            identity=self.identity_for(step),
            rules=self.rules_for(step),
            cwd=cwd,
            srt_boundary=bool(wrap),
            hook=hook,
        )
        return CodexRunner(
            step.agent,
            launch=launch,
            budget=step.budget,
            price_for=self.prices,
            contract=step.contract,
            time_limit_s=None,
            wrap=wrap,
            require_sandbox=self.settings.require_sandbox,
            fallback_estimate_usd=self.fallback_estimate_usd,
        )
