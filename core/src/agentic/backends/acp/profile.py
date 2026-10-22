"""What an ACP agent profile is (M2-57, spike S7 decision 2).

ACP standardises the conversation (initialize, session, prompt, streamed tool calls,
permission requests) but not how an agent is pointed at TokenLean, locked down or
priced. That differs per agent, so it is a profile, held as data: the launch command
and pinned version, the auth method and session mode, the egress switches, the hosts
the agent is known to try besides the gateway (the sandbox blocks them), the user
variables it may inherit, and `configure` - our routing, model and shell policy
compiled into the agent's own config. The engine (`runner.py`) knows no agent by name:
a new ACP agent is a new `AcpProfile` plus its pinned contract tests (E2-06).

Every agent runs with a home of its own per step: no user config, credentials or
history of the developer's is ever read, and the agent's own state is thrown away
with the step.
"""

import itertools
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from agentic.policy import ResolvedAgent
from agentic.policy.model import ModelPrice
from agentic.sandbox.process import SECRET_NAME, secret_value


@dataclass(frozen=True)
class ShellRules:
    """Our shell policy, provider-neutral: command prefixes allowed, needing a person's
    approval, and refused (deny wins), and the path globs never written. A rule's word
    may list alternatives (`git rebase -x|--exec`). `sdlc_commons.guards.Guard.shell_rules`
    gives the full set as plain data (framework floor included); `from_agent` is the
    agent's resolved lists."""

    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    deny_paths: tuple[str, ...] = ()

    @classmethod
    def from_agent(cls, agent: ResolvedAgent) -> "ShellRules":
        return cls(
            allow=tuple(agent.shell),
            ask=tuple(agent.ask_shell),
            deny=tuple(agent.deny_shell),
            deny_paths=tuple(agent.deny_paths),
        )


def spelled_out(rule: str) -> list[str]:
    """A rule's `a|b` alternatives as separate prefixes, in order."""
    words = [word.split("|") for word in rule.split()]
    return [" ".join(combination) for combination in itertools.product(*words)]


@dataclass(frozen=True)
class StepSetup:
    """What a profile compiles for one step."""

    agent: ResolvedAgent
    gateway_url: str  # TokenLean's root
    api_key: str = field(repr=False)  # the user's TokenLean key
    headers: Mapping[str, str] = field(default_factory=dict)  # our X-Agentic-* identity
    rules: ShellRules = field(default_factory=ShellRules)
    price: ModelPrice | None = None  # policy's price for the agent's model
    home: Path = Path()  # the step's own home folder
    tree: Path = Path()  # the step's working tree


@dataclass(frozen=True)
class Launch:
    """A profile's output for one step: environment variables and files for its home."""

    env: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[str, str] = field(default_factory=dict)  # home-relative POSIX path -> text


@dataclass(frozen=True)
class AcpProfile:
    name: str  # the backend name agents ask for, e.g. "opencode"
    command: tuple[str, ...]  # the agent's ACP entry point
    version: str  # the agent release the contract tests pin
    configure: Callable[[StepSetup], Launch]  # routing, model and policy -> agent config
    auth_method: str | None = None  # ACP `authenticate` method id, when one is needed
    mode: str | None = None  # ACP session mode to set, when the profile needs one
    quiet: Mapping[str, str] = field(default_factory=dict)  # egress and update switches
    egress_hosts: tuple[str, ...] = ()  # hosts it tries besides the gateway; srt blocks them
    inherit: tuple[str, ...] = ()  # the user's variables it may see (CA bundle, shell path)
    # A server of its own that it calls over HTTP: (the flag that sets its port, its host).
    # The runner gives it a free port and has exactly that host:port skip any proxy - a
    # gateway elsewhere on loopback still goes through the sandbox's proxy.
    own_server: tuple[str, str] | None = None
    # The agent sums every model call at the prices `configure` wrote (policy's) and
    # reports the session's cost: the step is charged from that.
    session_cost: bool = False
    # What in a tree the agent would run as code whatever its config says (one reason
    # per file): the agent is never started in such a tree - fail closed.
    refuse_tree: Callable[[Path], list[str]] | None = None


#: The variables that name a home or config folder, on every OS.
_HOME_VARS = ("HOME", "USERPROFILE")
_HOME_FOLDERS = {
    "APPDATA": "AppData/Roaming",
    "LOCALAPPDATA": "AppData/Local",
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_STATE_HOME": ".local/state",
    "XDG_CACHE_HOME": ".cache",
}


def home_env(home: Path) -> dict[str, str]:
    """Every home and config folder variable, pointing inside `home`."""
    env = {name: str(home) for name in _HOME_VARS}
    env.update({name: str(home / sub) for name, sub in _HOME_FOLDERS.items()})
    return env


def launch_env(
    profile: AcpProfile,
    launch: Launch,
    home: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The agent's environment on top of the ACP SDK's minimal one (PATH and system
    basics): the step home, the inherited variables the profile names (never one with a
    secret name or a credential in its value), its quiet switches, then the launch."""
    source = os.environ if environ is None else environ
    env = home_env(home)
    for name in profile.inherit:
        value = source.get(name)
        if value and not SECRET_NAME.search(name) and not secret_value(value):
            env[name] = value
    env.update(profile.quiet)
    env.update(launch.env)
    return env


def write_files(home: Path, files: Mapping[str, str]) -> None:
    """Write a profile's files under `home`; a path that would land outside is refused."""
    for name, text in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or Path(name).is_absolute():
            raise ValueError(f"profile file {name!r} is outside the step home")
        target = home.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
