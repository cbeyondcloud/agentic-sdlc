"""The org policy: what admins allow, held in the policy repo (spec §12).

Agent and pipeline files only *request*; this model is the authority that the
resolver (agentic.policy.resolve, M1-04) checks every request against.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

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


class RoleLimits(_Strict):
    """Upper bounds for one agent role. Fields left as None impose no cap here."""

    max: Limits = Limits()
    children_max: ChildLimits | None = None  # None = no narrowing of child agents


class TeamPolicy(_Strict):
    """Team narrowing: may only tighten org policy (spec §12.2)."""

    allowed_tiers: list[str] | None = None  # None = every tier the org allows
    monthly_budget_usd: float | None = None
    limits_max: dict[str, RoleLimits] = {}  # keyed by agent name
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

    # --- limits (keyed by agent name; default_role_limits for any other agent) ---
    role_limits: dict[str, RoleLimits] = {}
    default_role_limits: RoleLimits
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
