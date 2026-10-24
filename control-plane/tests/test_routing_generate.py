"""Routing generation (M2-09, Review Focus 1): an accepted policy becomes this tenant's part
of TokenLean's routing overlay - G6 rules pinning each tier's model (per team and per
classification), team monthly caps and team request limits - and the same policy always
gives the same bytes. `expected.json` pins them; regenerate it only on purpose, after
reading the diff."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentic.policy import OrgPolicy, load_policy
from agentic_cp.routing.generate import (
    GENERATOR_FORMAT,
    RoutingPlan,
    canonical,
    plan_digest,
    routing_plan,
)

FIXTURES = Path(__file__).parent / "fixtures" / "routing"
RULE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


@pytest.fixture
def policy() -> OrgPolicy:
    return load_policy(FIXTURES / "policy", version="git:" + "0" * 40)


def _rules(plan: RoutingPlan) -> list[dict]:
    return plan.tenant["groups"]["G6_routing"]["rules"]


def _by_id(plan: RoutingPlan) -> dict[str, dict]:
    return {rule["id"]: rule for rule in _rules(plan)}


def test_plan_matches_the_pinned_fixture(policy):
    plan = routing_plan(policy)
    expected = (FIXTURES / "expected.json").read_bytes().strip()
    assert canonical(plan.to_settings()) == expected


def test_plan_ignores_mapping_order(policy):
    """Every mapping of the policy rebuilt in reverse order gives the same bytes."""

    def reverse(value):
        if isinstance(value, dict):
            return {key: reverse(value[key]) for key in reversed(list(value))}
        if isinstance(value, list):
            return [reverse(item) for item in value]
        return value

    data = policy.model_dump(mode="json", exclude_defaults=True)
    shuffled = OrgPolicy.model_validate(reverse(data))
    assert canonical(routing_plan(shuffled).to_settings()) == canonical(
        routing_plan(policy).to_settings()
    )
    assert plan_digest(routing_plan(shuffled)) == plan_digest(routing_plan(policy))


def test_every_tier_pins_its_gateway_model(policy):
    rules = _by_id(routing_plan(policy))
    tier = rules["agentic.tier.deep-reasoning"]
    assert tier["match"] == {"params": {"x_agentic_tier": ["deep-reasoning"]}}
    # the gateway knows the model by the part after its route (DF-394)
    assert tier["action"] == {"model": "openrouter/vendor-a/deep-model", "allow_escalation": True}
    assert rules["agentic.tier.local"]["action"]["model"] == "qwen-coder"


def test_a_team_override_wins_for_that_team_only(policy):
    rules = _by_id(routing_plan(policy))
    team = rules["agentic.team.payments.tier.balanced"]
    assert team["match"]["params"] == {
        "x_agentic_team": ["payments"],
        "x_agentic_tier": ["balanced"],
    }
    assert team["action"]["model"] == "vendor-c-balanced"
    assert team["priority"] > rules["agentic.tier.balanced"]["priority"]
    assert rules["agentic.tier.balanced"]["action"]["model"] == "vendor-a-balanced"


def test_an_unrestrictive_class_makes_no_class_rules(policy):
    assert not [rule for rule in _rules(routing_plan(policy)) if "internal" in rule["id"]]


def test_a_restrictive_class_pins_allowed_models_and_a_fallback(policy):
    rules = _by_id(routing_plan(policy))
    assert rules["agentic.class.restricted.tier.local"]["action"]["model"] == "qwen-coder"
    fallback = rules["agentic.class.restricted"]
    assert fallback["match"] == {"params": {"x_agentic_classification": ["restricted"]}}
    assert fallback["action"]["model"] == "qwen-coder"  # any other tier, or none, lands here
    # confidential leaves out `fast`: its fallback is its first allowed tier's model
    assert "agentic.class.confidential.tier.fast" not in rules
    assert rules["agentic.class.confidential"]["action"]["model"] == "vendor-a-balanced"
    assert rules["agentic.class.confidential"]["priority"] > rules["agentic.tier.fast"]["priority"]


def test_a_team_override_inside_a_class(policy):
    rules = _by_id(routing_plan(policy))
    # payments' own balanced model is allowed in confidential repos: a class+team rule
    combo = rules["agentic.class.confidential.team.payments.tier.balanced"]
    assert combo["action"]["model"] == "vendor-c-balanced"
    assert combo["priority"] > rules["agentic.class.confidential.tier.balanced"]["priority"]
    assert rules["agentic.class.confidential.team.payments"]["action"]["model"] == (
        "vendor-c-balanced"
    )
    # payments' own local model is not allowed in restricted repos: no rule offers it there
    assert "agentic.class.restricted.team.payments.tier.local" not in rules
    assert "agentic.class.restricted.team.payments" not in rules
    restricted = [r for r in _rules(routing_plan(policy)) if "restricted" in r["id"]]
    assert {r["action"]["model"] for r in restricted} == {"qwen-coder"}


def test_escalation_is_allowed_so_the_policy_model_is_served(policy):
    assert all(rule["action"]["allow_escalation"] is True for rule in _rules(routing_plan(policy)))


def test_rule_ids_fit_the_gateway_and_are_unique(policy, tmp_path):
    rules = _rules(routing_plan(policy))
    assert all(RULE_ID.fullmatch(rule["id"]) for rule in rules)
    assert len({rule["id"] for rule in rules}) == len(rules)
    long_name = "x" * 70
    data = policy.model_dump(mode="json", exclude_defaults=True)
    data["tiers"][long_name] = {"model": "tokenlean/m"}
    data["classifications"]["restricted"]["allowed_tiers"].append(long_name)
    data["classifications"]["restricted"]["allowed_models"].append("tokenlean/m")
    rules = _rules(routing_plan(OrgPolicy.model_validate(data)))
    assert all(RULE_ID.fullmatch(rule["id"]) for rule in rules)
    assert len({rule["id"] for rule in rules}) == len(rules)


def test_rules_are_ordered_by_priority(policy):
    priorities = [rule["priority"] for rule in _rules(routing_plan(policy))]
    assert priorities == sorted(priorities, reverse=True)


def test_team_caps_and_limits(policy):
    plan = routing_plan(policy)
    assert plan.tenant["teams"] == {
        "payments": {"monthly_spend_cap_usd": 2000},
        "search": {"monthly_spend_cap_usd": 512.5},
    }
    assert isinstance(plan.tenant["teams"]["payments"]["monthly_spend_cap_usd"], int)
    assert plan.per_team == {"search": {"requests_per_minute": 120, "requests_per_hour": 3000}}


def test_a_zero_or_negative_budget_is_not_sent_and_is_named(policy):
    data = policy.model_dump(mode="json", exclude_defaults=True)
    data["teams"]["search"]["monthly_budget_usd"] = 0
    data["teams"]["payments"]["monthly_budget_usd"] = -5
    plan = routing_plan(OrgPolicy.model_validate(data))
    assert "teams" not in plan.tenant
    assert any("search" in warning for warning in plan.warnings)
    assert any("payments" in warning for warning in plan.warnings)


@pytest.mark.parametrize("budget", [float("inf"), float("nan")])
def test_a_budget_that_is_not_a_number_is_a_value_error(policy, budget):
    """Review F1: the policy model refuses such a budget; should one reach the generator
    anyway, it raises ValueError (policy sync records a rejection), never OverflowError or
    a NaN in the plan that the database cannot store."""
    search = policy.teams["search"].model_copy(update={"monthly_budget_usd": budget})
    broken = policy.model_copy(update={"teams": {**policy.teams, "search": search}})
    with pytest.raises(ValueError, match="search"):
        routing_plan(broken)


def test_a_class_that_allows_no_model_is_named(policy):
    data = policy.model_dump(mode="json", exclude_defaults=True)
    data["classifications"]["restricted"]["allowed_models"] = ["ollama/not-a-tier-model"]
    plan = routing_plan(OrgPolicy.model_validate(data))
    assert any("restricted" in warning for warning in plan.warnings)
    assert "agentic.class.restricted" not in _by_id(plan)


def test_the_settings_round_trip(policy):
    plan = routing_plan(policy)
    stored = json.loads(canonical(plan.to_settings()))
    assert stored["format"] == GENERATOR_FORMAT
    assert RoutingPlan.from_settings(stored) == plan
    assert RoutingPlan.from_settings({"format": GENERATOR_FORMAT - 1}) is None
    assert RoutingPlan.from_settings(None) is None
