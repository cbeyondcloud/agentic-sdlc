"""Zyroflo's runner evidence tiers behind the framework's names (M2-41): what kind of runner
produced a conformance verdict - none, a scripted stand-in, or a live agent."""

import pytest

from sdlc_commons import conformance


def test_the_tiers_are_zyroflos_weakest_first():
    assert conformance.RUNNER_TIERS == ("null", "contract", "agent")
    assert conformance.SCRIPTED == "contract" and conformance.LIVE == "agent"


@pytest.mark.parametrize(
    "have, need, ok",
    [
        ("agent", "contract", True),
        ("contract", "contract", True),
        ("contract", "agent", False),
        ("null", "contract", False),
    ],
)
def test_a_stronger_tier_satisfies_a_weaker_requirement(have, need, ok):
    assert conformance.satisfies(have, need) is ok


def test_an_unknown_tier_is_refused():
    with pytest.raises(ValueError, match="not a runner tier"):
        conformance.rank("live")
    with pytest.raises(ValueError, match="not a runner tier"):
        conformance.satisfies("contract", "gold")


def test_each_tier_says_what_it_proves():
    assert set(conformance.MEANING) == set(conformance.RUNNER_TIERS)
    assert "scripted" in conformance.MEANING["contract"]
