"""The org policy: what admins allow, held in the policy repo (spec §12).

Agent and pipeline files only *request*; this model is the authority that the
resolver (agentic.policy.resolve, M1-04) checks every request against.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentic.definitions import ChildLimits, DenyRules, Limits


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typos in policy files are errors


class TierSpec(_Strict):
    """What one capability tier means: the real model plus default settings."""

    model: str
    effort: str | None = None
    temperature: float | None = None


class ClassificationRule(_Strict):
    """What a repo of this sensitivity class may use."""

    allowed_tiers: list[str]
    allowed_models: list[str] | None = None  # None = any model the tiers map to
    allowed_backends: list[str]
    trace_capture: bool = False  # may full prompts/responses be stored centrally?


class CapLimits(_Strict):
    """A complete per-run cap: every field is required, so nothing is unlimited by omission."""

    budget_usd: float = Field(ge=0)
    max_turns: int = Field(ge=0)
    max_minutes: int = Field(ge=0)


class CapChildLimits(_Strict):
    """A complete cap on child agents (0 = this type may not spawn children)."""

    max_total: int = Field(ge=0)
    max_concurrent: int = Field(ge=0)
    max_depth: int = Field(ge=0)
    per_child: CapLimits


class TypeCaps(_Strict):
    max: CapLimits
    children_max: CapChildLimits


class AgentType(_Strict):
    """The org-approved template for one kind of agent (D35).

    Agents of this type inherit every setting they leave out, and anything they
    set must stay within these ceilings. Caps here are the org maximum; a team
    may lower them but never raise them.
    """

    description: str
    tier: str  # default tier
    backends: list[str] = Field(min_length=1)  # allowed; the first is the default
    tools: list[str] = []  # allowed and default
    shell: list[str] = []  # allowed command prefixes and default
    skills: list[str] = []  # allowed and default
    consumes: list[str] = []  # allowed handoff kinds and default
    produces: str | None = None  # fixed handoff kind for this type
    limits: TypeCaps


class RoleLimits(_Strict):
    """A team's narrowing of one agent type's caps. A field left out keeps the type cap."""

    max: Limits = Limits()
    children_max: ChildLimits | None = None


class TeamPolicy(_Strict):
    """Team narrowing: may only tighten org policy (spec §12.2)."""

    allowed_tiers: list[str] | None = None  # None = every tier the org allows
    monthly_budget_usd: float | None = None
    limits_max: dict[str, RoleLimits] = {}  # keyed by agent TYPE (D35)
    deny: DenyRules = DenyRules()


class OrgPolicy(_Strict):
    # --- models ---
    tiers: dict[str, TierSpec]
    team_overrides: dict[str, dict[str, TierSpec]] = {}  # team -> tier -> different model

    # --- repo classes ---
    classifications: dict[str, ClassificationRule]
    default_classification: str

    # --- tools and guardrails ---
    tools_catalog: list[str] = []
    forbidden_tools: list[str] = []
    deny: DenyRules = DenyRules()  # org-wide; merged with team and agent denies (D31)

    # --- agent types (D35): caps, ceilings and defaults per kind of agent ---
    agent_types: dict[str, AgentType] = Field(min_length=1)
    concurrency: dict[str, int] = {}

    # --- licences and retention ---
    blocked_licences: list[str] = []
    licence_scan: bool = False
    retention_days: dict[str, int] = {}  # placeholder until OR-01

    # --- teams ---
    teams: dict[str, TeamPolicy] = {}

    # --- org-wide settings ---
    allowed_repo_patterns: list[str] = []  # empty = any repo (standalone use only)
    tool_mirror_url: str | None = None
    allow_repo_pipelines: bool = False  # D33: pipelines only from the policy repo

    # --- which policy this is (git commit or content hash), recorded on every run ---
    version: str

    @model_validator(mode="after")
    def references_are_consistent(self) -> Self:
        """Every tier, class and tool named anywhere must exist."""
        problems: list[str] = []
        known_tiers = set(self.tiers)

        def check_tiers(where: str, names: list[str] | None) -> None:
            for name in names or []:
                if name not in known_tiers:
                    problems.append(f"{where} names unknown tier '{name}'")

        for team, overrides in self.team_overrides.items():
            check_tiers(f"team_overrides.{team}", list(overrides))
        for cls_name, rule in self.classifications.items():
            check_tiers(f"classifications.{cls_name}", rule.allowed_tiers)
        for team, team_policy in self.teams.items():
            check_tiers(f"teams.{team}.allowed_tiers", team_policy.allowed_tiers)
            for type_name in team_policy.limits_max:
                if type_name not in self.agent_types:
                    problems.append(
                        f"teams.{team}.limits_max names unknown agent type '{type_name}'"
                    )
        for team in self.team_overrides:
            if team not in self.teams:
                problems.append(f"team_overrides names unknown team '{team}'")
        for type_name, agent_type in self.agent_types.items():
            check_tiers(f"agent_types.{type_name}", [agent_type.tier])
            for tool in agent_type.tools:
                if tool not in self.tools_catalog:
                    problems.append(
                        f"agent_types.{type_name} names tool '{tool}' not in the catalog"
                    )

        if self.default_classification not in self.classifications:
            problems.append(
                f"default_classification '{self.default_classification}' "
                "is not one of the classifications"
            )
        for tool in self.forbidden_tools:
            if tool not in self.tools_catalog:
                problems.append(f"forbidden tool '{tool}' is not in the tool catalog")

        if problems:
            raise ValueError("; ".join(problems))
        return self
