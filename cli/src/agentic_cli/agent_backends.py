"""The agent-CLI backends in the CLI runtime (M2-60, decision 89): `claude-code`, `codex` and
`opencode`, each built locked down beside `native`.

What every agent-CLI step gets from the CLI:

  * the user's TokenLean key and the run's identity headers (`Runtime.identity`) on every
    model call the agent CLI makes, on the step's resolved model;
  * the guard's shell policy with the framework floor, in the agent's own format:
    claude-code's permission rules (`Guard.claude_permissions`, don't-ask mode); codex's
    execpolicy rules plus the guard itself as a PreToolUse hook
    (`sdlc_commons.codex_hook`, so unlisted commands are refused - `HONOURED`); OpenCode's
    `bash` and edit rules (`Guard.shell_rules`);
  * its process wrap (`StepWiring.process_wrap_for`): srt, reaching TokenLean and the
    policy's registries and no other host (M2-42), writing only the run's tree, the
    policy's caches and the step's own home - never codex's config, rules, catalog or the
    guard hook's spec (DF-425), never the guard's protected paths;
  * the kill switch at each turn the backend reports; a pause holds the run at the next
    step boundary (decision 89) and the engine's watchdog still kills a step at once;
  * after the step, a protected-path check of what it changed (`ProtectedWrites`): an
    agent CLI's writes, its shell commands' included, never pass the file tools' check,
    and srt enforces literal paths only (DF-443).

Sandbox, by backend (the safe default):

  * `codex` runs only inside srt - no unsandboxed codex (decision 22); on native Windows
    it is refused (run agentic inside WSL2);
  * `claude-code` and `opencode` follow the regime's sandbox decision (D38): the whole
    process wrapped where srt is used; where the regime lets steps run on guards only,
    unwrapped under their compiled permission rules, with the same preflight warning a
    native step gets.

Optional extras stay optional: a step on a backend this machine cannot run fails preflight,
before any spend, with what to install (`missing`).

Where each agent CLI is found, when not the default (user config `~/.agentic/config.yaml`):

    agent_clis:
      codex: /usr/local/bin/codex                  # default: `codex` on PATH
      opencode: C:/Tools/opencode/opencode.exe     # default: `opencode` on PATH
      claude-code: /opt/claude/claude              # default: the binary in the SDK wheel

A value is a program or a list (a program and its first arguments). On Windows, name
OpenCode's signed `opencode.exe`: npm's `opencode.cmd` shim is refused (cmd.exe re-parses
its arguments), and OpenCode's bash tool needs Git for Windows' bash
(`OPENCODE_GIT_BASH_PATH`, passed through to it).
"""

import importlib.util
import os
import subprocess
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from agentic.agents.models import GatewaySettings
from agentic.agents.runner import AgentOutcome, AgentRunner, AgentTask
from agentic.backends import OPTIONAL
from agentic.backends.acp import OPENCODE
from agentic.backends.acp import ShellRules as AcpShellRules
from agentic.backends.base import Backend
from agentic.backends.codex_hooks import CodexHook
from agentic.backends.codex_rules import ShellRules as CodexShellRules
from agentic.engine.engine import StepContext
from agentic.engine.preflight import PreflightReport, Problem
from agentic.policy import OrgPolicy
from agentic.sandbox.readiness import SandboxDecision
from agentic.transport import CallIdentity
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.wiring import StepWiring
from sdlc_commons.codex_hook import hook_argv, write_spec
from sdlc_commons.guards import Guard

#: The agent-CLI backends this CLI builds, and every backend it configures.
AGENT_CLIS: tuple[str, ...] = ("claude-code", "codex", "opencode")
CONFIGURED_BACKENDS: tuple[str, ...] = ("native", *AGENT_CLIS)
#: Backends that never run outside srt (decision 22).
NEEDS_SRT = frozenset({"codex"})
#: Capability settings the wiring makes good (`preflight(honoured=...)`): the guard hook
#: gives codex the shell's default-deny (DF-426).
HONOURED: dict[str, frozenset[str]] = {"codex": frozenset({"shell_default_deny"})}
#: The user config key naming where each agent CLI is.
CONFIG_KEY = "agent_clis"
STATE = Path(".agentic") / "local" / "state"
_INSTALL_EXTRA = (
    "install the {extra} extra of agentic-core into agentic's environment: "
    '`uv tool install agentic-cli --with "agentic-core[{extra}]"` '
    '(or `pip install "agentic-core[{extra}]"`)'
)
#: What one agent CLI's codex home holds that its shell commands must never rewrite.
_CODEX_OWN_FILES = ("config.toml", "rules", "models.json", "schema.json")


def native_windows() -> bool:
    """Windows itself, not WSL2 (tests replace it)."""
    return os.name == "nt"


# ── where each agent CLI is ───────────────────────────────────────────────────


def cli_command(settings: Mapping[str, Any], backend: str) -> tuple[str, ...] | None:
    """The user config's command for `backend` (`agent_clis`), or None for the default.
    CliError (exit 3) when the setting is not a program or a list of words."""
    section = settings.get(CONFIG_KEY)
    if section is None:
        return None
    if not isinstance(section, Mapping):
        raise CliError(
            f"{CONFIG_KEY} in the user config must map a backend to its program",
            ExitCode.INVALID,
            hint=f"e.g. {CONFIG_KEY}: {{codex: /usr/local/bin/codex}}",
        )
    value = section.get(backend)
    if value is None:
        return None
    words = [value] if isinstance(value, str) else value
    if not isinstance(words, list) or not words or not all(isinstance(w, str) and w for w in words):
        raise CliError(
            f"{CONFIG_KEY}.{backend} in the user config must be a program, or a list of words",
            ExitCode.INVALID,
            hint=f"e.g. {CONFIG_KEY}: {{{backend}: /path/to/its/executable}}",
        )
    return tuple(words)


# ── what this machine lacks ───────────────────────────────────────────────────


def missing(backend: str, settings: Mapping[str, Any]) -> str | None:
    """What this machine lacks to run `backend`, said as what to install; None when it
    can run it. Checks the extra, the agent CLI and (codex) its pinned version."""
    if backend not in AGENT_CLIS:
        return None
    if backend == "codex" and native_windows():
        return (
            "the codex backend runs on macOS and Linux; on Windows run agentic inside WSL2 "
            "(agentic init says how)"
        )
    if importlib.util.find_spec(OPTIONAL[backend]) is None:
        return _INSTALL_EXTRA.format(extra=backend)
    command = cli_command(settings, backend)
    if backend == "claude-code":
        return _claude_code_missing(command)
    if backend == "codex":
        return _codex_missing(command or ("codex",))
    return _opencode_missing(command)


def _claude_code_missing(command: tuple[str, ...] | None) -> str | None:
    from agentic.backends.claude_code import bundled_cli

    path = Path(command[0]) if command else bundled_cli()
    if path.is_file():
        return None
    if command:
        return f"{CONFIG_KEY}.claude-code names {path}, which does not exist"
    return (
        "the installed claude-agent-sdk has no Claude Code binary for this platform: "
        + _INSTALL_EXTRA.format(extra="claude-code")
    )


def _codex_missing(command: tuple[str, ...]) -> str | None:
    from agentic.backends.codex import CodexError, check_version

    try:
        check_version(command)
    except CodexError as error:
        return str(error)
    except (OSError, subprocess.SubprocessError) as error:  # a broken install
        return f"the Codex CLI did not answer ({error})"
    return None


def _opencode_missing(command: tuple[str, ...] | None) -> str | None:
    from agentic.backends.acp.runner import resolve_command

    try:
        resolve_command(command or OPENCODE.command)
    except RuntimeError as error:
        return (
            f"{error} - install OpenCode {OPENCODE.version} (`npm install -g "
            f"opencode-ai@{OPENCODE.version}`; on Windows set {CONFIG_KEY}.opencode in "
            "~/.agentic/config.yaml to the path of its opencode.exe)"
        )
    return None


# ── preflight ─────────────────────────────────────────────────────────────────


def problems(
    report: PreflightReport,
    decision: SandboxDecision,
    *,
    repo: Path,
    settings: Mapping[str, Any],
) -> list[Problem]:
    """Why an agent-CLI step of this pipeline cannot run here, before any spend: a missing
    extra or agent CLI (with what to install), codex without srt, a sandbox wrap on native
    Windows (DF-81), and OpenCode plugins in the repository (DF-446) - each an error."""
    users: dict[str, list[str]] = {}
    for name, agent in sorted(report.resolved.items()):
        if agent.backend in AGENT_CLIS:
            users.setdefault(agent.backend, []).append(name)
    found = []
    for backend, agents in sorted(users.items()):
        who = f"agent{'s' if len(agents) > 1 else ''} {', '.join(agents)}"
        why = _cannot_run(backend, decision, settings)
        if why:
            found.append(Problem(None, "backend", f"{who} run on {backend}, but {why}", backend))
        elif backend == "opencode" and OPENCODE.refuse_tree is not None:
            for reason in OPENCODE.refuse_tree(repo):
                detail = (
                    f"{who} run on opencode, which would run code from this repository "
                    f"whatever its config says: {reason} (remove it from the branch, or run "
                    "the step on another backend)"
                )
                found.append(Problem(None, "backend", detail, backend))
    return found


def _cannot_run(backend: str, decision: SandboxDecision, settings: Mapping[str, Any]) -> str | None:
    if decision.refusal:  # the sandbox check refuses the run already
        return None
    if why := missing(backend, settings):
        return f"it cannot run on this machine: {why}"
    if backend in NEEDS_SRT and not decision.use_srt:
        return (
            f"{backend} steps run only inside the srt sandbox, and this run has none "
            "(agentic init says what to install; on Windows, run agentic inside WSL2)"
        )
    if decision.use_srt and native_windows():
        return "a sandboxed agent CLI process needs macOS, Linux or WSL2 (run agentic in WSL2)"
    return None


# ── the post-step check ───────────────────────────────────────────────────────


class ProtectedWriteError(RuntimeError):
    """An agent-CLI step changed paths its guard refuses: its work is not kept."""

    def __init__(self, backend: str, refused: list[str]) -> None:
        shown = "; ".join(refused[:5])
        more = f" and {len(refused) - 5} more" if len(refused) > 5 else ""
        super().__init__(
            f"the {backend} agent wrote paths it may not write, so its work is not kept: "
            f"{shown}{more}"
        )
        self.refused = refused


class ProtectedWrites:
    """A step's runner whose answer is kept only when what the step changed passes the
    guard (`StepWiring.denied_writes`). A refused change raises `ProtectedWriteError`
    before the engine checkpoints the step: the step fails, a person decides, and a retry
    starts from the step's base."""

    def __init__(self, runner: AgentRunner, check: Callable[[], list[str]], backend: str):
        self.runner, self.check, self.backend = runner, check, backend

    async def run(self, task: AgentTask) -> AgentOutcome:
        return self._checked(await self.runner.run(task))

    async def resume(self, state_path, decisions, expected_digest) -> AgentOutcome:
        return self._checked(await self.runner.resume(state_path, decisions, expected_digest))

    def _checked(self, outcome: AgentOutcome) -> AgentOutcome:
        if outcome.status in ("completed", "invalid"):  # what the engine would checkpoint
            refused = self.check()
            if refused:
                raise ProtectedWriteError(self.backend, refused)
        return outcome


class ProtectedBackend:
    """An agent-CLI backend whose every runner is wrapped in `ProtectedWrites`."""

    def __init__(self, inner: Backend, wiring: StepWiring) -> None:
        self.inner, self.wiring, self.name = inner, wiring, inner.name

    def runner(self, step: StepContext, **kwargs) -> AgentRunner:
        return self.wrap(self.inner.runner(step, **kwargs), step)

    def wrap(self, runner: AgentRunner, step: StepContext) -> ProtectedWrites:
        return ProtectedWrites(runner, lambda: self.wiring.denied_writes(step), self.name)


# ── building them ─────────────────────────────────────────────────────────────


def build(
    *,
    repo: Path,
    policy: OrgPolicy,
    settings: Mapping[str, Any],
    gateway: GatewaySettings,
    wiring: StepWiring,
    identity_for: Callable[[StepContext], CallIdentity],
    fallback_estimate_usd: Decimal,
) -> dict[str, ProtectedBackend]:
    """Every agent-CLI backend, locked down for this workspace (see the module text).
    Building one imports no optional extra: a backend this machine cannot run is refused
    by preflight (`problems`), never here."""
    common = {
        "identity_for": identity_for,
        "prices": policy.price_for,
        "fallback_estimate_usd": fallback_estimate_usd,
    }
    state = repo / STATE
    pass_env = tuple(policy.sandbox.pass_env)
    commands = {name: cli_command(settings, name) for name in AGENT_CLIS}
    inner = (
        _claude_code(state, gateway, wiring, commands["claude-code"], pass_env, common),
        _codex(state, gateway, wiring, commands["codex"], pass_env, common),
        _opencode(state, gateway, wiring, commands["opencode"], pass_env, common),
    )
    return {backend.name: ProtectedBackend(backend, wiring) for backend in inner}


def _guard(wiring: StepWiring, step: StepContext) -> Guard:
    return Guard.for_agent(wiring.target, step.run_id, step.agent)


def _step_home(root: Path, step: StepContext) -> Path:
    """A backend's per-step folder, as each backend lays it out under its root."""
    return root / step.run_id / f"{step.step}-{step.iteration}" / step.agent.definition.name


def _claude_code(state, gateway, wiring, command, pass_env, common):
    from agentic.backends.claude_code import ClaudeCodeBackend, ClaudeCodeSettings

    settings = ClaudeCodeSettings(
        gateway_url=gateway.base_url,
        api_key=gateway.api_key,
        config_root=state / "claude-code",
        cli_path=Path(command[0]) if command else None,
        pass_env=pass_env,
    )

    def wrap_for(step: StepContext) -> list[str] | None:
        if not wiring.decision.use_srt:  # the regime runs on guards: its own rules only
            return None
        return wiring.process_wrap_for(step, write=(_step_home(settings.config_root, step),))

    return ClaudeCodeBackend(
        settings,
        permissions_for=lambda step: _guard(wiring, step).claude_permissions(),
        wrap_for=wrap_for,
        **common,
    )


def _codex(state, gateway, wiring, command, pass_env, common):
    from agentic.backends.codex import CodexBackend, CodexSettings

    settings = CodexSettings(
        gateway_url=gateway.base_url,
        api_key=gateway.api_key,
        home_root=state / "codex",
        cli=command or ("codex",),
        pass_env=pass_env,
    )

    def rules_for(step: StepContext) -> CodexShellRules:
        rules = _guard(wiring, step).shell_rules()
        allow = tuple(rule for rule in rules["allow"] if rule != "*")
        # Zyroflo's destructive floor as prefixes too; the hook judges it anywhere
        deny = tuple(dict.fromkeys((*rules["deny"], *rules["deny_anywhere"])))
        return CodexShellRules(allow=allow, ask=rules["ask"], deny=deny)

    def hook_for(step: StepContext, tree: Path) -> CodexHook:
        spec = _hooks(state, step) / f"{step.step}-{step.iteration}-{step.agent.definition.name}"
        argv = hook_argv(write_spec(spec.with_suffix(".json"), _guard(wiring, step), tree))
        if not Path(argv[0]).is_file():  # Codex runs a call whose hook cannot start (DF-429)
            raise RuntimeError(
                f"the codex guard hook's interpreter {argv[0]} does not exist, and Codex would "
                "run every command unchecked without it: reinstall agentic"
            )
        return CodexHook(argv=argv)

    def wrap_for(step: StepContext) -> list[str]:
        home = _step_home(settings.home_root, step).resolve()  # as codex.lock_down has it
        own = tuple(home / name for name in _CODEX_OWN_FILES)
        return wiring.process_wrap_for(step, write=(home,), deny=(*own, _hooks(state, step)))

    return CodexBackend(
        settings, rules_for=rules_for, hook_for=hook_for, wrap_for=wrap_for, **common
    )


def _hooks(state: Path, step: StepContext) -> Path:
    """Where a run's codex guard hook specs are written: outside its tree and every
    agent home, and denied to the sandbox."""
    return state / step.run_id / "hooks"


def _opencode(state, gateway, wiring, command, pass_env, common):
    from agentic.backends.acp.runner import AcpBackend, AcpSettings

    settings = AcpSettings(
        gateway_url=gateway.base_url,
        api_key=gateway.api_key,
        home_root=state / "opencode",
        command=(*command, *OPENCODE.command[1:]) if command else None,
        pass_env=pass_env,
    )

    def wrap_for(step: StepContext) -> list[str] | None:
        if not wiring.decision.use_srt:  # the regime runs on guards: its own rules only
            return None
        return wiring.process_wrap_for(step, write=(_step_home(settings.home_root, step),))

    return AcpBackend(
        OPENCODE,
        settings,
        rules_for=lambda step: AcpShellRules(**_guard(wiring, step).shell_rules()),
        wrap_for=wrap_for,
        **common,
    )
