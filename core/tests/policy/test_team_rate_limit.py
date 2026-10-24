"""Team request limits (`teams/<team>.yaml: rate_limit`, M2-09) and the gateway's name for a
tier model (`gateway_model`, the one rule the client and the routing generator share)."""

import shutil
from pathlib import Path

import pytest

from agentic.policy import PolicyError, TeamRateLimit, gateway_model, load_policy


def policy_copy(fixtures: Path, tmp_path: Path, team_extra: str) -> Path:
    root = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", root)
    team = root / "teams" / "payments.yaml"
    team.write_text(team.read_text(encoding="utf-8") + team_extra, encoding="utf-8")
    return root


def test_a_team_has_no_rate_limit_by_default(fixtures):
    assert load_policy(fixtures / "policy" / "policy").teams["payments"].rate_limit is None


def test_a_team_rate_limit_loads(fixtures, tmp_path):
    extra = "rate_limit:\n  requests_per_minute: 120\n"
    found = load_policy(policy_copy(fixtures, tmp_path, extra)).teams["payments"].rate_limit
    assert found == TeamRateLimit(requests_per_minute=120)
    assert found.requests_per_hour is None


@pytest.mark.parametrize(
    "extra",
    [
        "rate_limit: {}\n",  # names no limit at all
        "rate_limit:\n  requests_per_minute: 0\n",  # 0 would refuse every request
        "rate_limit:\n  requests_per_day: 10\n",  # not a limit the gateway knows
    ],
)
def test_a_meaningless_team_rate_limit_is_refused(fixtures, tmp_path, extra):
    with pytest.raises(PolicyError):
        load_policy(policy_copy(fixtures, tmp_path, extra))


@pytest.mark.parametrize(
    ("model_id", "sent"),
    [
        ("tokenlean/openrouter/vendor/model-x", "openrouter/vendor/model-x"),
        ("local/small-model", "small-model"),
        ("plain-model", "plain-model"),
    ],
)
def test_the_gateway_gets_the_part_after_the_route(model_id, sent):
    assert gateway_model(model_id) == sent


def _edited(fixtures: Path, tmp_path: Path, name: str, old: str, new: str) -> Path:
    root = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", root)
    path = root / name
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return root


@pytest.mark.parametrize("budget", [".inf", "-.inf", ".nan"])
def test_a_team_budget_that_is_not_a_number_is_refused(fixtures, tmp_path, budget):
    """Review F1: an infinite or NaN budget cannot become a gateway spend cap or a budget
    alert; the policy is refused when it loads, never later when it is used."""
    team = "teams/payments.yaml"
    root = _edited(fixtures, tmp_path, team, "budget_usd: 2000", f"budget_usd: {budget}")
    with pytest.raises(PolicyError, match="monthly_budget_usd"):
        load_policy(root)


#: A class (allowing only the local tier) added under a name that is not header-safe.
CLASS = "  {name}:\n    allowed_tiers: [local]\n    allowed_backends: [native]\n  restricted:"


@pytest.mark.parametrize("name", ["geheim-é", '"two\\nlines"', '" padded "'])
def test_a_class_name_that_cannot_travel_as_a_header_is_refused(fixtures, tmp_path, name):
    """Review F8: a class name is sent as X-Agentic-Classification on every model call, so
    one that is not 1-256 printable ASCII is refused when the policy loads, not at the
    first model call of a run."""
    added = CLASS.format(name=name)
    root = _edited(fixtures, tmp_path, "classifications.yaml", "  restricted:", added)
    with pytest.raises(PolicyError, match="header"):
        load_policy(root)


def test_a_tier_name_that_cannot_travel_as_a_header_is_refused(fixtures, tmp_path):
    fast = "  fast: { model: google/gemini-flash }"
    added = fast + "\n  schnell-ü: { model: vendor/model-x }"
    root = _edited(fixtures, tmp_path, "tiers.yaml", fast, added)
    with pytest.raises(PolicyError, match="header"):
        load_policy(root)
