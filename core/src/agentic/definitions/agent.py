"""The agent definition: a Markdown file whose YAML header holds settings and
whose body holds the agent's plain-English instructions (spec §5.1).

Settings such as tier, tools and limits are only *requests* here; org policy
decides what is actually allowed (see agentic.policy.resolve).
"""

import re
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from agentic.definitions.errors import DefinitionError
from agentic.yamlio import YamlFileError, load_yaml, read_text, split_front_matter

# Backends an agent may ask to run on (spec §9.1, D32).
#   native / claude-code / codex / opencode -> dedicated adapters with tested lock-down profiles
#   acp:<agent>     -> any Agent Client Protocol agent via one generic adapter (acp:antigravity)
#   remote:<service> -> hosted agents such as Devin (submit / poll / collect)
BACKEND_PATTERN = r"^(native|claude-code|codex|opencode|acp:.+|remote:.+)$"
# An agent's name: lower-case letters, digits and dashes (DF-07), starting with a letter
# - the run ledger records `<name>@<version>` only in that shape (review F1).
AGENT_NAME = r"^[a-z][a-z0-9-]{0,63}$"
# Names Windows maps to devices in every folder, with or without an extension: a file
# named after one cannot be created there (DF-324, DF-180).
WINDOWS_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{n}" for n in range(1, 10)]
    + [f"LPT{n}" for n in range(1, 10)]
)


def windows_device_name(name: str) -> bool:
    """Whether `name`, used as a file name, opens a Windows device (`nul`, `com1.md`)."""
    return name.split(".", 1)[0].rstrip(" ").upper() in WINDOWS_DEVICE_NAMES


def valid_agent_name(name: str) -> bool:
    return re.fullmatch(AGENT_NAME, name) is not None and not windows_device_name(name)


class Limits(BaseModel):
    """Upper bounds for one agent run. None means "not set here: use the type's cap"."""

    model_config = ConfigDict(extra="forbid")

    budget_usd: float | None = None
    max_turns: int | None = None
    max_minutes: int | None = None


class ChildLimits(BaseModel):
    """How many child agents an agent may spawn, and the limits each child gets.

    None means "not set here: use the agent type's cap" (D35). 0 means none allowed.
    """

    model_config = ConfigDict(extra="forbid")

    max_total: int | None = None
    max_concurrent: int | None = None
    max_depth: int | None = None
    per_child: Limits = Limits()


class DenyRules(BaseModel):
    """What this agent may never do, even if org policy or its own allow-lists permit it.

    Deny always beats allow. When the agent runs, these are combined (union) with
    the org's and team's deny lists, and child agents inherit them (D31).
    """

    model_config = ConfigDict(extra="forbid")

    shell: list[str] = []  # command prefixes, e.g. "git push" also blocks "git push --force"
    paths: list[str] = []  # file globs the agent may never write, e.g. "infra/**"
    # command prefixes that need a person's approval on every use, e.g. "npm publish";
    # a restriction too, so merged the same way (forbid beats ask, ask beats allow)
    ask: list[str] = []


class AgentDefinition(BaseModel):
    """Everything an agent file declares. Unknown fields are rejected (typo safety).

    Every agent has an org-defined `type` (D35). A setting left as None is not set
    here and is inherited from the type; a setting given here may only narrow the
    type's ceiling. The resolver (agentic.policy.resolve) applies both rules.
    """

    model_config = ConfigDict(extra="forbid")

    # --- identity ---
    # a file name (state-<agent>.json, children/<agent>), a header and a ledger field:
    # lower case, so it is one file on case-insensitive disks too (Windows, macOS)
    name: str
    type: str = Field(pattern=r"^[a-z][a-z0-9-]*$")  # an agent type defined in org policy
    version: str
    owner: str | None = None
    status: Literal["active", "deprecated"] = "active"
    description: str

    # --- model request (admin policy maps tiers to real models) ---
    tier: str | None = None
    model: str | None = None
    effort: Literal["low", "medium", "high"] | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)

    # --- where and how it runs ---
    backend: str | None = Field(default=None, pattern=BACKEND_PATTERN)
    tools: list[str] | None = None
    shell: list[str] | None = None
    skills: list[str] | None = None

    # --- handoffs: which documents it reads and which one it writes ---
    consumes: list[str] | None = None
    produces: str | None = None

    # --- limits (clamped by policy) ---
    limits: Limits = Limits()
    children: ChildLimits = ChildLimits()

    # --- guardrails: what this agent may never do (D31) ---
    deny: DenyRules = DenyRules()

    # --- the Markdown body: what the agent is supposed to do, in plain English ---
    instructions: str

    @field_validator("name")
    @classmethod
    def name_is_a_file_name(cls, value: str) -> str:
        if re.fullmatch(AGENT_NAME, value) is None:
            raise ValueError(
                "use lower-case letters, digits and dashes, starting with a letter, "
                "at most 64 characters - the name is also a file name"
            )
        if windows_device_name(value):
            raise ValueError(f"{value!r} is a Windows device name, so no file can carry it")
        return value

    @field_validator("instructions")
    @classmethod
    def instructions_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("the Markdown body (the agent's instructions) is empty")
        return value.strip()

    @model_validator(mode="after")
    def allow_and_deny_do_not_contradict(self) -> "AgentDefinition":
        # Allowing "git" while denying "git push" is fine (deny narrows the allow).
        # Listing the very same command in both is a mistake the author should fix.
        contradictions = sorted(set(self.shell or []) & set(self.deny.shell))
        if contradictions:
            raise ValueError(
                f"shell commands are both allowed and denied: {', '.join(contradictions)}"
            )
        return self


def load_agent(path: Path) -> AgentDefinition:
    """Read an agent file and return a validated AgentDefinition.

    Raises DefinitionError with one readable line per problem.
    """
    try:
        header, body = split_front_matter(read_text(path))
        meta, lines = load_yaml(header, first_line=2)  # line 1 is the opening '---'
    except YamlFileError as error:
        raise DefinitionError(path, [str(error)]) from error
    if not isinstance(meta, dict):
        raise DefinitionError(path, ["the YAML header must contain key: value settings"])

    try:
        return AgentDefinition.model_validate({**meta, "instructions": body})
    except ValidationError as error:
        raise DefinitionError.from_validation_error(path, error, "agent", lines=lines) from error
