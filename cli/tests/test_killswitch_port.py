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


def test_the_server_ttl_shortens_the_cache(plane):
    """DF-516: an operator shortens propagation with AGENTIC_CP_KILL_TTL_SECONDS."""
    plane.kill_ttl_seconds = 3
    clock = Clock()
    ks = port(plane, clock)
    assert ks.check(SCOPE).action == "run"
    plane.add_switch("pipeline", "deliver", mode="kill", reason="stop now")
    clock.now += 2.9
    assert ks.check(SCOPE).action == "run"  # still the cached answer
    clock.now += 0.1
    assert ks.check(SCOPE).action == "kill"
    assert len(state_requests(plane)) == 2


def test_the_server_ttl_never_lengthens_the_cache(plane):
    """DF-516 (ruling R5): the CLI's 10 s bound keeps the 30 s propagation target."""
    plane.kill_ttl_seconds = 30
    clock = Clock()
    ks = port(plane, clock)
    ks.check(SCOPE)
    clock.now += CACHE_TTL_S
    ks.check(SCOPE)
    assert len(state_requests(plane)) == 2


def test_a_304_confirms_the_cached_switches(plane):
    """DF-506: the next poll sends the answer's ETag; a 304 keeps the switches the port
    holds and counts as a confirmation, so it never goes stale while the server answers."""
    plane.add_switch("team", "payments", mode="pause", reason="maintenance")
    clock = Clock()
    ks = port(plane, clock)
    assert ks.check(SCOPE).action == "pause"
    first = state_requests(plane)[0]
    assert "if-none-match" not in first.headers
    for _ in range(int(STALE_AFTER_S // CACHE_TTL_S) + 2):  # well past the stale window
        clock.now += CACHE_TTL_S
        verdict = ks.check(SCOPE)
        assert verdict.action == "pause" and not verdict.stale
    later = state_requests(plane)[1:]
    assert later and all(r.headers["if-none-match"].startswith('"') for r in later)
    plane.switches.clear()  # a change: a full answer again
    clock.now += CACHE_TTL_S
    assert ks.check(SCOPE).action == "run"


def test_a_304_without_a_cached_answer_is_not_a_confirmation(plane, monkeypatch):
    """A 304 the port did not ask for (no tag sent) confirms nothing: never "run"."""
    import httpx2

    def not_modified(request):
        return httpx2.Response(304, headers={"ETag": '"x"'})

    client = ControlPlaneClient(CP_URL, TOKEN, transport=httpx2.MockTransport(not_modified))
    ks = ControlPlaneKillSwitch(client, user=None, team="payments", repo=None, clock=Clock())
    verdict = ks.check(SCOPE)
    assert verdict.action == "pause" and verdict.stale


def test_an_answer_of_the_wrong_shape_is_unconfirmed(plane):
    """Review F5: a proxy or captive portal answering 200 with other JSON is no answer:
    the verdict is unconfirmed (hold), never an exception out of the port."""
    import httpx2

    def portal(request):
        return httpx2.Response(200, json={"error": "sign in to the network"})

    client = ControlPlaneClient(CP_URL, TOKEN, transport=httpx2.MockTransport(portal))
    ks = ControlPlaneKillSwitch(client, user=None, team="payments", repo=None, clock=Clock())
    verdict = ks.check(SCOPE)
    assert verdict.action == "pause" and verdict.stale
    assert "not a kill-switch state" in verdict.describe()


def test_standalone_has_no_switches():
    assert NoKillSwitch().check(SCOPE).action == "run"
