"""Auto-trip thresholds in org policy (M2-11): `org.yaml: auto_trip` sets the org's spend
and error-rate thresholds and how the control plane measures and resets them; a team's
`teams/<team>.yaml: auto_trip` may only tighten them."""

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from agentic.policy import AutoTripPolicy, PolicyError, load_policy


def policy_copy(fixtures: Path, tmp_path: Path, org_extra: str = "", team_extra: str = "") -> Path:
    root = tmp_path / "policy"
    shutil.copytree(fixtures / "policy" / "policy", root)
    for path, extra in (
        (root / "org.yaml", org_extra),
        (root / "teams" / "payments.yaml", team_extra),
    ):
        path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return root


def test_auto_trip_is_off_by_default(fixtures):
    policy = load_policy(fixtures / "policy" / "policy")
    assert policy.auto_trip == AutoTripPolicy()
    assert policy.auto_trip.spend_usd_per_hour is None and policy.auto_trip.error_rate is None
    assert policy.teams["payments"].auto_trip is None
    assert policy.auto_trip.effective(None) == (None, None)


def test_org_thresholds_load_as_exact_decimals(fixtures, tmp_path):
    extra = (
        "auto_trip:\n  spend_usd_per_hour: 12.1\n  error_rate: 0.3\n  window_minutes: 30\n"
        "  min_steps: 10\n  reset_ratio: 0.6\n  hold_minutes: 15\n  auto_clear: true\n"
    )
    trip = load_policy(policy_copy(fixtures, tmp_path, extra)).auto_trip
    assert trip.spend_usd_per_hour == Decimal("12.1")  # through text, never 12.0999...
    assert trip.error_rate == Decimal("0.3") and trip.reset_ratio == Decimal("0.6")
    assert (trip.window_minutes, trip.min_steps, trip.hold_minutes) == (30, 10, 15)
    assert trip.auto_clear is True


def test_a_team_may_tighten(fixtures, tmp_path):
    org = "auto_trip:\n  spend_usd_per_hour: 50\n"
    team = "auto_trip:\n  spend_usd_per_hour: 20\n  error_rate: 0.25\n"
    policy = load_policy(policy_copy(fixtures, tmp_path, org, team))
    assert policy.auto_trip.effective(policy.teams["payments"].auto_trip) == (
        Decimal(20),
        Decimal("0.25"),
    )
    assert policy.auto_trip.effective(None) == (Decimal(50), None)


@pytest.mark.parametrize(
    ("org", "team", "named"),
    [
        (
            "auto_trip:\n  spend_usd_per_hour: 50\n",
            "auto_trip:\n  spend_usd_per_hour: 80\n",
            "teams.payments.auto_trip.spend_usd_per_hour",
        ),
        (
            "auto_trip:\n  error_rate: 0.2\n",
            "auto_trip:\n  error_rate: 0.5\n",
            "teams.payments.auto_trip.error_rate",
        ),
    ],
)
def test_a_team_cannot_loosen(fixtures, tmp_path, org, team, named):
    with pytest.raises(PolicyError, match=named):
        load_policy(policy_copy(fixtures, tmp_path, org, team))


@pytest.mark.parametrize(
    "extra",
    [
        "auto_trip:\n  spend_usd_per_hour: 0\n",  # 0 would trip on any spend at all
        "auto_trip:\n  error_rate: 1.5\n",
        "auto_trip:\n  error_rate: 0\n",
        "auto_trip:\n  reset_ratio: 1\n",  # no hysteresis: it would flap
        "auto_trip:\n  window_minutes: 1\n",
        "auto_trip:\n  spend_usd_per_hour: .nan\n",
        "auto_trip:\n  spend_per_hour: 5\n",  # a typo is an error, never ignored
    ],
)
def test_values_that_would_misbehave_are_errors(fixtures, tmp_path, extra):
    with pytest.raises(PolicyError, match="auto_trip"):
        load_policy(policy_copy(fixtures, tmp_path, extra))
