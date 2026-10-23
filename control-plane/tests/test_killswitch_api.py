"""The kill-switch API (M2-06, M2-10; Review Focus 1 and 2): who may set and clear which
switches, what a run's CLI is told, and the audit record of every change."""

import pytest
from apiclient import member
from policy_repos import make_policy_repo, url

pytestmark = pytest.mark.anyio

TEAM_SWITCH = {"scope_kind": "team", "scope_value": "payments", "reason": "incident 42"}


async def _set(api, token, **body):
    return await api.post("/v1/killswitch", token, {"reason": "incident 42", **body})


async def _audit(api, tenant):
    reply = await api.get("/v1/audit", tenant.admin_token)
    assert reply.status_code == 200, reply.text
    return reply.json()


async def test_org_admin_sets_and_clears_a_tenant_switch(api):
    made = await _set(api, api.a.admin_token, scope_kind="tenant")
    assert made.status_code == 201, made.text
    info = made.json()
    assert info["scope_kind"] == "tenant" and info["mode"] == "kill" and info["active"]
    assert info["created_by"] == "admin@a.example.test" and info["team"] is None
    cleared = await api.delete(f"/v1/killswitch/{info['id']}", api.a.admin_token)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["active"] is False
    assert cleared.json()["cleared_by"] == "admin@a.example.test"
    again = await api.delete(f"/v1/killswitch/{info['id']}", api.a.admin_token)
    assert again.status_code == 409 and again.json()["code"] == "killswitch.cleared"


async def test_team_lead_limited_to_own_team(api):
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    own = await _set(api, lead, **{k: v for k, v in TEAM_SWITCH.items() if k != "reason"})
    assert own.status_code == 201, own.text
    assert own.json()["team"] == "payments"
    qualified = await _set(api, lead, scope_kind="model", scope_value="acme/m", team="payments")
    assert qualified.status_code == 201, qualified.text
    for body in (
        {"scope_kind": "tenant"},
        {"scope_kind": "team", "scope_value": "search"},
        {"scope_kind": "model", "scope_value": "acme/m"},  # unqualified: tenant-wide
        {"scope_kind": "model", "scope_value": "acme/m", "team": "search"},
    ):
        refused = await _set(api, lead, **body)
        assert refused.status_code == 403, (body, refused.text)
        assert refused.json()["code"] == "rbac.forbidden"
    other = await _set(api, api.a.admin_token, scope_kind="team", scope_value="search")
    clear_other = await api.delete(f"/v1/killswitch/{other.json()['id']}", lead)
    assert clear_other.status_code == 403
    tenant_wide = await _set(api, api.a.admin_token, scope_kind="tenant")
    assert (await api.delete(f"/v1/killswitch/{tenant_wide.json()['id']}", lead)).status_code == 403
    assert (await api.delete(f"/v1/killswitch/{own.json()['id']}", lead)).status_code == 200


async def test_developer_cannot_set_or_list_switches(api):
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    assert (await _set(api, dev, scope_kind="team", scope_value="payments")).status_code == 403
    listed = await api.get("/v1/killswitch", dev)
    assert listed.status_code == 403 and listed.json()["code"] == "rbac.forbidden"


async def test_scope_values_are_validated(api):
    token = api.a.admin_token
    for body in (
        {"scope_kind": "team", "scope_value": "Payments"},
        {"scope_kind": "tenant", "scope_value": "x"},
        {"scope_kind": "user", "scope_value": "not-an-email"},
        {"scope_kind": "agent", "scope_value": ""},
        {"scope_kind": "team", "scope_value": "payments", "team": "search"},
        {"scope_kind": "repo", "scope_value": "github.com/acme/*"},  # review F5
        {"scope_kind": "model", "scope_value": "acme/*"},
    ):
        refused = await _set(api, token, **body)
        assert refused.status_code == 422, (body, refused.text)
        assert refused.json()["code"] == "killswitch.invalid_scope"


async def test_every_change_is_audited(api):
    made = await _set(api, api.a.admin_token, scope_kind="pipeline", scope_value="deliver")
    switch = made.json()["id"]
    await api.delete(f"/v1/killswitch/{switch}?reason=fixed", api.a.admin_token)
    records = [r for r in await _audit(api, api.a) if r["event"].startswith("killswitch.")]
    assert [r["event"] for r in records] == ["killswitch.set", "killswitch.cleared"]
    set_record, cleared = records
    assert set_record["actor"] == "admin@a.example.test"
    assert set_record["target"] == f"killswitch:{switch}"
    assert set_record["reason"] == "incident 42"
    assert set_record["after"] == {
        "scope_kind": "pipeline",
        "scope_value": "deliver",
        "team": None,
        "mode": "kill",
    }
    assert cleared["reason"] == "fixed" and cleared["before"]["mode"] == "kill"
    verified = await api.get("/v1/audit/verify", api.a.admin_token)
    assert verified.status_code == 200 and verified.json()["problems"] == []
    assert not [r for r in await _audit(api, api.b) if r["event"].startswith("killswitch.")]


async def test_state_gives_a_run_the_switches_that_may_apply(api):
    token = api.a.admin_token
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    wanted = [
        await _set(api, token, scope_kind="tenant", mode="pause"),
        await _set(api, token, scope_kind="team", scope_value="payments"),
        await _set(api, token, scope_kind="user", scope_value="dev@a.example.test"),
        await _set(api, token, scope_kind="repo", scope_value="github.com/acme/app"),
        await _set(api, token, scope_kind="pipeline", scope_value="deliver"),
        await _set(api, token, scope_kind="agent", scope_value="developer"),
        await _set(api, token, scope_kind="model", scope_value="acme/m", team="payments"),
    ]
    unwanted = [
        await _set(api, token, scope_kind="team", scope_value="search"),
        await _set(api, token, scope_kind="user", scope_value="other@a.example.test"),
        await _set(api, token, scope_kind="pipeline", scope_value="hotfix"),
        await _set(api, token, scope_kind="backend", scope_value="native", team="search"),
    ]
    cleared = await _set(api, token, scope_kind="team", scope_value="payments")
    await api.delete(f"/v1/killswitch/{cleared.json()['id']}", token)
    query = "team=payments&repo=github.com/acme/app&pipeline=deliver"
    state = await api.get(f"/v1/killswitch/state?{query}", dev)
    assert state.status_code == 200, state.text
    body = state.json()
    assert body["ttl_seconds"] == 10 and body["checked_at"]
    got = {s["id"] for s in body["switches"]}
    assert got == {r.json()["id"] for r in wanted}
    assert not got & {r.json()["id"] for r in unwanted}


async def test_state_of_tenant_b_never_shows_a_switch_of_a(api):
    await _set(api, api.a.admin_token, scope_kind="tenant")
    state = await api.get("/v1/killswitch/state?team=payments", api.b.admin_token)
    assert state.status_code == 200 and state.json()["switches"] == []


async def test_list_shows_what_the_caller_may_clear(api):
    token = api.a.admin_token
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    own = await _set(api, token, scope_kind="team", scope_value="payments")
    other = await _set(api, token, scope_kind="team", scope_value="search")
    done = await _set(api, token, scope_kind="tenant")
    await api.delete(f"/v1/killswitch/{done.json()['id']}", token)
    as_lead = {s["id"] for s in (await api.get("/v1/killswitch", lead)).json()}
    assert as_lead == {own.json()["id"]}
    as_admin = {s["id"] for s in (await api.get("/v1/killswitch", token)).json()}
    assert as_admin == {own.json()["id"], other.json()["id"]}
    everything = await api.get("/v1/killswitch?include_cleared=true", token)
    assert done.json()["id"] in {s["id"] for s in everything.json()}


async def test_unknown_team_refused_when_a_policy_names_the_teams(api, tmp_path):
    repo = make_policy_repo(tmp_path / "policy")
    source = {"repo": url(repo), "branch": "main"}
    synced = await api.put("/v1/tenant/policy-source", api.a.admin_token, source)
    assert synced.status_code == 200, synced.text
    refused = await _set(api, api.a.admin_token, scope_kind="team", scope_value="nosuchteam")
    assert refused.status_code == 422 and refused.json()["code"] == "killswitch.unknown_team"
