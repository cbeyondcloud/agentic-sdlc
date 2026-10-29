"""The kill-switch poll's short cache (M3-22, DF-704): two thirds of the load test's requests
were kill polls, each a tenant transaction reading switches that almost never change. A
process now reads a tenant's switches at most once per `kill_cache_seconds` (2 s), and a
change it makes itself shows at once; another replica sees it within the window, which keeps
a kill inside the 30 s target (CLI cache 10 s + watchdog 5 s + 2 s)."""

from datetime import timedelta

import pytest

from agentic_cp.killswitch import api as killswitch_api

pytestmark = pytest.mark.anyio

STATE = "/v1/killswitch/state?team=payments&pipeline=deliver"
SWITCH = {"scope_kind": "team", "scope_value": "payments", "reason": "incident 42"}


@pytest.fixture
def reads(monkeypatch):
    found = []
    real = killswitch_api.store.rows

    async def counting(conn):
        found.append(1)
        return await real(conn)

    monkeypatch.setattr(killswitch_api.store, "rows", counting)
    return found


async def _switches(api, token=None):
    reply = await api.get(STATE, token or api.a.admin_token)
    assert reply.status_code == 200, reply.text
    return reply.json()["switches"]


async def test_polls_read_the_switches_once_per_window(api, reads):
    for _ in range(5):
        assert await _switches(api) == []
    assert len(reads) == 1
    api.clock.advance(timedelta(seconds=2))
    await _switches(api)
    assert len(reads) == 2
    assert await _switches(api, api.b.admin_token) == []  # each tenant has its own entry
    assert len(reads) == 3


async def test_a_change_made_by_this_process_shows_at_once(api):
    assert await _switches(api) == []
    made = await api.post("/v1/killswitch", api.a.admin_token, SWITCH)
    assert made.status_code == 201, made.text
    assert [s["id"] for s in await _switches(api)] == [made.json()["id"]]
    cleared = await api.delete(f"/v1/killswitch/{made.json()['id']}", api.a.admin_token)
    assert cleared.status_code == 200, cleared.text
    assert await _switches(api) == []


async def test_another_replica_sees_a_change_within_the_window(api):
    replica = api.with_app()
    try:
        assert await _switches(replica) == []
        made = await api.post("/v1/killswitch", api.a.admin_token, SWITCH)
        assert made.status_code == 201
        api.clock.advance(timedelta(seconds=2))
        assert [s["id"] for s in await _switches(replica)] == [made.json()["id"]]
    finally:
        await replica.http.aclose()


async def test_the_cache_can_be_turned_off(api, reads):
    off = api.with_settings(kill_cache_seconds=0)
    try:
        for _ in range(3):
            await _switches(off)
        assert len(reads) == 3
    finally:
        await off.http.aclose()
