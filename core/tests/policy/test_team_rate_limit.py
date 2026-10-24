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
