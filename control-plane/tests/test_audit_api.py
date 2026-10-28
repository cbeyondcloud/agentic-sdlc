"""Reading the audit chain over the API: only with an audit permission, only the caller's
tenant, and a verifier that answers [] for an intact chain."""

import pytest
from apiclient import member

pytestmark = pytest.mark.anyio


async def test_audit_read_needs_audit_read_permission(api):
    developer, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    auditor, _ = await member(api, api.a, "aud@a.example.test", "auditor")
    refused = await api.get("/v1/audit", developer)
    assert refused.status_code == 403
    assert refused.json()["code"] == "rbac.forbidden"
    listed = await api.get("/v1/audit", auditor)
    assert listed.status_code == 200
    records = listed.json()
    assert records and {r["tenant"] for r in records} == {str(api.a.id)}
    assert [r["seq"] for r in records] == list(range(1, len(records) + 1))
    verified = await api.get("/v1/audit/verify", auditor)
    assert verified.status_code == 200
    assert verified.json() == {"problems": []}
    assert (await api.get("/v1/audit/verify", developer)).status_code == 403


async def test_audit_list_pages_by_seq(api):
    first = (await api.get("/v1/audit?limit=2", api.a.admin_token)).json()
    assert [r["seq"] for r in first] == [1, 2]
    rest = (await api.get("/v1/audit?after_seq=2&limit=2", api.a.admin_token)).json()
    assert [r["seq"] for r in rest] == [3, 4][: len(rest)]
    assert (await api.get("/v1/audit?limit=0", api.a.admin_token)).status_code == 422


async def test_team_lead_reads_only_their_teams_records(api):
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    assert (await api.get("/v1/audit", lead)).status_code == 403
    own = await api.get("/v1/audit?team=payments", lead)
    assert own.status_code == 200
    assert all(r["team"] == "payments" for r in own.json())
    assert any(r["event"] == "role.assigned" for r in own.json())
    other = await api.get("/v1/audit?team=billing", lead)
    assert other.status_code == 403
