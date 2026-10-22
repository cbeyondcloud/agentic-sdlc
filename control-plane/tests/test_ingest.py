"""Run event ingest per tenant (SP15 Task 9, M2-04; Review Focus 1): a run's chain is taken
exactly as written or not at all, a replay is a no-op, and runs never cross tenants."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest
from apiclient import member
from policy_repos import head, make_policy_repo, url
from sqlalchemy import func, select

from agentic_cp.audit import read_records
from agentic_cp.tables import run_events, runs
from agentic_cp.tenancy.context import tenant_tx
from sdlc_commons import ledger as ledger_module
from sdlc_commons.ledger import RunIdentity, RunLedger, entry_hash, verify_entries

pytestmark = pytest.mark.anyio

POLICY = "git:" + "c" * 40


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


def chain(tmp_path: Path, run_id="run-1", team="payments", policy=POLICY, events=5, close=False):
    """A real run ledger's entries (Zyroflo's format), `events` + 1 entries long."""
    ledger = RunLedger(tmp_path / f"{run_id}-{team}-{events}", run_id)
    ledger.open(
        identity=RunIdentity(team=team, policy_version=policy),
        kind="feature",
        project="demo",
        owner=team,
        criticality="low",
    )
    for n in range(events - (1 if close else 0)):
        ledger.record("step_started", phase="requirements", attempt=n + 1)
    if close:
        ledger.record("run_closed", status="done")
    entries = ledger.events()
    ledger.release()
    return entries


async def post(api, token, entries, run_id="run-1"):
    return await api.post(f"/v1/runs/{run_id}/events", token, {"entries": entries})


@pytest.fixture
async def dev(api):
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    return token


async def _stored(api, tenant_id, run_id="run-1"):
    async with tenant_tx(api.db.app, tenant_id) as conn:
        return (
            await conn.execute(
                select(func.count()).select_from(run_events).where(run_events.c.run_id == run_id)
            )
        ).scalar_one()


async def test_first_batch_registers_the_run(api, dev, tmp_path):
    entries = chain(tmp_path)
    reply = await post(api, dev, entries)
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body == {
        "accepted": 6,
        "duplicates": 0,
        "head": {"seq": 6, "hash": entries[-1]["hash"]},
    }
    summary = (await api.get("/v1/runs/run-1", dev)).json()
    assert summary["team"] == "payments" and summary["started_by"] == "dev@a.example.test"
    assert summary["status"] == "open" and summary["head"]["seq"] == 6


async def test_broken_chain_rejected_and_nothing_stored(api, dev, tmp_path):
    entries = copy.deepcopy(chain(tmp_path, events=4))
    entries[2]["attempt"] = 99  # edited after hashing
    reply = await post(api, dev, entries)
    assert reply.status_code == 422, reply.text
    problem = reply.json()
    assert problem["code"] == "ingest.chain_broken"
    assert any(p.startswith("event 3") for p in problem["problems"]), problem
    assert (await api.get("/v1/runs/run-1", dev)).status_code == 404
    assert await _stored(api, api.a.id) == 0
    records = (await api.get("/v1/audit", api.a.admin_token)).json()
    assert records[-1]["event"] == "ingest.chain_rejected" and records[-1]["run_id"] == "run-1"


async def test_duplicate_replay_is_a_noop(api, dev, tmp_path):
    entries = chain(tmp_path, events=4)
    assert (await post(api, dev, entries)).status_code == 200
    again = await post(api, dev, entries)
    assert again.status_code == 200
    assert again.json()["accepted"] == 0 and again.json()["duplicates"] == 5
    assert await _stored(api, api.a.id) == 5


async def test_overlapping_replay_accepts_only_new(api, dev, tmp_path):
    entries = chain(tmp_path, events=7)
    assert (await post(api, dev, entries[:5])).json()["accepted"] == 5
    overlap = await post(api, dev, entries[2:])
    assert overlap.json() == {
        "accepted": 3,
        "duplicates": 3,
        "head": {"seq": 8, "hash": entries[-1]["hash"]},
    }


async def test_fork_is_rejected(api, dev, tmp_path):
    entries = chain(tmp_path, events=5)
    await post(api, dev, entries)
    forked = copy.deepcopy(entries[3])
    forked["at"] = "2026-01-01T00:00:00.000+00:00"
    forked["hash"] = entry_hash(forked)
    reply = await post(api, dev, [forked])
    assert reply.status_code == 409 and reply.json()["code"] == "ingest.fork"
    assert reply.json()["head"]["seq"] == 6
    assert (await api.get("/v1/runs/run-1", dev)).json()["head"]["seq"] == 6
    records = (await api.get("/v1/audit", api.a.admin_token)).json()
    assert records[-1]["event"] == "ingest.fork"


async def test_gap_is_409_with_head(api, dev, tmp_path):
    entries = chain(tmp_path, events=6)
    await post(api, dev, entries[:3])
    reply = await post(api, dev, entries[5:7])
    assert reply.status_code == 409 and reply.json()["code"] == "ingest.gap"
    assert reply.json()["head"] == {"seq": 3, "hash": entries[2]["hash"]}
    unknown = await post(api, dev, entries[1:3], run_id="run-1x")
    assert unknown.status_code == 409 and unknown.json()["head"] == {"seq": 0, "hash": ""}


async def test_first_batch_must_open_the_run(api, dev, tmp_path):
    entries = copy.deepcopy(chain(tmp_path, events=2))
    entries[0]["event"] = "step_started"
    entries[0].pop("kind")
    entries[0].pop("project")
    entries[0].pop("owner")
    entries[0].pop("criticality")
    entries[0]["attempt"] = 1
    entries[0]["hash"] = entry_hash(entries[0])
    entries[1]["prev"] = entries[0]["hash"]
    entries[1]["hash"] = entry_hash(entries[1])
    reply = await post(api, dev, entries)
    assert reply.status_code == 422 and reply.json()["code"] == "ingest.not_opened"


async def test_caller_needs_run_trigger_in_the_runs_team(api, tmp_path):
    other, _ = await member(api, api.a, "other@a.example.test", "developer", "billing")
    approver, _ = await member(api, api.a, "ap@a.example.test", "approver", "payments")
    entries = chain(tmp_path)
    assert (await post(api, other, entries)).status_code == 403
    assert (await post(api, approver, entries)).status_code == 403
    assert await _stored(api, api.a.id) == 0


async def test_only_the_starter_or_run_cancel_any_may_append(api, dev, tmp_path):
    entries = chain(tmp_path, events=5)
    await post(api, dev, entries[:3])
    peer, _ = await member(api, api.a, "peer@a.example.test", "developer", "payments")
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    refused = await post(api, peer, entries[3:])
    assert refused.status_code == 403
    allowed = await post(api, lead, entries[3:])
    assert allowed.status_code == 200 and allowed.json()["accepted"] == 3


async def test_batch_too_large_is_413(api, dev, tmp_path):
    small = api.with_settings(max_ingest_entries=3)
    reply = await post(small, dev, chain(tmp_path, events=4))
    assert reply.status_code == 413 and reply.json()["code"] == "ingest.too_large"
    await small.http.aclose()


async def test_unknown_policy_version_is_flagged_not_dropped(api, dev, tmp_path):
    reply = await post(api, dev, chain(tmp_path))
    assert reply.status_code == 200
    summary = (await api.get("/v1/runs/run-1", dev)).json()
    assert summary["policy_known"] is False
    records = (await api.get("/v1/audit", api.a.admin_token)).json()
    assert any(r["event"] == "ingest.unknown_policy_version" for r in records)


async def test_known_policy_version_is_not_flagged(api, dev, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    source = {"repo": url(repo), "branch": "main"}
    assert (await api.put("/v1/tenant/policy-source", api.a.admin_token, source)).status_code == 200
    reply = await post(api, dev, chain(tmp_path, policy="git:" + head(repo)))
    assert reply.status_code == 200, reply.text
    assert (await api.get("/v1/runs/run-1", dev)).json()["policy_known"] is True


async def test_policy_version_known_only_to_another_tenant_is_flagged(api, dev, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-b")
    source = {"repo": url(repo), "branch": "main"}
    assert (await api.put("/v1/tenant/policy-source", api.b.admin_token, source)).status_code == 200
    await post(api, dev, chain(tmp_path, policy="git:" + head(repo)))
    assert (await api.get("/v1/runs/run-1", dev)).json()["policy_known"] is False


async def test_same_run_id_in_two_tenants_is_two_runs(api, dev, tmp_path):
    dev_b, _ = await member(api, api.b, "dev@b.example.test", "developer", "payments")
    chain_a = chain(tmp_path, events=3)
    chain_b = chain(tmp_path, team="payments", events=5)
    assert chain_a[0]["hash"] != chain_b[0]["hash"]
    assert (await post(api, dev, chain_a)).status_code == 200
    assert (await post(api, dev_b, chain_b)).status_code == 200
    a_events = (await api.get("/v1/runs/run-1/events", dev)).json()
    b_events = (await api.get("/v1/runs/run-1/events", dev_b)).json()
    assert a_events == chain_a and b_events == chain_b
    assert (await api.get("/v1/runs/run-1", dev)).json()["head"]["seq"] == 4


async def test_run_closed_sets_status(api, dev, tmp_path):
    await post(api, dev, chain(tmp_path, events=3, close=True))
    assert (await api.get("/v1/runs/run-1", dev)).json()["status"] == "done"


async def test_run_summary_and_events_readable_by_starter_and_team_auditor(api, dev, tmp_path):
    await post(api, dev, chain(tmp_path))
    auditor, _ = await member(api, api.a, "aud@a.example.test", "auditor", "payments")
    stranger, _ = await member(api, api.a, "x@a.example.test", "developer", "billing")
    for token in (dev, auditor, api.a.admin_token):
        assert (await api.get("/v1/runs/run-1", token)).status_code == 200
        assert (await api.get("/v1/runs/run-1/events", token)).status_code == 200
    assert (await api.get("/v1/runs/run-1", stranger)).status_code == 403
    assert (await api.get("/v1/runs/run-1/events", stranger)).status_code == 403
    assert (await api.get("/v1/runs/run-1", api.b.admin_token)).status_code == 404
    page = (await api.get("/v1/runs/run-1/events?after_seq=4&limit=1", dev)).json()
    assert [e["seq"] for e in page] == [5]


async def test_concurrent_batches_for_one_run_serialise(api, dev, tmp_path):
    entries = chain(tmp_path, events=7)
    replies = await asyncio.gather(post(api, dev, entries), post(api, dev, entries))
    assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
    totals = [(r.json()["accepted"], r.json()["duplicates"]) for r in replies]
    assert sorted(totals) == [(0, 8), (8, 0)]
    assert await _stored(api, api.a.id) == 8


async def test_ingested_entries_rehash_after_the_jsonb_round_trip(api, dev, tmp_path):
    """R17: a float cost and unicode survive jsonb, so the stored entries verify again."""
    ledger = RunLedger(tmp_path / "floaty", "run-f")
    ledger.open(
        identity=RunIdentity(team="payments", policy_version=POLICY),
        kind="feature",
        project="demo",
        owner="payments",
        criticality="low",
    )
    ledger.record(
        "step_finished",
        phase="requirements",
        attempt=1,
        outcome="pass",
        cost={"usd": 0.1234567890123, "tiny": 1e-07, "big": 12345678901234567},
        tokens={"in": 3, "out": 4},
    )
    entries = ledger.events()
    ledger.release()
    assert (await post(api, dev, entries, run_id="run-f")).status_code == 200
    stored = (await api.get("/v1/runs/run-f/events", dev)).json()
    assert stored == entries
    assert verify_entries(stored, run_id="run-f") == []


async def test_nan_and_nul_are_refused(api, dev, tmp_path):
    entries = copy.deepcopy(chain(tmp_path, events=1))
    entries[0]["project"] = "demo\u0000"
    entries[0]["hash"] = entry_hash(entries[0])
    reply = await post(api, dev, entries)
    assert reply.status_code == 422
    raw = '{"entries": [{"seq": NaN}]}'
    sent = await api.http.post(
        "/v1/runs/run-1/events",
        content=raw,
        headers={"Authorization": f"Bearer {dev}", "Content-Type": "application/json"},
    )
    assert sent.status_code == 422


async def test_runs_never_cross_tenants_in_the_tables(api, dev, tmp_path):
    await post(api, dev, chain(tmp_path))
    async with tenant_tx(api.db.app, api.b.id) as conn:
        assert (await conn.execute(select(runs))).all() == []
    async with tenant_tx(api.db.app, api.a.id) as conn:
        records = await read_records(conn)
    assert all(r.tenant == str(api.a.id) for r in records)
