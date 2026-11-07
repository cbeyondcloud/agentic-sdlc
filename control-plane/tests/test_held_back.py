"""A run the publish scan held back (DF-536, owner decision 2026-11-07): the CLI sends a
terminal `held_back` marker carrying only the finding kinds; the control plane closes the
run out as held back, audits it once, and still takes the rest of the chain later (an admin
redacts locally and publishes again)."""

from __future__ import annotations

import pytest
from apiclient import member
from sqlalchemy import select, update
from test_ingest import chain, post

from agentic_cp.audit import read_records
from agentic_cp.tables import runs
from agentic_cp.tenancy.context import tenant_tx
from sdlc_commons import ledger as ledger_module

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


@pytest.fixture
async def dev(api):
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    return token


async def hold(api, token, seq, kinds=("pii:credit-card",), run_id="run-1"):
    body = {"seq": seq, "kinds": list(kinds)}
    return await api.post(f"/v1/runs/{run_id}/held-back", token, body)


async def _events(api, name):
    async with tenant_tx(api.db.app, api.a.id) as conn:
        return [r for r in await read_records(conn) if r.event == name]


async def test_the_marker_closes_the_run_out_as_held_back(api, dev, tmp_path):
    entries = chain(tmp_path, events=5)
    await post(api, dev, entries[:3])
    reply = await hold(api, dev, 4, ["pii:credit-card", "aws-access-key"])
    assert reply.status_code == 200, reply.text
    assert reply.json() == {
        "status": "held_back",
        "head": {"seq": 3, "hash": entries[2]["hash"]},
        "duplicate": False,
    }
    summary = (await api.get("/v1/runs/run-1", dev)).json()
    assert summary["status"] == "held_back"
    assert summary["head"]["seq"] == 3  # the marker never moves the chain
    [record] = await _events(api, "run.held_back")
    assert record.target == "run:run-1" and record.run_id == "run-1"
    assert record.after == {"seq": 4, "kinds": ["aws-access-key", "pii:credit-card"]}
    assert record.before == {"status": "open"}


async def test_the_same_marker_again_is_a_no_op(api, dev, tmp_path):
    await post(api, dev, chain(tmp_path, events=5)[:3])
    assert (await hold(api, dev, 4)).status_code == 200
    again = await hold(api, dev, 4)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert len(await _events(api, "run.held_back")) == 1


async def test_a_marker_must_follow_the_stored_head(api, dev, tmp_path):
    await post(api, dev, chain(tmp_path, events=5)[:3])
    for seq in (2, 3, 5):  # a client cannot skip events or hold back what is stored
        reply = await hold(api, dev, seq)
        assert reply.status_code == 409 and reply.json()["code"] == "ingest.gap", reply.text
        assert reply.json()["head"]["seq"] == 3
    assert (await api.get("/v1/runs/run-1", dev)).json()["status"] == "open"


async def test_a_marker_carries_kinds_only(api, dev, tmp_path):
    await post(api, dev, chain(tmp_path, events=5)[:3])
    card = "4111" + "1111" + "1111" + "1111"
    for kinds in ([], [f"order {card}"], ["x" * 65], ["a"] * 33):
        reply = await hold(api, dev, 4, kinds)
        assert reply.status_code == 422, kinds
    extra = await api.post(
        "/v1/runs/run-1/held-back", dev, {"seq": 4, "kinds": ["pii:email"], "value": card}
    )
    assert extra.status_code == 422  # no field can carry what the scan found
    assert card not in str([r.entry() for r in await _events(api, "run.held_back")])


async def test_only_the_starter_or_run_cancel_any_may_hold_back(api, dev, tmp_path):
    await post(api, dev, chain(tmp_path, events=5)[:3])
    peer, _ = await member(api, api.a, "peer@a.example.test", "developer", "payments")
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    assert (await hold(api, peer, 4)).status_code == 403
    assert (await hold(api, lead, 4)).status_code == 200
    assert (await hold(api, api.b.admin_token, 4)).status_code == 404  # another tenant's


async def test_a_run_the_control_plane_never_saw_is_not_found(api, dev):
    assert (await hold(api, dev, 1)).status_code == 404


async def test_the_rest_published_after_a_redaction_reopens_the_run(api, dev, tmp_path):
    entries = chain(tmp_path, events=5)
    await post(api, dev, entries[:3])
    await hold(api, dev, 4)
    # an admin redacted the run's ledger locally; the re-chained tail verifies from the head
    reply = await post(api, dev, entries[3:])
    assert reply.status_code == 200 and reply.json()["accepted"] == 3, reply.text
    assert (await api.get("/v1/runs/run-1", dev)).json()["status"] == "open"
    [record] = await _events(api, "run.republished")
    assert record.before == {"status": "held_back"} and record.after["head_seq"] == 6


async def test_a_republished_run_that_closes_takes_the_closing_status(api, dev, tmp_path):
    entries = chain(tmp_path, events=5, close=True)
    await post(api, dev, entries[:3])
    await hold(api, dev, 4)
    assert (await post(api, dev, entries[3:])).status_code == 200
    assert (await api.get("/v1/runs/run-1", dev)).json()["status"] == "done"


async def test_a_stale_run_held_back_is_no_longer_stale(api, dev, tmp_path):
    entries = chain(tmp_path, events=5)
    await post(api, dev, entries[:3])
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(update(runs).values(status="stale", stale_at=api.clock()))
    assert (await hold(api, dev, 4)).status_code == 200
    assert (await post(api, dev, entries[3:])).status_code == 200
    async with tenant_tx(api.db.app, api.a.id) as conn:
        row = (await conn.execute(select(runs.c.status, runs.c.stale_at))).one()
    assert row.status == "open" and row.stale_at is None


async def test_a_closed_run_is_not_held_back(api, dev, tmp_path):
    await post(api, dev, chain(tmp_path, events=5, close=True))
    reply = await hold(api, dev, 7)
    assert reply.status_code == 409 and reply.json()["code"] == "run.closed"
    assert await _events(api, "run.held_back") == []
