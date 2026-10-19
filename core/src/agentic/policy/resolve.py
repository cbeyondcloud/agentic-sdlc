"""Resolving an agent: turning what its file *requests* into what policy *allows*.

    AgentDefinition (requests) + its AgentType (template) + OrgPolicy + team -> ResolvedAgent

For every setting:
  1. inherit  - a setting the agent leaves unset comes from its agent type (D35);
  2. ceiling  - a setting the agent does set must stay within its type's ceiling;
  3. policy   - the result must also satisfy org policy, the repo's class and the team.

Two kinds of outcome:
  * refused  - a forbidden request (unknown team or type, tier, model, backend,
               tool, shell command, skill, handoff kind) is a Violation; any
               violation means the agent does not run (preflight stops the run).
  * clamped  - a number above the allowed cap (budget, turns, children...) is
               quietly lowered to the cap. Caps come from the agent TYPE, never
               from the agent's name; a team may only lower them.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from agentic.definitions import AgentDefinition, ChildLimits, DenyRules, Limits
from agentic.policy.model import AgentType, ClassificationRule, OrgPolicy, RoleLimits, TeamPolicy

if TYPE_CHECKING:
    from agentic.handoffs.schema import HandoffCatalog


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
    """What the agent is actually allowed to do on this run (every value final)."""

    model_config = ConfigDict(frozen=True)

    definition: AgentDefinition
    type: str
    classification: str
    tier: str
    model: str
    effort: str | None
    temperature: float | None
    backend: str
    tools: list[str]
    shell: list[str]  # allowed command prefixes, minus any that a deny entry covers
    skills: list[str]
    consumes: list[str]
    produces: str | None
    limits: Limits
    children: ChildLimits
    deny_shell: list[str]  # org + team + agent, in that order (D31)
    deny_paths: list[str]
    ask_shell: list[str] = []  # need a person's approval each time; minus what a deny covers


def resolve_agent(
    agent: AgentDefinition, policy: OrgPolicy, ctx: ResolveContext
) -> tuple[ResolvedAgent | None, list[Violation]]:
    """Check every setting. Returns (ResolvedAgent, []) or (None, violations)."""
    violations: list[Violation] = []

    def refuse(field: str, requested: object, allowed: object, reason: str) -> None:
        violations.append(Violation(field, requested, allowed, reason))

    # --- context first: an unknown team or class must fail closed, never open ---
    team: TeamPolicy | None = None
    if ctx.team is not None:
        team = policy.teams.get(ctx.team)
        if team is None:
            refuse("team", ctx.team, sorted(policy.teams), "the team is not defined in org policy")
    class_name = ctx.repo_classification or policy.default_classification
    rule = policy.classifications.get(class_name)
    if rule is None:
        refuse("classification", class_name, sorted(policy.classifications),
               "the repo's classification is not defined in org policy")  # fmt: skip
    agent_type = policy.agent_types.get(agent.type)
    if agent_type is None:
        refuse("type", agent.type, sorted(policy.agent_types),
               "the agent type is not defined in org policy")  # fmt: skip
    if violations:
        return None, violations
    assert rule is not None and agent_type is not None  # narrowed by the checks above

    # --- tier: inherit from the type; must be allowed for this class and team ---
    tier = agent.tier or agent_type.tier
    allowed_tiers = _allowed_tiers(policy, rule, team)
    if tier not in policy.tiers:
        refuse("tier", tier, sorted(policy.tiers), "unknown tier")
    else:
        if tier not in rule.allowed_tiers:
            refuse("tier", tier, rule.allowed_tiers, f"not allowed for '{class_name}' repos")
        if team and team.allowed_tiers is not None and tier not in team.allowed_tiers:
            refuse("tier", tier, team.allowed_tiers, f"not allowed for team '{ctx.team}'")

    # --- model: from the tier (team override first); an explicit model must be approved ---
    tier_model = _tier_model(policy, tier, ctx.team)
    approved = _approved_models(policy, rule, allowed_tiers, ctx.team)
    if agent.model is not None:
        if agent.model not in approved:
            reason = "not an approved model for this repo and team; ask for a tier instead"
            refuse("model", agent.model, sorted(approved), reason)
        model = agent.model
    else:
        model = tier_model
        if (
            model is not None
            and rule.allowed_models is not None
            and model not in rule.allowed_models
        ):
            refuse("model", model, rule.allowed_models, f"not allowed for '{class_name}' repos")

    # --- backend: inherit the type's first; within the type and the repo class ---
    backend = agent.backend or agent_type.backends[0]
    if backend not in agent_type.backends:
        refuse("backend", backend, agent_type.backends, f"beyond the '{agent.type}' type")
    elif backend not in rule.allowed_backends:
        refuse("backend", backend, rule.allowed_backends, f"not allowed for '{class_name}' repos")

    # --- tools: inherit; each in the catalog, not forbidden, within the type ---
    tools = agent.tools if agent.tools is not None else list(agent_type.tools)
    for tool in tools:
        if tool not in policy.tools_catalog:
            refuse("tools", tool, policy.tools_catalog, "not in the org tool catalog")
        elif tool in policy.forbidden_tools:
            refuse("tools", tool, "not " + ", ".join(policy.forbidden_tools),
                   "forbidden by org policy")  # fmt: skip
        elif tool not in agent_type.tools:
            refuse("tools", tool, agent_type.tools, f"beyond the '{agent.type}' type")

    # --- shell: inherit; each entry within one of the type's command prefixes ---
    shell = agent.shell if agent.shell is not None else list(agent_type.shell)
    for command in shell:
        if not any(_covers(prefix, command) for prefix in agent_type.shell):
            refuse("shell", command, agent_type.shell, f"beyond the '{agent.type}' type")

    # --- skills and handoffs: inherit; within the type ---
    skills = agent.skills if agent.skills is not None else list(agent_type.skills)
    for skill in skills:
        if skill not in agent_type.skills:
            refuse("skills", skill, agent_type.skills, f"beyond the '{agent.type}' type")
    consumes = agent.consumes if agent.consumes is not None else list(agent_type.consumes)
    for kind in consumes:
        if kind not in agent_type.consumes:
            refuse("consumes", kind, agent_type.consumes, f"beyond the '{agent.type}' type")
    produces = agent.produces or agent_type.produces
    if agent_type.produces and produces != agent_type.produces:
        refuse("produces", produces, agent_type.produces,
               f"the '{agent.type}' type always produces '{agent_type.produces}'")  # fmt: skip

    if violations:
        return None, violations

    # --- deny lists: union of org, team and agent (D31) ---
    deny = _merge_deny(policy.deny, team.deny if team else None, agent.deny)
    team_cap = team.limits_max.get(agent.type) if team else None

    # Tier defaults (effort, temperature) belong to the tier's model; they are not
    # applied to an explicitly named different model.
    tier_spec = policy.team_overrides.get(ctx.team or "", {}).get(tier) or policy.tiers[tier]
    defaults_apply = agent.model is None or agent.model == tier_model

    resolved = ResolvedAgent(
        definition=agent,
        type=agent.type,
        classification=class_name,
        tier=tier,
        model=model,  # type: ignore[arg-type]
        effort=agent.effort or (tier_spec.effort if defaults_apply else None),
        temperature=agent.temperature
        if agent.temperature is not None
        else (tier_spec.temperature if defaults_apply else None),
        backend=backend,
        tools=tools,
        shell=[cmd for cmd in shell if not any(_covers(d, cmd) for d in deny.shell)],
        skills=skills,
        consumes=consumes,
        produces=produces,
        limits=_clamp_limits(agent.limits, agent_type, team_cap),
        children=_clamp_children(agent.children, agent_type, team_cap),
        deny_shell=deny.shell,
        deny_paths=deny.paths,
        ask_shell=[cmd for cmd in deny.ask if not any(_covers(d, cmd) for d in deny.shell)],
    )
    return resolved, []


# --- helpers ---------------------------------------------------------------


def _allowed_tiers(
    policy: OrgPolicy, rule: ClassificationRule, team: TeamPolicy | None
) -> list[str]:
    """Tiers allowed for this repo class AND this team."""
    tiers = [t for t in rule.allowed_tiers if t in policy.tiers]
    if team and team.allowed_tiers is not None:
        tiers = [t for t in tiers if t in team.allowed_tiers]
    return tiers


def _tier_model(policy: OrgPolicy, tier: str, team_name: str | None) -> str | None:
    """The real model behind a tier, using this team's override (never another team's)."""
    override = policy.team_overrides.get(team_name or "", {}).get(tier)
    if override:
        return override.model
    spec = policy.tiers.get(tier)
    return spec.model if spec else None


def _approved_models(
    policy: OrgPolicy, rule: ClassificationRule, allowed_tiers: list[str], team_name: str | None
) -> set[str]:
    """Models an agent may name explicitly (review finding 1).

    The class's own list when it has one; otherwise only the models behind the
    tiers this repo class and this team may use (with this team's overrides).
    """
    if rule.allowed_models is not None:
        return set(rule.allowed_models)
    return {m for t in allowed_tiers if (m := _tier_model(policy, t, team_name))}


def _covers(prefix: str, command: str) -> bool:
    """True if `command` is the command prefix itself or a narrower form of it.

    "git push" covers "git push" and "git push origin main", but not "git"
    (allowing "git" while denying "git push" is a valid, narrower combination).
    Compared word by word: a prefix word may hold alternatives ("git push|fetch",
    the guard's rule syntax), and the first word is a program name, so "GIT.exe"
    is "git" (DF-04).
    """
    wanted, words = prefix.split(), command.split()
    if not wanted or len(words) < len(wanted):
        return False
    for position, (alternatives, word) in enumerate(zip(wanted, words, strict=False)):
        options = alternatives.split("|")
        if position == 0:
            options, word = [_program_name(o) for o in options], _program_name(word)
        if word not in options:
            return False
    return True


_PROGRAM_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1", ".sh")


def _program_name(word: str) -> str:
    """A program word as the shell finds it: base name, lower case, no Windows suffix."""
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in _PROGRAM_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _merge_deny(*layers: DenyRules | None) -> DenyRules:
    shell: list[str] = []
    paths: list[str] = []
    ask: list[str] = []
    for layer in layers:
        if layer is None:
            continue
        shell += [cmd for cmd in layer.shell if cmd not in shell]
        paths += [glob for glob in layer.paths if glob not in paths]
        ask += [cmd for cmd in layer.ask if cmd not in ask]
    return DenyRules(shell=shell, paths=paths, ask=ask)


def _smallest(*values: float | int | None) -> float | int | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _clamp_one(requested: Limits, type_cap: object, team_cap: Limits | None) -> Limits:
    """Each field = min(request, type cap, team cap). The type cap is always set (D35)."""
    return Limits(**{
        name: _smallest(getattr(requested, name), getattr(type_cap, name),
                        getattr(team_cap, name) if team_cap else None)
        for name in Limits.model_fields
    })  # fmt: skip


def _clamp_limits(requested: Limits, agent_type: AgentType, team_cap: RoleLimits | None) -> Limits:
    return _clamp_one(requested, agent_type.limits.max, team_cap.max if team_cap else None)


def _clamp_children(
    requested: ChildLimits, agent_type: AgentType, team_cap: RoleLimits | None
) -> ChildLimits:
    type_children = agent_type.limits.children_max
    team_children = team_cap.children_max if team_cap else None
    counts = {
        name: _smallest(getattr(requested, name), getattr(type_children, name),
                        getattr(team_children, name) if team_children else None)
        for name in ("max_total", "max_concurrent", "max_depth")
    }  # fmt: skip
    per_child = _clamp_one(
        requested.per_child,
        type_children.per_child,
        team_children.per_child if team_children else None,
    )
    return ChildLimits(**counts, per_child=per_child)


def kind_violations(agent: ResolvedAgent, catalog: "HandoffCatalog") -> list[Violation]:
    """Handoff kinds the resolved agent produces or consumes that the run's catalog
    lacks - including kinds inherited from its agent type (D37). Preflight calls it."""
    known = catalog.kinds()
    wanted = [("produces", agent.produces)] + [("consumes", kind) for kind in agent.consumes]
    return [
        Violation(field_name, kind, known, "unknown handoff kind")
        for field_name, kind in wanted
        if kind is not None and kind not in catalog.schemas
    ]
