"""What each backend honours, and what org policy does when it cannot (M1-27, spec §9.1).

A resolved agent asks for a backend, tool capabilities and sampling settings. Not
every backend can honour all of them: Claude Code never sends a temperature and has
no child agents; Pydantic AI drops temperature for reasoning models (DF-25). Before
any spend, `backend_issues` names each gap. A missing backend or tool always blocks;
an unhonoured setting follows `OrgPolicy.unhonoured` (ignore, warn or block).

The matrix is data: vendor-specific tool names live here as values only, so the
engine, policy and protocols stay provider-agnostic.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Literal

from agentic.policy import OrgPolicy, ResolvedAgent

Action = Literal["ignore", "warn", "block"]

#: Our tool capabilities, as policy names them (agentic.agents.tools).
TOOL_CAPABILITIES = (
    "read_files",
    "search",
    "edit_files",
    "run_shell",
    "codeintel",
    "activate_skill",
    "spawn_agent",
)
#: Model names Pydantic AI sends no temperature to: reasoning models (DF-25).
REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


@dataclass(frozen=True)
class BackendCapabilities:
    name: str
    tools: Mapping[str, tuple[str, ...]]  # our capability -> the backend's tool names
    temperature: bool
    effort: bool
    approvals: bool  # can pause for a person's approval of a tool call
    children: bool  # can spawn child agents
    stuck_check: bool
    # its model calls go through agentic.transport, so the secret scan (M1-41) sees them
    secret_scan: bool = True
    notes: tuple[str, ...] = field(default=())


CAPABILITIES: dict[str, BackendCapabilities] = {
    "native": BackendCapabilities(
        name="native",
        tools={name: (name,) for name in TOOL_CAPABILITIES},
        temperature=True,
        effort=True,
        approvals=True,
        children=True,
        stuck_check=True,
    ),
    "claude-code": BackendCapabilities(
        name="claude-code",
        # what Claude Code offers in --bare mode: Read, Edit, Bash (review I5) - no
        # Grep/Glob/Write, so `search` is unavailable there and preflight blocks it
        tools={"read_files": ("Read",), "edit_files": ("Edit",), "run_shell": ("Bash",)},
        temperature=False,
        effort=True,
        approvals=False,
        children=False,
        stuck_check=False,
        # Claude Code calls the model itself; only TokenLean sees its egress (DF-149)
        secret_scan=False,
        notes=("spike S2",),
    ),
    "opencode": BackendCapabilities(
        name="opencode",
        # OpenCode's own permission keys (1.18.35): the ACP profile sets every key and
        # denies those no capability maps to, which also hides their tools (M2-57, S7)
        tools={
            "read_files": ("read", "list"),
            "search": ("glob", "grep"),
            "edit_files": ("edit",),
            "run_shell": ("bash",),
        },
        temperature=True,  # set on OpenCode's agent; reasoning models still drop it
        effort=False,
        approvals=False,  # the ACP permission callback refuses; no pause yet
        children=False,
        stuck_check=False,
        secret_scan=False,  # OpenCode calls TokenLean itself, past agentic.transport
        notes=("spike S7", "ACP profile, OpenCode 1.18.35"),
    ),
}


@dataclass(frozen=True)
class Issue:
    setting: str  # backend | tools | ask | temperature | effort | secret_scan
    action: Action
    detail: str


def backend_issues(
    agent: ResolvedAgent, policy: OrgPolicy, *, available: Collection[str]
) -> list[Issue]:
    """Every way `agent`'s backend falls short of what the agent asks for."""
    caps = CAPABILITIES.get(agent.backend)
    if caps is None or agent.backend not in available:
        why = f"backend {agent.backend!r} is not available here (available: {sorted(available)})"
        return [Issue("backend", "block", why)]
    found: list[Issue] = []
    missing = [t for t in agent.tools if t not in caps.tools]
    if missing:
        found.append(Issue("tools", "block", f"{agent.backend} cannot offer {missing}"))
    if agent.ask_shell and not caps.approvals:
        found.append(
            Issue(
                "ask",
                "warn",
                f"ask-listed commands are refused (no approval pause on {agent.backend})",
            )
        )
    if agent.temperature is not None and not _temperature_honoured(agent, caps):
        found += _unhonoured(policy, "temperature", f"{agent.backend} / {agent.model}")
    if agent.effort is not None and not caps.effort:
        found += _unhonoured(policy, "effort", agent.backend)
    if not caps.secret_scan:
        found += _unhonoured(
            policy, "secret_scan", f"{agent.backend} (it sends model calls itself, unscanned)"
        )
    return found


def _temperature_honoured(agent: ResolvedAgent, caps: BackendCapabilities) -> bool:
    if not caps.temperature:
        return False
    name = agent.model.partition("/")[2] or agent.model
    return not name.lower().startswith(REASONING_MODEL_PREFIXES)


def _unhonoured(policy: OrgPolicy, setting: str, where: str) -> list[Issue]:
    action = policy.unhonoured.get(setting, "warn")
    if action == "ignore":
        return []
    return [Issue(setting, action, f"{setting} is not honoured by {where}")]
