"""What one tenant's accepted policy asks of the gateway (M2-09), as TokenLean's routing
overlay expresses it (`/admin/routing`, ST-10). Pure: the same policy always gives the same
plan, and `canonical` the same bytes.

Policy tiers have free names (`balanced`, `deep-reasoning`); TokenLean's `G6_routing.tiers`
are its own complexity levels. So tiers are mapped with G6 `rules` matching the headers the
CLI sends (spec §5.3: "the request still carries `X-Agentic-Tier`, and TokenLean routing
rules remain the enforcement point"), each pinning the gateway's name for the policy model
with `allow_escalation` on, so the policy's model is served rather than the one asked for:

  400  classification + team + tier  a team's own model, where the class allows it
  350  classification + team         the team's fallback in that class
  300  classification + tier         the org model, where the class allows it
  250  classification                the class's fallback: its first allowed model
  200  team + tier                   `team_overrides`
  100  tier                          `tiers`

Class rules exist only for a restrictive class (one that leaves a tier out or names
`allowed_models`), so a request of that class asking for a disallowed tier, or none, still
gets an allowed model. Team rules match `X-Agentic-Team` and sit in the tenant's list: the
operator-level `tenants.<id>.groups` overlay replaces a team overlay's `rules` wholesale.

A team's `monthly_budget_usd` becomes `teams.<team>.monthly_spend_cap_usd`, and its
`rate_limit` the entry for `rate_limit.per_team.<team>` (written only where the deployment
allows: that section is keyed by team name across every tenant)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal

from agentic.policy import ClassificationRule, OrgPolicy, gateway_model

#: Bumped whenever the plan for an unchanged policy would change: a stored plan of another
#: format is generated again from the version's bundle.
GENERATOR_FORMAT = 1
TIER_PARAM = "x_agentic_tier"
CLASS_PARAM = "x_agentic_classification"
TEAM_PARAM = "x_agentic_team"
#: TokenLean's rule id pattern.
RULE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
PRIORITY = {
    "class-team-tier": 400,
    "class-team": 350,
    "class-tier": 300,
    "class": 250,
    "team-tier": 200,
    "tier": 100,
}


@dataclass(frozen=True)
class RoutingPlan:
    """`tenant`: the subtree for `tenants.<gateway tenant>`; `per_team`: team -> request
    limits for `rate_limit.per_team`; `warnings`: what the gateway cannot be told."""

    tenant: dict
    per_team: dict[str, dict] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_settings(self) -> dict:
        return {
            "format": GENERATOR_FORMAT,
            "tenant": self.tenant,
            "per_team": self.per_team,
            "warnings": self.warnings,
        }

    @classmethod
    def from_settings(cls, stored: object) -> RoutingPlan | None:
        """The plan stored with a version, or None when absent or of another format."""
        if not isinstance(stored, dict) or stored.get("format") != GENERATOR_FORMAT:
            return None
        return cls(
            tenant=dict(stored.get("tenant") or {}),
            per_team=dict(stored.get("per_team") or {}),
            warnings=list(stored.get("warnings") or []),
        )


def canonical(value: object) -> bytes:
    """The one serialisation of a plan: sorted keys, no spaces, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def plan_digest(plan: RoutingPlan) -> str:
    """`sha256:` of what is sent: the tenant subtree and the team limits."""
    body = canonical({"tenant": plan.tenant, "per_team": plan.per_team})
    return "sha256:" + hashlib.sha256(body).hexdigest()


def routing_plan(policy: OrgPolicy) -> RoutingPlan:
    warnings: list[str] = []
    rules = _rules(policy, warnings)
    tenant: dict = {"groups": {"G6_routing": {"rules": rules}}}
    teams = _team_caps(policy, warnings)
    if teams:
        tenant["teams"] = teams
    per_team = {
        team: team_policy.rate_limit.model_dump(exclude_none=True)
        for team, team_policy in sorted(policy.teams.items())
        if team_policy.rate_limit is not None
    }
    return RoutingPlan(tenant=tenant, per_team=per_team, warnings=warnings)


# ── rules ────────────────────────────────────────────────────────────────────


def _models(policy: OrgPolicy, team: str | None) -> dict[str, str]:
    """tier -> model id for `team` (the org's tiers with the team's overrides)."""
    models = {tier: spec.model for tier, spec in policy.tiers.items()}
    if team is not None:
        models.update({tier: spec.model for tier, spec in policy.team_overrides[team].items()})
    return models


def _allowed(rule: ClassificationRule, models: dict[str, str]) -> list[tuple[str, str]]:
    """(tier, model id) a class allows, in the class's own order."""
    allowed = []
    for tier in dict.fromkeys(rule.allowed_tiers):
        model = models.get(tier)
        if model is not None and (rule.allowed_models is None or model in rule.allowed_models):
            allowed.append((tier, model))
    return allowed


def _restrictive(rule: ClassificationRule, policy: OrgPolicy) -> bool:
    return bool(set(policy.tiers) - set(rule.allowed_tiers)) or rule.allowed_models is not None


def _rule(kind: str, names: dict[str, str], model: str) -> dict:
    params = {}
    if "class" in names:
        params[CLASS_PARAM] = [names["class"]]
    if "team" in names:
        params[TEAM_PARAM] = [names["team"]]
    if "tier" in names:
        params[TIER_PARAM] = [names["tier"]]
    label = ", ".join(f"{key} {value}" for key, value in names.items())
    return {
        "id": _rule_id(names),
        "description": f"agentic policy: {label}"[:200],
        "priority": PRIORITY[kind],
        "match": {"params": params},
        "action": {"model": gateway_model(model), "allow_escalation": True},
    }


def _rule_id(names: dict[str, str]) -> str:
    """`agentic.class.<c>.team.<t>.tier.<x>` (the parts present), or a digest of it when
    that does not fit TokenLean's pattern."""
    readable = ".".join(["agentic", *(f"{key}.{value}" for key, value in names.items())])
    if RULE_ID.fullmatch(readable):
        return readable
    return "agentic." + hashlib.sha256(readable.encode("utf-8")).hexdigest()[:16]


def _rules(policy: OrgPolicy, warnings: list[str]) -> list[dict]:
    rules: list[dict] = []
    org = _models(policy, None)
    override_teams = sorted(team for team, tiers in policy.team_overrides.items() if tiers)
    for name, rule in sorted(policy.classifications.items()):
        if not _restrictive(rule, policy):
            continue
        org_allowed = _allowed(rule, org)
        if not org_allowed:
            warnings.append(
                f"classification {name} allows no tier model, so the gateway cannot keep its "
                "requests on an allowed model (the CLI still refuses them)"
            )
        org_models = dict(org_allowed)
        org_fallback = org_allowed[0][1] if org_allowed else None
        for team in override_teams:
            team_allowed = _allowed(rule, _models(policy, team))
            for tier, model in team_allowed:
                if org_models.get(tier) != model:
                    names = {"class": name, "team": team, "tier": tier}
                    rules.append(_rule("class-team-tier", names, model))
            if team_allowed and team_allowed[0][1] != org_fallback:
                names = {"class": name, "team": team}
                rules.append(_rule("class-team", names, team_allowed[0][1]))
        for tier, model in org_allowed:
            rules.append(_rule("class-tier", {"class": name, "tier": tier}, model))
        if org_fallback is not None:
            rules.append(_rule("class", {"class": name}, org_fallback))
    for team in override_teams:
        for tier, spec in sorted(policy.team_overrides[team].items()):
            rules.append(_rule("team-tier", {"team": team, "tier": tier}, spec.model))
    for tier, spec in sorted(policy.tiers.items()):
        rules.append(_rule("tier", {"tier": tier}, spec.model))
    return sorted(rules, key=lambda rule: -rule["priority"])  # stable: authoring order kept


# ── team caps ────────────────────────────────────────────────────────────────


def _money(where: str, value: float) -> int | float:
    """A budget as a JSON number written the way the policy wrote it (2000, 512.5);
    ValueError for infinity or NaN, which no JSON number holds (review F1)."""
    if not math.isfinite(value):
        raise ValueError(f"{where} is {value}: a spend cap must be a finite number")
    exact = Decimal(str(value))
    return int(exact) if exact == exact.to_integral_value() else float(exact)


def _team_caps(policy: OrgPolicy, warnings: list[str]) -> dict[str, dict]:
    teams = {}
    for team, team_policy in sorted(policy.teams.items()):
        budget = team_policy.monthly_budget_usd
        if budget is None:
            continue
        if budget <= 0:
            warnings.append(
                f"teams.{team}.monthly_budget_usd is {budget}: the gateway reads 0 as no cap "
                "and refuses every request under a negative one, so no team cap is set"
            )
            continue
        cap = _money(f"teams.{team}.monthly_budget_usd", budget)
        teams[team] = {"monthly_spend_cap_usd": cap}
    return teams
