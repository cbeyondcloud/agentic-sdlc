"""The org policy: what admins allow, held in the policy repo (spec §12).

Agent and pipeline files only *request*; this model is the authority that the
resolver (agentic.policy.resolve, M1-04) checks every request against.
"""

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentic.definitions import ChildLimits, DenyRules, Limits


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typos in policy files are errors


class ModelPrice(_Strict):
    """What one model costs, in USD per million tokens - for client-side cost
    ESTIMATES where TokenLean reports none (live streams, the Anthropic route).
    Kept in line with TokenLean's own pricing table, so both sides estimate alike."""

    input_per_mtok: Decimal = Field(ge=0, allow_inf_nan=False)
    output_per_mtok: Decimal = Field(ge=0, allow_inf_nan=False)
    cache_read_per_mtok: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    cache_write_per_mtok: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)


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


#: Read-denied in every sandbox, whatever policy adds: where credentials live (D38).
DEFAULT_DENY_READ = (
    "~/.ssh",
    "~/.aws",
    "~/.azure",
    "~/.config/gcloud",
    "~/.config/gh",
    "~/.docker",
    "~/.kube",
    "~/.gnupg",
    "~/.netrc",
    "~/.git-credentials",
    "~/.pypirc",
    "~/.npmrc",
    "~/.agentic",
)
_GLOB = set("*?[")


class SandboxPolicy(_Strict):
    """How agent commands are sandboxed (D38, spike S6): `srt` on every OS."""

    # regimes that refuse to run without a sandbox; the others run on guards, with a warning.
    # Team runs too (owner decision, DF-72): guards cannot stop code an allowed program runs.
    required_for: list[Literal["solo", "team", "regulated"]] = ["team", "regulated"]
    allowed_domains: list[str] = []  # package registries a sandboxed command may reach
    deny_read: list[str] = []  # added to DEFAULT_DENY_READ, never instead of it
    allow_write: list[str] = []  # writable besides the run's tree, e.g. "~/.cache/uv"
    srt_version: str = "0.0.78"

    @property
    def effective_deny_read(self) -> list[str]:
        return list(dict.fromkeys([*DEFAULT_DENY_READ, *self.deny_read]))

    @model_validator(mode="after")
    def writes_stay_narrow(self) -> Self:
        problems = []
        for entry in self.allow_write:
            if entry.rstrip("/") in ("", "~"):
                problems.append(f"allow_write entry {entry!r} may not open the root or home")
            elif _GLOB & set(entry):
                problems.append(f"allow_write entry {entry!r} may not be a glob")
            elif any(_inside(entry, denied) for denied in self.effective_deny_read):
                problems.append(f"allow_write entry {entry!r} may not open a read-denied path")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class SecretScanPolicy(_Strict):
    """What happens to a secret in content about to leave for a model (M1-41): the call
    is refused (`block`) or the secret is replaced by a marker (`mask`), and either way
    an audit event is recorded. The same rule applies to tool output (DF-23)."""

    action: Literal["block", "mask"] = "block"
    detectors: list[str] | None = None  # None = the scanner's default set

    @model_validator(mode="after")
    def detectors_are_known(self) -> Self:
        if self.detectors is None:
            return self
        if not self.detectors:
            raise ValueError("detectors may not be empty (that would switch the scan off)")
        from agentic.security.secret_scan import known_detectors  # lazy: no import cycle

        if unknown := sorted(set(self.detectors) - set(known_detectors())):
            raise ValueError(f"unknown detectors {unknown}; known: {known_detectors()}")
        return self


def _inside(path: str, folder: str) -> bool:
    path, folder = path.rstrip("/"), folder.rstrip("/")
    return path == folder or path.startswith(folder + "/")


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
    sandbox: SandboxPolicy = SandboxPolicy()  # D38
    secret_scan: SecretScanPolicy = SecretScanPolicy()  # M1-41
    # what to do when a backend cannot honour a resolved setting (M1-27, spec §9.1)
    unhonoured: dict[Literal["temperature", "effort"], Literal["ignore", "warn", "block"]] = {
        "temperature": "warn",
        "effort": "warn",
    }

    # --- model prices, for estimating costs TokenLean does not report ---
    prices: dict[str, ModelPrice] = {}

    # --- which policy this is (git commit or content hash), recorded on every run ---
    version: str

    def price_for(self, model: str) -> ModelPrice | None:
        """The price of `model`: an exact name wins, otherwise the longest priced name
        contained in it, case-insensitively (TokenLean's rule). Never a default."""
        if model in self.prices:
            return self.prices[model]
        lowered = model.lower()
        matches = [name for name in self.prices if name.lower() in lowered]
        return self.prices[max(matches, key=len)] if matches else None

    @model_validator(mode="before")
    @classmethod
    def unhonoured_defaults(cls, data: object) -> object:
        """A policy naming one setting keeps the default for the other."""
        if isinstance(data, dict) and isinstance(data.get("unhonoured"), dict):
            data = {
                **data,
                "unhonoured": {"temperature": "warn", "effort": "warn", **data["unhonoured"]},
            }
        return data

    @model_validator(mode="after")
    def references_are_consistent(self) -> Self:
        """Every tier, class and tool named anywhere must exist."""
        problems: list[str] = []
        if any(not name.strip() for name in self.prices):
            problems.append(
                "prices: a price needs a model name (an empty one would match every model)"
            )
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
