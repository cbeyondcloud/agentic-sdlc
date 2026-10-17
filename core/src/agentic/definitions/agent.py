"""The agent definition: a Markdown file whose YAML header holds settings and
whose body holds the agent's plain-English instructions (spec §5.1).

Settings such as tier, tools and limits are only *requests* here; org policy
decides what is actually allowed (see agentic.policy.resolve).
"""

from pathlib import Path
from typing import Literal

import frontmatter
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agentic.definitions.errors import DefinitionError

# Backends an agent may ask to run on (spec §9.1).
BACKEND_PATTERN = r"^(native|claude-code|codex|acp:.+|remote:.+)$"


class Limits(BaseModel):
    """Upper bounds for one agent run. None means "use the policy default"."""

    model_config = ConfigDict(extra="forbid")

    budget_usd: float | None = None
    max_turns: int | None = None
    max_minutes: int | None = None


class ChildLimits(BaseModel):
    """How many child agents an agent may spawn, and the limits each child gets."""

    model_config = ConfigDict(extra="forbid")

    max_total: int = 0  # 0 = this agent may not spawn children
    max_concurrent: int = 1
    max_depth: int = 1
    per_child: Limits = Limits()


class AgentDefinition(BaseModel):
    """Everything an agent file declares. Unknown fields are rejected (typo safety)."""

    model_config = ConfigDict(extra="forbid")

    # --- identity ---
    name: str
    version: str
    owner: str | None = None
    status: Literal["active", "deprecated"] = "active"
    description: str

    # --- model request (admin policy maps tiers to real models) ---
    tier: str | None = "balanced"
    model: str | None = None
    effort: Literal["low", "medium", "high"] | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)

    # --- where and how it runs ---
    backend: str = Field(default="native", pattern=BACKEND_PATTERN)
    tools: list[str] = []
    shell: list[str] = []
    skills: list[str] = []

    # --- handoffs: which documents it reads and which one it writes ---
    consumes: list[str] = []
    produces: str | None = None

    # --- limits (clamped by policy) ---
    limits: Limits = Limits()
    children: ChildLimits = ChildLimits()

    # --- the Markdown body: what the agent is supposed to do, in plain English ---
    instructions: str

    @field_validator("instructions")
    @classmethod
    def instructions_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("the Markdown body (the agent's instructions) is empty")
        return value.strip()


def load_agent(path: Path) -> AgentDefinition:
    """Read an agent file and return a validated AgentDefinition.

    Raises DefinitionError with one readable line per problem.
    """
    try:
        document = frontmatter.load(path)
    except yaml.YAMLError as error:
        raise DefinitionError(path, [f"YAML header is not valid: {error}"]) from error

    data = {**document.metadata, "instructions": document.content}
    try:
        return AgentDefinition.model_validate(data)
    except ValidationError as error:
        problems = [
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        ]
        raise DefinitionError(path, problems) from error
