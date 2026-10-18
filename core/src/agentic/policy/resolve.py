"""Resolving an agent: turning what its file *requests* into what policy *allows*.

    AgentDefinition (requests)  +  OrgPolicy (+ team narrowing)  ->  ResolvedAgent or violations

Two kinds of outcome per setting:
  * refused  - a forbidden request (tool, tier, backend, model) is a Violation;
               any violation means the agent does not run (preflight stops the run).
  * clamped  - a number above the allowed maximum (budget, turns, children...)
               is quietly lowered to the maximum; that is not a violation.
"""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from agentic.definitions import AgentDefinition, ChildLimits, DenyRules, Limits
from agentic.policy.model import OrgPolicy, RoleLimits, TierSpec


@dataclass(frozen=True)
class ResolveContext:
    """Where the agent will run: which team, and the repo's sensitivity class."""

    team: str | None = None
    repo_classification: str | None = None  # None = the org's default class


@dataclass(frozen=True)
class Violation:
    """One request that policy does not allow, with a reason a person can act on."""

    field: str
    requested: object
    allowed: object
    reason: str


class ResolvedAgent(BaseModel):
    """What the agent is actually allowed to do on this run."""

    model_config = ConfigDict(frozen=True)

    definition: AgentDefinition
    classification: str
    tier: str | None
    model: str
    effort: str | None
    temperature: float | None
    backend: str
    tools: list[str]
    shell: list[str]  # allowed command prefixes, minus any that a deny entry covers
    skills: list[str]
    limits: Limits
    children: ChildLimits
    deny_shell: list[str]  # org + team + agent, in that order (D31)
    deny_paths: list[str]


def resolve_agent(
    agent: AgentDefinition, policy: OrgPolicy, ctx: ResolveContext
) -> tuple[ResolvedAgent | None, list[Violation]]:
    """Check every restricted setting. Returns (ResolvedAgent, []) or (None, violations)."""
    violations: list[Violation] = []
    team = policy.teams.get(ctx.team) if ctx.team else None

    # --- repo classification ---
    class_name = ctx.repo_classification or policy.default_classification
    rule = policy.classifications.get(class_name)
    if rule is None:
        reason = "the repo's classification is not defined in org policy"
        known = sorted(policy.classifications)
        violations.append(Violation("classification", class_name, known, reason))
        return None, violations

    # --- tier: must exist, be allowed for this repo class, and for this team ---
    tier_spec: TierSpec | None = None
    if agent.tier is not None:
        if agent.tier not in policy.tiers:
            violations.append(Violation("tier", agent.tier, sorted(policy.tiers),
                                        "unknown tier"))  # fmt: skip
        else:
            tier_spec = (
                policy.team_overrides.get(ctx.team or "", {}).get(agent.tier)
                or (policy.tiers[agent.tier])
            )
            if agent.tier not in rule.allowed_tiers:
                violations.append(Violation("tier", agent.tier, rule.allowed_tiers,
                                            f"not allowed for '{class_name}' repos"))  # fmt: skip
            if team and team.allowed_tiers is not None and agent.tier not in team.allowed_tiers:
                violations.append(Violation("tier", agent.tier, team.allowed_tiers,
                                            f"not allowed for team '{ctx.team}'"))  # fmt: skip

    # --- model: an explicit model must be approved; the final model must suit the repo ---
    approved = _approved_models(policy, rule.allowed_models)
    if agent.model is not None and agent.model not in approved:
        violations.append(Violation("model", agent.model, sorted(approved),
                                    "not an approved model; ask for a tier instead"))  # fmt: skip
    model = agent.model or (tier_spec.model if tier_spec else None)
    if model is None and agent.tier is None:
        violations.append(Violation("model", None, sorted(policy.tiers),
                                    "the agent names neither a tier nor a model"))  # fmt: skip
    elif model is not None and rule.allowed_models is not None and model not in rule.allowed_models:
        violations.append(Violation("model", model, rule.allowed_models,
                                    f"not allowed for '{class_name}' repos"))  # fmt: skip

    # --- backend ---
    if agent.backend not in rule.allowed_backends:
        violations.append(Violation("backend", agent.backend, rule.allowed_backends,
                                    f"not allowed for '{class_name}' repos"))  # fmt: skip

    # --- tools: must be in the catalog and not forbidden ---
    for tool in agent.tools:
        if tool not in policy.tools_catalog:
            violations.append(Violation("tools", tool, policy.tools_catalog,
                                        "not in the org tool catalog"))  # fmt: skip
        elif tool in policy.forbidden_tools:
            violations.append(Violation("tools", tool, "not " + ", ".join(policy.forbidden_tools),
                                        "forbidden by org policy"))  # fmt: skip

    if violations:
        return None, violations

    # --- deny lists: union of org, team and agent (D31) ---
    deny = _merge_deny(policy.deny, team.deny if team else None, agent.deny)

    # --- limits: min(requested, org role cap, team cap); missing request = the cap ---
    org_cap = policy.role_limits.get(agent.name, policy.default_role_limits)
    team_cap = team.limits_max.get(agent.name) if team else None

    resolved = ResolvedAgent(
        definition=agent,
        classification=class_name,
        tier=agent.tier,
        model=model,
        effort=agent.effort or (tier_spec.effort if tier_spec else None),
        temperature=agent.temperature
        if agent.temperature is not None
        else (tier_spec.temperature if tier_spec else None),
        backend=agent.backend,
        tools=list(agent.tools),
        shell=[cmd for cmd in agent.shell if not any(_covers(d, cmd) for d in deny.shell)],
        skills=list(agent.skills),
        limits=_clamp_limits(agent.limits, org_cap, policy.default_role_limits, team_cap),
        children=_clamp_children(agent.children, org_cap, policy.default_role_limits, team_cap),
        deny_shell=deny.shell,
        deny_paths=deny.paths,
    )
    return resolved, []


# --- helpers ---------------------------------------------------------------


def _approved_models(policy: OrgPolicy, class_models: list[str] | None) -> set[str]:
    """Models an agent may name explicitly: the class list, else what the tiers map to."""
    if class_models is not None:
        return set(class_models)
    models = {spec.model for spec in policy.tiers.values()}
    for overrides in policy.team_overrides.values():
        models |= {spec.model for spec in overrides.values()}
    return models


def _covers(deny_prefix: str, allowed: str) -> bool:
    """True if a denied command prefix makes an allowed entry pointless.

    "git push" covers "git push" and "git push origin main", but not "git"
    (allowing "git" while denying "git push" is a valid, narrower combination).
    """
    return allowed == deny_prefix or allowed.startswith(deny_prefix + " ")


def _merge_deny(*layers: DenyRules | None) -> DenyRules:
    shell: list[str] = []
    paths: list[str] = []
    for layer in layers:
        if layer is None:
            continue
        shell += [cmd for cmd in layer.shell if cmd not in shell]
        paths += [glob for glob in layer.paths if glob not in paths]
    return DenyRules(shell=shell, paths=paths)


def _smallest(*values: float | int | None) -> float | int | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _clamp_one(requested: Limits, caps: list[Limits | None]) -> Limits:
    clamped = {}
    for name in Limits.model_fields:
        cap = _smallest(*(getattr(c, name) for c in caps if c is not None))
        wanted = getattr(requested, name)
        clamped[name] = cap if wanted is None else _smallest(wanted, cap)
    return Limits(**clamped)


def _clamp_limits(
    requested: Limits, org_cap: RoleLimits, default: RoleLimits, team_cap: RoleLimits | None
) -> Limits:
    # A field the role cap leaves unset falls back to the org default for that field.
    org = Limits(**{
        name: getattr(org_cap.max, name) if getattr(org_cap.max, name) is not None
        else getattr(default.max, name)
        for name in Limits.model_fields
    })  # fmt: skip
    return _clamp_one(requested, [org, team_cap.max if team_cap else None])


def _clamp_children(
    requested: ChildLimits, org_cap: RoleLimits, default: RoleLimits, team_cap: RoleLimits | None
) -> ChildLimits:
    caps = [org_cap.children_max or default.children_max,
            team_cap.children_max if team_cap else None]  # fmt: skip
    present = [cap for cap in caps if cap is not None]
    counts = {
        name: _smallest(getattr(requested, name), *(getattr(cap, name) for cap in present))
        for name in ("max_total", "max_concurrent", "max_depth")
    }
    per_child = _clamp_one(requested.per_child, [cap.per_child for cap in present])
    return ChildLimits(**counts, per_child=per_child)
