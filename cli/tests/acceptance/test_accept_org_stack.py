"""The org stack the M2 acceptance scenarios run on (M2-53, M2-55) is the merged control plane,
unchanged: brought up by its own operator commands, serving over HTTP with forced row-level
security under the runtime role, tenant-bound tokens and the per-tenant audit chain; its
policy sync reads a real git repo and its routing apply writes the gateway's overlay
(FakeRouting). Nothing is stubbed between a client and the API but the network address."""

import httpx2
from _acceptance_support import HOST, MODELS, sandbox_available
from _org_stack import ADMIN, GATEWAY_TENANT, TENANT, org_policy_repo, policy_git

from agentic.policy import gateway_model


def test_the_org_stack_is_the_real_control_plane(org, tmp_path):
    ready = httpx2.get(org.url + "/readyz", trust_env=False).json()
    # forced RLS, insert-only audit; whether a gateway is linked is informational (M3-01)
    assert {k: ready[k] for k in ("status", "db_enforced")} == {
        "status": "ready",
        "db_enforced": True,
    }
    assert isinstance(ready["gateway_linked"], bool)
    me = org.ok("GET", "/v1/me")
    assert (me["tenant"]["slug"], me["user"]["email"]) == (TENANT, ADMIN)

    repo = org_policy_repo(
        tmp_path / "policy-repo", MODELS, sandbox=sandbox_available(), hosts=f"{HOST}/acme/*"
    )
    head = policy_git(repo, "rev-parse", "HEAD")  # the branch head is what syncs
    outcome = org.connect_policy(repo)
    assert (outcome["kind"], outcome["version"]) == ("activated", f"git:{head}"), outcome
    assert org.ok("GET", "/v1/policy/active")["version"] == f"git:{head}"

    report = org.apply_routing()  # the worker's job: the overlay at the gateway follows
    assert report is not None and report.status == "applied", report
    assert org.tier_models()["balanced"] == gateway_model(MODELS["change-me/balanced-model"])
    assert set(org.routing.current["tenants"]) == {GATEWAY_TENANT}  # only this tenant's part

    events = [record["event"] for record in org.audit()]
    assert events[0] == "tenant.created"
    assert {"policy.activated", "routing.queued", "routing.applied"} <= set(events)

    # tenant-bound tokens: another tenant's admin sees none of acme's objects
    argv = "admin tenant create --slug globex --name Globex --admin-email admin@globex.example"
    other = org.operator(*argv.split()).strip().splitlines()[-1]
    assert org.api("GET", "/v1/policy/active", other).status_code == 404
    theirs = org.ok("GET", "/v1/audit", other)  # its own chain, from seq 1
    assert theirs[0]["seq"] == 1 and theirs[0]["event"] == "tenant.created"
    assert str(org.tenant_id) not in {record["tenant"] for record in theirs}
