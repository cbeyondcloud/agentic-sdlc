"""The policy API (SP15 Task 7, M2-02): the caller's tenant's active policy, versions and
bundles; manual sync; the per-tenant webhook (ND-14); the team guard (DF-404)."""

from __future__ import annotations

import hashlib

import pytest
from apiclient import assign, member
from policy_repos import break_tiers, commit, head, make_policy_repo, url, write
from sqlalchemy import select

from agentic.orgapi import WEBHOOK_TOKEN_PREFIX
from agentic_cp.tables import tenants
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.ratelimit import RateDecision

pytestmark = pytest.mark.anyio


async def configure(api, tenant, repo, branch="main"):
    reply = await api.put(
        "/v1/tenant/policy-source", tenant.admin_token, {"repo": url(repo), "branch": branch}
    )
    assert reply.status_code == 200, reply.text
    return reply


async def test_active_policy_requires_authentication(api):
    reply = await api.get("/v1/policy/active")
    assert reply.status_code == 401
    assert reply.json()["code"] == "auth.missing_token"


async def test_active_policy_before_first_sync_is_policy_none(api):
    reply = await api.get("/v1/policy/active", api.a.admin_token)
    assert reply.status_code == 404 and reply.json()["code"] == "policy.none"


async def test_policy_source_change_syncs_and_active_is_served(api, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    reply = await configure(api, api.a, repo)
    assert reply.json()["policy_repo"] == url(repo)
    active = await api.get("/v1/policy/active", api.a.admin_token)
    assert active.status_code == 200, active.text
    body = active.json()
    assert body["version"] == "git:" + head(repo)
    assert body["bundle_path"] == f"/v1/policy/versions/{body['version']}/bundle"
    developer, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    assert (await api.get("/v1/policy/active", developer)).json() == body


async def test_manual_sync_requires_policy_edit_org(api, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    await configure(api, api.a, repo)
    developer, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    refused = await api.post("/v1/policy/sync", developer)
    assert refused.status_code == 403 and refused.json()["code"] == "rbac.forbidden"
    done = await api.post("/v1/policy/sync", api.a.admin_token)
    assert done.status_code == 200 and done.json()["kind"] == "unchanged"


async def test_versions_list_and_bundle_download(api, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    await configure(api, api.a, repo)
    break_tiers(repo)
    rejected = await api.post("/v1/policy/sync", api.a.admin_token)
    assert rejected.json()["kind"] == "rejected"
    versions = (await api.get("/v1/policy/versions", api.a.admin_token)).json()
    assert [v["status"] for v in versions] == ["rejected", "accepted"]
    assert versions[1]["active"] and not versions[0]["active"]
    active = (await api.get("/v1/policy/active", api.a.admin_token)).json()
    bundle = await api.get(active["bundle_path"], api.a.admin_token)
    assert bundle.status_code == 200
    assert bundle.headers["content-type"] == "application/gzip"
    assert bundle.headers["etag"] == f'"{active["digest"]}"'
    assert "immutable" in bundle.headers["cache-control"]
    assert "sha256:" + hashlib.sha256(bundle.content).hexdigest() == active["digest"]
    missing = await api.get(
        f"/v1/policy/versions/{versions[0]['version']}/bundle", api.a.admin_token
    )
    assert missing.status_code == 404  # a rejected version has no bundle


async def test_webhook_token_resolves_only_its_tenant(api, tmp_path):
    repo_a = make_policy_repo(tmp_path / "policy-a")
    repo_b = make_policy_repo(tmp_path / "policy-b")
    await configure(api, api.a, repo_a)
    await configure(api, api.b, repo_b)
    issued = await api.post("/v1/tenant/webhook-token", api.a.admin_token)
    assert issued.status_code == 201, issued.text
    path = issued.json()["path"]
    token = path.rsplit("/", 1)[1]
    assert token.startswith(WEBHOOK_TOKEN_PREFIX)
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        stored = (
            await conn.execute(
                select(tenants.c.webhook_token_sha256).where(tenants.c.id == api.a.id)
            )
        ).scalar_one()
    assert stored == hashlib.sha256(token.encode()).digest()
    write(repo_a, "policy/teams/ops.yaml", "{}\n")
    new_a = commit(repo_a, "ops team")
    write(repo_b, "policy/teams/ops.yaml", "{}\n")
    commit(repo_b, "ops team in b")
    hooked = await api.post(path, None, {"anything": "ignored"})
    assert hooked.status_code == 202, hooked.text
    assert (await api.get("/v1/policy/active", api.a.admin_token)).json()["version"] == (
        "git:" + new_a
    )
    b_active = (await api.get("/v1/policy/active", api.b.admin_token)).json()["version"]
    assert b_active != "git:" + head(repo_b)  # b was not synced by a's hook
    unknown = await api.post(f"/v1/policy/webhook/{WEBHOOK_TOKEN_PREFIX}{'x' * 43}")
    assert unknown.status_code == 404
    rotated = await api.post("/v1/tenant/webhook-token", api.a.admin_token)
    assert rotated.status_code == 201
    assert (await api.post(path)).status_code == 404  # the old token stops working
    assert (await api.post(rotated.json()["path"])).status_code == 202


async def test_webhook_rotation_requires_policy_edit_org_and_is_audited(api):
    developer, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    assert (await api.post("/v1/tenant/webhook-token", developer)).status_code == 403
    issued = await api.post("/v1/tenant/webhook-token", api.a.admin_token)
    records = (await api.get("/v1/audit", api.a.admin_token)).json()
    assert records[-1]["event"] == "tenant.webhook_rotated"
    assert issued.json()["path"].rsplit("/", 1)[1] not in str(records)


async def test_access_log_never_shows_a_webhook_token(api):
    """Review F2: uvicorn's access log line carries the path; the token is masked."""
    import logging

    secret = WEBHOOK_TOKEN_PREFIX + "s" * 43
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "POST", f"/v1/policy/webhook/{secret}?x=1", "1.1", 202),
        None,
    )
    for handler_filter in logging.getLogger("uvicorn.access").filters:
        handler_filter.filter(record)
    assert secret not in record.getMessage()
    assert "/v1/policy/webhook/<redacted>" in record.getMessage()


class RefuseWebhooksOf:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id

    async def check(self, tenant_id, principal_id, route):
        if tenant_id == self.tenant_id and route == "webhook":
            return RateDecision(allowed=False, retry_after=30)
        return RateDecision(allowed=True)


async def test_webhook_is_rate_limited_per_tenant(api):
    limited = api.with_app(rate_limiter=RefuseWebhooksOf(api.a.id))
    path_a = (await limited.post("/v1/tenant/webhook-token", api.a.admin_token)).json()["path"]
    path_b = (await limited.post("/v1/tenant/webhook-token", api.b.admin_token)).json()["path"]
    refused = await limited.post(path_a)
    assert refused.status_code == 429 and refused.headers["retry-after"] == "30"
    assert (await limited.post(path_b)).status_code == 202
    await limited.http.aclose()


async def test_assignment_to_unknown_team_refused(api, tmp_path):
    before = await assign(api, api.a.admin_token, "x@a.example.test", "developer", "ghosts")
    assert before.status_code == 201  # no active policy yet: any team slug (R5)
    repo = make_policy_repo(tmp_path / "policy-a", teams=("payments",))
    await configure(api, api.a, repo)
    refused = await assign(api, api.a.admin_token, "y@a.example.test", "developer", "billing")
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "rbac.unknown_team"
    assert "policy/teams/billing.yaml" in refused.json()["hint"]
    known = await assign(api, api.a.admin_token, "y@a.example.test", "developer", "payments")
    assert known.status_code == 201
    org = await assign(api, api.a.admin_token, "z@a.example.test", "auditor")
    assert org.status_code == 201  # org scope needs no team
