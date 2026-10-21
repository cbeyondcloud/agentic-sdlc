"""The org policy: what admins allow, held in the policy repo (spec §12).

Agent and pipeline files only *request*; this model is the authority that the
resolver (agentic.policy.resolve, M1-04) checks every request against.
"""

import re
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
    # the levels an agent file may ask for: a typo is refused here, not at a model call
    effort: Literal["low", "medium", "high"] | None = None
    temperature: float | None = None
    # The model's family for the eval judge rule (spike S10), when its id's prefix before
    # '/' does not say it (a gateway alias, a team's own deployment).
    family: str | None = None


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
    skill_packs: list[str] = []  # opt-in skill packs from the org catalog (spec §11.2)


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


class EvalPolicy(_Strict):
    """Agent evaluation defaults (M1-63, spike S10); an A/B plan may set its own."""

    repeats: int = Field(default=3, ge=1)  # runs per case per variant
    # A/B guard: median cost per run B/A may rise by at most this fraction
    max_cost_increase: float = Field(default=0.25, ge=0)
    # Spend cap per eval or A/B in USD; at the cap the run stops as INCOMPLETE
    budget_usd: float = Field(default=100, gt=0)
    budget_usd_by_agent: dict[str, float] = {}  # e.g. a costlier QA or Developer agent

    def budget_for(self, agent: str) -> float:
        return self.budget_usd_by_agent.get(agent, self.budget_usd)


class SecretScanPolicy(_Strict):
    """What happens to a secret in content about to leave for a model (M1-41): the call
    is refused (`block`) or the secret is replaced by a marker (`mask`), and either way
    an audit event is recorded. The same rule applies to tool output (DF-23)."""

    action: Literal["block", "mask"] = "block"
    detectors: list[str] | None = None  # None = the scanner's default set
    # values never treated as secrets (test fixtures, documented examples); each at
    # least 8 characters. Vendors' documented example keys are always allowed.
    allow_values: list[str] = []

    @model_validator(mode="after")
    def allowed_values_are_specific(self) -> Self:
        from agentic.security.secret_scan import MIN_ALLOWED  # lazy: no import cycle

        if any(len(value) < MIN_ALLOWED for value in self.allow_values):
            raise ValueError(
                f"allow_values must each be at least {MIN_ALLOWED} characters - a shorter "
                "one would let real secrets through"
            )
        return self

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


#: `<os>-<arch>` keys of a tool's per-platform artefacts (Zyroflo's third_party platforms).
PlatformKey = Literal[
    "windows-x86_64", "linux-x86_64", "linux-aarch64", "darwin-x86_64", "darwin-aarch64"
]
#: A tool name, version or binary: each becomes one folder or file name in the tool cache,
#: so never '.', '..', a separator, a drive or anything else that could leave it.
PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


def _plain_name_problem(where: str, value: str) -> str | None:
    if PLAIN_NAME.fullmatch(value):
        return None
    return f"{where} {value!r} must be a plain name (letters, digits, '.', '_', '+', '-')"


class ToolArtefact(_Strict):
    """One downloadable file of a pinned tool, for one platform, on the org mirror."""

    file: str  # relative to OrgPolicy.tool_mirror_url
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The org checked the publisher's code signature of these exact bytes. Required for a
    # Windows artefact: an unsigned native binary is never installed on Windows.
    signed: bool = False

    @model_validator(mode="after")
    def stays_under_the_mirror(self) -> Self:
        parts = self.file.replace("\\", "/").split("/")
        if ":" in self.file or self.file.startswith(("/", "\\")) or ".." in parts:
            raise ValueError(
                f"artefact file {self.file!r} must be a relative path under the tool mirror "
                "(no scheme, no leading '/', no '..')"
            )
        return self


class ToolPin(_Strict):
    """A user-space tool `agentic init` fetches, pinned by version and sha256 (spec §24)."""

    version: str
    binary: str  # the base name; Windows adds .exe
    # always: every developer init; licence_scan: only when the org scans licences;
    # on_request: only with `agentic init --install-missing`, after asking.
    when: Literal["always", "licence_scan", "on_request"] = "always"
    assets: dict[PlatformKey, ToolArtefact] = Field(min_length=1)

    @model_validator(mode="after")
    def binary_is_a_base_name(self) -> Self:
        if self.binary.lower().endswith(".exe") or "/" in self.binary or "\\" in self.binary:
            raise ValueError(f"binary {self.binary!r} must be a base name (Windows adds .exe)")
        problems = [
            _plain_name_problem("version", self.version),
            _plain_name_problem("binary", self.binary),
        ]
        if found := [problem for problem in problems if problem]:
            raise ValueError("; ".join(found))
        return self


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
    tool_pins: dict[str, ToolPin] = {}  # tools `init` fetches from the mirror (M1-51)
    allow_repo_pipelines: bool = False  # D33: pipelines only from the policy repo
    # D39: `init --repo` adds the nightly index export workflow only when this is on
    index_export_workflow: bool = False
    sandbox: SandboxPolicy = SandboxPolicy()  # D38
    secret_scan: SecretScanPolicy = SecretScanPolicy()  # M1-41
    eval: EvalPolicy = EvalPolicy()  # M1-63
    # what to do when a backend cannot honour a resolved setting (M1-27, spec §9.1)
    # (`secret_scan`: the backend calls the model itself, past the framework's scanner)
    unhonoured: dict[
        Literal["temperature", "effort", "secret_scan"], Literal["ignore", "warn", "block"]
    ] = {"temperature": "warn", "effort": "warn", "secret_scan": "warn"}

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
        """A policy naming some settings keeps the defaults for the others."""
        if isinstance(data, dict) and isinstance(data.get("unhonoured"), dict):
            data = {
                **data,
                "unhonoured": {
                    "temperature": "warn",
                    "effort": "warn",
                    "secret_scan": "warn",
                    **data["unhonoured"],
                },
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
        if self.tool_pins and not self.tool_mirror_url:
            problems.append("tool pins need tool_mirror_url (the org mirror they come from)")
        for name in self.tool_pins:
            if problem := _plain_name_problem("tool pin name", name):
                problems.append(problem)

        if problems:
            raise ValueError("; ".join(problems))
        return self
