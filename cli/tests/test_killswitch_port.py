"""The CLI's kill-switch port (M2-19; master Review Focus 3): the control plane's answer is
cached for a short TTL, the last good answer is used while it is fresh enough, and switch
state older than the stale threshold gives an unconfirmed verdict - a pause, never "run"."""

from __future__ import annotations

import pytest
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic.killswitch import NoKillSwitch, RunScope
from agentic_cli.control_plane import ControlPlaneClient
from agentic_cli.killswitch import CACHE_TTL_S, STALE_AFTER_S, ControlPlaneKillSwitch

SCOPE = RunScope(team="payments", user="someone@else", pipeline="deliver", agent="dev@1.0.0")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def plane() -> FakeControlPlane:
    return FakeControlPlane()


def port(plane, clock, **kwargs):
    client = ControlPlaneClient(CP_URL, TOKEN, transport=plane.transport)
    return ControlPlaneKillSwitch(
        client,
        user="dev@acme.example",
        team="payments",
        repo="github.com/acme/app",
        clock=clock,
        **kwargs,
    )


def state_requests(plane):
    return [r for r in plane.requests if r.url.path == "/v1/killswitch/state"]


def test_no_switch_runs_and_the_answer_is_cached_for_the_ttl(plane):
    clock = Clock()
    ks = port(plane, clock)
    assert ks.check(SCOPE).action == "run"
    clock.now += CACHE_TTL_S - 1
    assert ks.check(SCOPE).action == "run"
    assert len(state_requests(plane)) == 1
    query = state_requests(plane)[0].url.params
    assert (query["team"], query["repo"], query["pipeline"]) == (
        "payments",
        "github.com/acme/app",
        "deliver",
    )
    clock.now += 2
    ks.check(SCOPE)
    assert len(state_requests(plane)) == 2


def test_a_kill_switch_is_seen_after_the_ttl(plane):
    clock = Clock()
    ks = port(plane, clock)
    assert ks.check(SCOPE).action == "run"
    plane.add_switch("agent", "dev", mode="kill", reason="bad release")
    clock.now += CACHE_TTL_S
    verdict = ks.check(SCOPE)
    assert verdict.action == "kill" and "bad release" in verdict.describe()


def test_user_switches_match_the_signed_in_user(plane):
    plane.add_switch("user", "dev@acme.example", mode="pause", reason="offboarding")
    assert port(plane, Clock()).check(SCOPE).action == "pause"


def test_unreachable_within_the_stale_threshold_uses_the_last_good_answer(plane):
    clock = Clock()
    plane.add_switch("pipeline", "deliver", mode="pause", reason="freeze")
    ks = port(plane, clock)
    assert ks.check(SCOPE).action == "pause"
    plane.down = True
    clock.now += STALE_AFTER_S - 1
    verdict = ks.check(SCOPE)
    assert verdict.action == "pause" and not verdict.stale


def test_beyond_the_stale_threshold_the_verdict_is_unconfirmed(plane):
    clock = Clock()
    ks = port(plane, clock)
    assert ks.check(SCOPE).action == "run"
    plane.down = True
    clock.now += STALE_AFTER_S + 1
    verdict = ks.check(SCOPE)
    assert verdict.action == "pause" and verdict.stale
    assert "cannot reach" in verdict.describe()


def test_never_confirmed_and_unreachable_is_unconfirmed(plane):
    plane.down = True
    verdict = port(plane, Clock()).check(SCOPE)
    assert verdict.stale and verdict.action == "pause"


def test_an_unreachable_control_plane_is_not_asked_on_every_check(plane):
    clock = Clock()
    plane.down = True
    ks = port(plane, clock)
    for _ in range(5):
        ks.check(SCOPE)
    assert len(state_requests(plane)) == 1  # retried once per TTL, not per check


def test_standalone_has_no_switches():
    assert NoKillSwitch().check(SCOPE).action == "run"
