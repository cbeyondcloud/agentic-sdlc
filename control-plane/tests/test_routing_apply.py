"""Applying routing to the gateway (M2-09; Review Focus 2 and 3): an accepted policy version
queues an apply in the activation's own transaction (the outbox); the worker job replaces
only this tenant's part of the gateway's overlay, always naming the version it read, reads
it back, records which gateway version applied which policy version, and audits it. A 409
is re-read and redone; a 422 is terminal; an unreachable gateway backs off and fails
audibly; a newer version supersedes a pending one; no tenant lock is held while the gateway
is called. Gateway: `FakeRouting` (no live TokenLean)."""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
from apiclient import assign, settings_for
from fake_routing import FakeRouting
from policy_repos import commit, git, url
from pydantic import SecretStr
from sqlalchemy import select, update

from agentic.policy import load_policy
from agentic_cp.audit import read_records
from agentic_cp.gitops import GitRunner
from agentic_cp.policy_sync.service import sync_once
from agentic_cp.routing import apply as routing_apply
from agentic_cp.routing.apply import apply_due, routing_job
from agentic_cp.routing.gateway import TokenLeanRouting
from agentic_cp.routing.generate import plan_digest, routing_plan
from agentic_cp.routing.outbox import queue_apply
from agentic_cp.tables import routing_applies, tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

FIXTURE_POLICY = Path(__file__).parent / "fixtures" / "routing" / "policy"
ADMIN_KEY = "tl-admin-" + "s3cr3t" * 4  # gitleaks:allow (made up)
GW = "gw-a"
PROMPT = 10.0


@pytest.fixture
def settings(api):
    return settings_for(api.db).model_copy(
        update={
            "gateway_admin_url": "https://gateway.example.test",
            "gateway_admin_key": SecretStr(ADMIN_KEY),
        }
    )


@pytest.fixture
def gateway() -> FakeRouting:
    return FakeRouting()


async def _gateway_tenant(api, tenant_id, value: str | None = GW) -> None:
    async with tenant_tx(api.db.owner, tenant_id) as conn:
        await conn.execute(
            update(tenants).where(tenants.c.id == tenant_id).values(gateway_tenant=value)
        )


def _policy_repo(path: Path, edit=None) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    shutil.copytree(FIXTURE_POLICY, path / "policy")
    if edit is not None:
        edit(path / "policy")
    commit(path, "routing policy")
    return path


async def _accept(api, settings, tmp_path, *, name="repo", edit=None) -> str:
    """Point tenant a at a policy repo holding the routing fixture and sync it."""
    repo = _policy_repo(tmp_path / name, edit)
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            update(tenants).where(tenants.c.id == api.a.id).values(policy_repo_url=url(repo))
        )
    outcome = await sync_once(api.db.app, GitRunner(settings), api.a.id, settings, api.clock)
    assert outcome.kind == "activated", outcome
    return outcome.version


async def _rows(api, tenant_id=None) -> list:
    async with tenant_tx(api.db.app, tenant_id or api.a.id) as conn:
        query = select(routing_applies).order_by(routing_applies.c.created_at)
        return list(await conn.execute(query))


async def _events(api, prefix="routing.") -> list:
    async with tenant_tx(api.db.app, api.a.id) as conn:
        return [r for r in await read_records(conn) if r.event.startswith(prefix)]


async def _apply(api, gateway, settings):
    return await apply_due(api.db.app, api.a.id, gateway, settings, api.clock)


def _expected_plan():
    return routing_plan(load_policy(FIXTURE_POLICY, version="git:" + "0" * 40))


async def test_activation_queues_one_apply(api, settings, tmp_path):
    version = await _accept(api, settings, tmp_path)
    rows = await _rows(api)
    assert [(r.policy_version, r.status) for r in rows] == [(version, "pending")]
    assert [e.event for e in await _events(api)] == ["routing.queued"]


async def test_without_a_gateway_admin_nothing_is_queued(api, tmp_path):
    await _accept(api, settings_for(api.db), tmp_path)
    assert await _rows(api) == []


async def test_apply_replaces_only_its_tenant(api, settings, gateway, tmp_path):
    await _gateway_tenant(api, api.a.id)
    others = {
        "groups": {"G6_routing": {"strategy": "priority"}},
        "teams": {"payments": {"monthly_spend_cap_usd": 1}},
        "tenants": {"gw-b": {"teams": {"ops": {"monthly_spend_cap_usd": 9}}}, GW: {"old": 1}},
    }
    gateway.store(others)
    version = await _accept(api, settings, tmp_path)
    report = await _apply(api, gateway, settings)
    assert report.status == "applied"
    plan = _expected_plan()
    current = gateway.current
    assert current["tenants"][GW] == plan.tenant
    assert current["tenants"]["gw-b"] == others["tenants"]["gw-b"]
    assert current["groups"] == others["groups"] and current["teams"] == others["teams"]
    assert "rate_limit" not in current  # team rate limits are off by default
    assert version in gateway.comments[-1]
    (row,) = await _rows(api)
    assert (row.status, row.gateway_version, row.previous_gateway_version) == ("applied", 2, 1)
    assert row.plan_sha256 == plan_digest(plan)
    applied = (await _events(api))[-1]
    assert applied.event == "routing.applied" and applied.policy_version == version
    assert applied.after["gateway_version"] == 2
    assert applied.after["plan_sha256"] == plan_digest(plan)
    assert applied.before == {"gateway_version": 1}
    assert any("rate limit" in w for w in row.warnings)  # search's limits were not applied


async def test_a_second_apply_of_the_same_plan_writes_nothing(api, settings, gateway, tmp_path):
    await _gateway_tenant(api, api.a.id)
    version = await _accept(api, settings, tmp_path)
    await _apply(api, gateway, settings)
    async with tenant_tx(api.db.app, api.a.id) as conn:
        await queue_apply(
            conn, version, actor="admin@a.example.test", actor_kind="human", clock=api.clock
        )
    report = await _apply(api, gateway, settings)
    assert report.status == "applied" and gateway.version == 1  # no new gateway version
    assert gateway.calls.count("write") == 1


async def test_conflict_rereads_and_retries(api, settings, gateway, tmp_path):
    await _gateway_tenant(api, api.a.id)
    await _accept(api, settings, tmp_path)
    meanwhile = {"tenants": {"gw-b": {"teams": {"ops": {"monthly_spend_cap_usd": 5}}}}}
    gateway.interleave = [meanwhile]
    report = await _apply(api, gateway, settings)
    assert report.status == "applied"
    assert gateway.calls.count("write") == 2  # the first one met a 409
    assert gateway.current["tenants"]["gw-b"] == meanwhile["tenants"]["gw-b"]
    assert gateway.current["tenants"][GW] == _expected_plan().tenant


async def test_a_gateway_that_keeps_moving_is_retried_later(api, settings, gateway, tmp_path):
    await _gateway_tenant(api, api.a.id)
    await _accept(api, settings, tmp_path)
    gateway.interleave = [{"teams": {}}, {"teams": {}}, {"teams": {}}]
    report = await _apply(api, gateway, settings)
    assert report.status == "pending"
    (row,) = await _rows(api)
    assert "kept changing" in row.last_error


async def test_invalid_fragment_is_terminal(api, settings, gateway, tmp_path):
    await _gateway_tenant(api, api.a.id)
    await _accept(api, settings, tmp_path)
    gateway.unknown_models = {"qwen-coder"}
    report = await _apply(api, gateway, settings)
    assert report.status == "invalid"
    (row,) = await _rows(api)
    assert row.status == "invalid" and any("qwen-coder" in p for p in row.problems)
    refused = (await _events(api))[-1]
    assert refused.event == "routing.refused" and "qwen-coder" in refused.reason
    api.clock.advance(timedelta(hours=3))
    assert await _apply(api, gateway, settings) is None  # never retried
    assert gateway.version == 0


async def test_backoff_then_failed(api, settings, gateway, tmp_path):
    settings = settings.model_copy(update={"routing_max_attempts": 3})
    await _gateway_tenant(api, api.a.id)
    await _accept(api, settings, tmp_path)
    gateway.down = True
    assert (await _apply(api, gateway, settings)).status == "pending"
    (row,) = await _rows(api)
    assert row.attempts == 1 and row.next_attempt_at == api.clock.now + timedelta(minutes=1)
    assert await _apply(api, gateway, settings) is None  # not due yet
    api.clock.advance(timedelta(minutes=1))
    assert (await _apply(api, gateway, settings)).status == "pending"
    (row,) = await _rows(api)
    assert row.next_attempt_at == api.clock.now + timedelta(minutes=2)
    api.clock.advance(timedelta(minutes=2))
    assert (await _apply(api, gateway, settings)).status == "failed"
    (row,) = await _rows(api)
    assert row.status == "failed" and "unreachable" in row.last_error
    assert [e.event for e in await _events(api)] == ["routing.queued", "routing.apply_failed"]


async def test_newer_activation_supersedes_pending(api, settings, gateway, tmp_path):
    await _gateway_tenant(api, api.a.id)
    first = await _accept(api, settings, tmp_path, name="one")

    def cheaper(policy: Path) -> None:
        tiers = policy / "tiers.yaml"
        tiers.write_text(
            tiers.read_text(encoding="utf-8").replace("vendor-b-fast", "vendor-b-faster"),
            encoding="utf-8",
        )

    api.clock.advance(timedelta(seconds=1))
    second = await _accept(api, settings, tmp_path, name="two", edit=cheaper)
    assert [(r.policy_version, r.status) for r in await _rows(api)] == [
        (first, "superseded"),
        (second, "pending"),
    ]
    report = await _apply(api, gateway, settings)
    assert report.policy_version == second and report.status == "applied"
    rules = gateway.current["tenants"][GW]["groups"]["G6_routing"]["rules"]
    assert any(rule["action"]["model"] == "vendor-b-faster" for rule in rules)


async def test_readback_mismatch_is_not_applied(api, settings, gateway, tmp_path):
    await _gateway_tenant(api, api.a.id)
    await _accept(api, settings, tmp_path)
    gateway.tamper_readback = True
    report = await _apply(api, gateway, settings)
    assert report.status == "pending"
    (row,) = await _rows(api)
    assert row.status == "pending" and "read back" in row.last_error
    assert row.gateway_version is None


async def _sync(api, settings, tmp_path, edit):
    repo = _policy_repo(tmp_path / "repo", edit)
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            update(tenants).where(tenants.c.id == api.a.id).values(policy_repo_url=url(repo))
        )
    return await sync_once(api.db.app, GitRunner(settings), api.a.id, settings, api.clock)


@pytest.mark.parametrize("budget", [".inf", ".nan"])
async def test_a_budget_that_is_not_a_number_is_rejected(api, settings, tmp_path, budget):
    """Review F1: such a policy used to wedge the tenant's sync (OverflowError, or NaN the
    database refused); it is now a recorded rejection naming the field."""

    def edit(root: Path) -> None:
        team = root / "teams" / "search.yaml"
        text = team.read_text(encoding="utf-8")
        team.write_text(text.replace("512.5", budget), encoding="utf-8")

    outcome = await _sync(api, settings, tmp_path, edit)
    assert outcome.kind == "rejected", outcome
    assert any("monthly_budget_usd" in problem for problem in outcome.problems)
    assert await _rows(api) == []


async def test_a_routing_generator_failure_rejects_the_version(
    api, settings, tmp_path, monkeypatch
):
    """Review F1: a generator bug becomes a rejection with its problem recorded, never an
    exception that the worker retries for ever."""

    def explode(policy):
        raise ValueError("teams.search: cannot be sent")

    monkeypatch.setattr("agentic_cp.policy_sync.service.routing_plan", explode)
    outcome = await _sync(api, settings, tmp_path, None)
    assert outcome.kind == "rejected", outcome
    assert any("routing" in p and "teams.search" in p for p in outcome.problems)
    assert await _rows(api) == []


async def test_no_gateway_tenant_is_skipped(api, settings, gateway, tmp_path):
    await _gateway_tenant(api, api.a.id, None)
    await _accept(api, settings, tmp_path)
    assert (await _apply(api, gateway, settings)).status == "skipped"
    assert gateway.calls == []
    assert (await _events(api))[-1].event == "routing.skipped"


async def test_per_team_limits_are_written_and_removed(api, settings, gateway, tmp_path):
    settings = settings.model_copy(update={"gateway_team_rate_limits": True})
    await _gateway_tenant(api, api.a.id)
    await _accept(api, settings, tmp_path, name="one")
    await _apply(api, gateway, settings)
    per_team = gateway.current["rate_limit"]["per_team"]
    assert per_team == {"search": {"requests_per_minute": 120, "requests_per_hour": 3000}}
    assert (await _rows(api))[-1].per_team == ["search"]

    def no_limits(policy: Path) -> None:
        (policy / "teams" / "search.yaml").write_text("monthly_budget_usd: 512.5\n", "utf-8")

    api.clock.advance(timedelta(seconds=1))
    await _accept(api, settings, tmp_path, name="two", edit=no_limits)
    await _apply(api, gateway, settings)
    assert "rate_limit" not in gateway.current


async def test_per_team_never_overwrites_an_unowned_entry(api, settings, gateway, tmp_path):
    settings = settings.model_copy(update={"gateway_team_rate_limits": True})
    await _gateway_tenant(api, api.a.id)
    theirs = {"rate_limit": {"per_team": {"search": {"requests_per_minute": 7}}}}
    gateway.store(theirs)
    await _accept(api, settings, tmp_path)
    report = await _apply(api, gateway, settings)
    assert report.status == "applied"
    assert gateway.current["rate_limit"] == theirs["rate_limit"]
    (row,) = await _rows(api)
    assert row.per_team == [] and any("search" in w for w in row.warnings)


async def test_job_holds_no_tenant_lock_while_the_gateway_waits(api, settings, gateway, tmp_path):
    await _gateway_tenant(api, api.a.id)
    await _accept(api, settings, tmp_path)
    gateway.hold = "write"
    task = asyncio.ensure_future(_apply(api, gateway, settings))
    try:
        await asyncio.wait_for(gateway.entered.wait(), PROMPT)
        other = await asyncio.wait_for(
            assign(api, api.a.admin_token, "other@a.example.test", "developer", "search"), PROMPT
        )
        assert other.status_code == 201, other.text
    finally:
        gateway.release.set()
    assert (await task).status == "applied"


async def test_the_worker_job_uses_the_deployment_gateway(
    api, settings, gateway, tmp_path, monkeypatch
):
    await _gateway_tenant(api, api.a.id)
    await _accept(api, settings, tmp_path)
    monkeypatch.setattr(routing_apply, "gateway_routing_for", lambda s: gateway)
    await routing_job(api.db.app, None, api.a.id, settings, api.clock)
    assert (await _rows(api))[-1].status == "applied"


async def test_admin_key_never_reaches_the_audit_chain(api, settings, tmp_path):
    """End to end over the HTTP port: the key goes in the bearer header and nowhere else."""
    await _gateway_tenant(api, api.a.id)
    await _accept(api, settings, tmp_path)
    overlay = {"version": 0, "fragment": {}}
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["Authorization"])
        if request.method == "PUT":
            body = json.loads(request.content)
            overlay.update(version=overlay["version"] + 1, fragment=body["fragment"])
        return httpx2.Response(200, json=copy.deepcopy(overlay))

    gateway = TokenLeanRouting(
        "https://gateway.example.test", ADMIN_KEY, transport=httpx2.MockTransport(handler)
    )
    assert (await _apply(api, gateway, settings)).status == "applied"
    assert seen and all(value == f"Bearer {ADMIN_KEY}" for value in seen)
    async with tenant_tx(api.db.app, api.a.id) as conn:
        records = await read_records(conn)
    assert ADMIN_KEY not in json.dumps([r.entry() for r in records])
    assert ADMIN_KEY not in json.dumps([dict(r._mapping) for r in await _rows(api)], default=str)


async def test_another_tenants_applies_are_invisible(api, settings, gateway, tmp_path):
    await _gateway_tenant(api, api.a.id)
    await _accept(api, settings, tmp_path)
    assert await _rows(api, api.b.id) == []
    assert await apply_due(api.db.app, api.b.id, gateway, settings, api.clock) is None
