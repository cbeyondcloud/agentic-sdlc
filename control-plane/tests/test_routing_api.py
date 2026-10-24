"""`/v1/routing` (M2-09): org admins and auditors see which gateway version applied which
policy version; an org admin queues the active version again (after a failed or refused
apply, or once the gateway is configured). Another tenant's applies never show."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from apiclient import member
from policy_repos import make_policy_repo, url
from pydantic import SecretStr
from sqlalchemy import select

from agentic_cp.audit import read_records
from agentic_cp.tables import routing_applies
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


@pytest.fixture
async def org(api):
    harness = api.with_settings(
        gateway_admin_url="https://gateway.example.test",
        gateway_admin_key=SecretStr("tl-admin-" + "x" * 24),  # gitleaks:allow
    )
    try:
        yield harness
    finally:
        await harness.http.aclose()


async def _activate_in(api, tenant) -> str:
    repo = make_policy_repo(Path(tempfile.mkdtemp(prefix="cp-routing-")) / "policy")
    made = await api.put(
        "/v1/tenant/policy-source", tenant.admin_token, {"repo": url(repo), "branch": "main"}
    )
    assert made.status_code == 200 and made.json()["sync"]["kind"] == "activated", made.text
    return made.json()["sync"]["version"]


async def test_an_admin_lists_the_applies(org):
    version = await _activate_in(org, org.a)
    listed = await org.get("/v1/routing", org.a.admin_token)
    assert listed.status_code == 200, listed.text
    (row,) = listed.json()
    assert row["policy_version"] == version and row["status"] == "pending"
    assert row["requested_by"] == "admin@a.example.test"


async def test_an_auditor_reads_and_a_developer_does_not(org):
    await _activate_in(org, org.a)
    auditor, _ = await member(org, org.a, "auditor@a.example.test", "auditor")
    developer, _ = await member(org, org.a, "dev@a.example.test", "developer", "payments")
    assert (await org.get("/v1/routing", auditor)).status_code == 200
    refused = await org.get("/v1/routing", developer)
    assert refused.status_code == 403 and refused.json()["code"] == "rbac.forbidden"


async def test_an_admin_queues_the_active_version_again(org):
    version = await _activate_in(org, org.a)
    queued = await org.post("/v1/routing/apply", org.a.admin_token)
    assert queued.status_code == 202, queued.text
    assert queued.json()["policy_version"] == version and queued.json()["status"] == "pending"
    async with tenant_tx(org.db.app, org.a.id) as conn:
        rows = list(await conn.execute(select(routing_applies.c.status)))
        events = [r for r in await read_records(conn) if r.event == "routing.queued"]
    assert sorted(r.status for r in rows) == ["pending", "superseded"]
    assert events[-1].actor == "admin@a.example.test"


async def test_queueing_needs_policy_edit_org(org):
    await _activate_in(org, org.a)
    auditor, _ = await member(org, org.a, "auditor@a.example.test", "auditor")
    assert (await org.post("/v1/routing/apply", auditor)).status_code == 403


async def test_queueing_needs_an_active_policy_and_a_gateway(api, org):
    none = await org.post("/v1/routing/apply", org.a.admin_token)
    assert none.status_code == 409 and none.json()["code"] == "routing.no_policy"
    await _activate_in(api, api.a)  # the default test settings name no gateway admin
    unconfigured = await api.post("/v1/routing/apply", api.a.admin_token)
    assert unconfigured.status_code == 409
    assert unconfigured.json()["code"] == "routing.no_gateway"


async def test_another_tenants_applies_never_show(org):
    await _activate_in(org, org.b)
    listed = await org.get("/v1/routing", org.a.admin_token)
    assert listed.status_code == 200 and listed.json() == []
