"""The CLI against the real control plane (SP15 Task 10/11 contract tests): the app runs
under uvicorn on 127.0.0.1 in a thread with its own engine and event loop, two tenants are
bootstrapped, and the CLI's own client, policy source and sink talk to it over HTTP."""

from __future__ import annotations

import threading
import time

import httpx2
import pytest
import uvicorn
from apiclient import FakeClock, bootstrap, settings_for
from policy_repos import commit, head, make_policy_repo, url, write

from agentic.orgapi import API_PREFIX
from agentic.telemetry.sinks import ForwardingRecorder
from agentic_cli.control_plane import ControlPlaneClient, ControlPlaneSink
from agentic_cli.policy_source import resolve_policy
from agentic_cp.app import create_app
from sdlc_commons import ledger as ledger_module
from sdlc_commons.ledger import RunIdentity, RunLedger

pytestmark = pytest.mark.anyio


class LiveServer:
    def __init__(self, app) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("the control plane did not start")
            time.sleep(0.05)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=30)


@pytest.fixture
async def live(db, monkeypatch):
    """(base URL, tenant a, tenant b) for a control plane serving `db`."""
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})

    clock = FakeClock()
    a = await bootstrap(db, clock, "tenant-a", "Tenant A")
    b = await bootstrap(db, clock, "tenant-b", "Tenant B")
    with LiveServer(create_app(settings_for(db))) as base:
        yield base, a, b


async def test_login_me_roundtrip(live):
    base, a, b = live
    with ControlPlaneClient(base, a.admin_token) as client:
        me = client.me()
    assert me.tenant.slug == "tenant-a" and me.user.email == "admin@a.example.test"
    with ControlPlaneClient(base, b.admin_token) as client:
        assert client.me().tenant.slug == "tenant-b"


def _set_source(base: str, token: str, repo) -> dict:
    with httpx2.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}) as http:
        reply = http.put(
            f"{API_PREFIX}/tenant/policy-source", json={"repo": url(repo), "branch": "main"}
        )
    assert reply.status_code == 200, reply.text
    return reply.json()


async def test_policy_fetch_end_to_end(live, tmp_path, monkeypatch):
    base, a, b = live
    repo_a = make_policy_repo(tmp_path / "policy-a")
    repo_b = make_policy_repo(tmp_path / "policy-b")
    write(repo_b, "policy/teams/billing.yaml", "{}\n")
    commit(repo_b, "billing")
    assert _set_source(base, a.admin_token, repo_a)["sync"]["kind"] == "activated"
    assert _set_source(base, b.admin_token, repo_b)["sync"]["kind"] == "activated"
    for tenant, repo in ((a, repo_a), (b, repo_b)):
        home = tmp_path / f"home-{tenant.slug}"
        env = {"AGENTIC_CONTROL_PLANE_URL": base, "AGENTIC_CONTROL_PLANE_TOKEN": tenant.admin_token}
        loaded = resolve_policy(
            standalone=False, policy_path=None, home=home, config={"org": "acme"}, env=env
        )
        try:
            assert loaded.policy.version == "git:" + head(repo)
            assert loaded.me.tenant.slug == tenant.slug
            loaded.commit()
        finally:
            loaded.discard()
        assert (home / "policy" / "acme" / "repo" / "policy").is_dir()


async def test_kill_switch_and_registration_end_to_end(live, tmp_path):
    """M2-06/M2-19/M2-21: the CLI's port sees a switch the admin sets through the CLI's own
    client, scoped to its team; registration answers every org check; tenant b sees none."""
    from agentic.killswitch import RunScope
    from agentic_cli.killswitch import ControlPlaneKillSwitch

    base, a, b = live
    repo = make_policy_repo(tmp_path / "policy")
    version = _set_source(base, a.admin_token, repo)["sync"]["version"]
    scope = RunScope(team="payments", pipeline="deliver", agent="developer@1.0.0")
    with ControlPlaneClient(base, a.admin_token) as client:
        port = ControlPlaneKillSwitch(
            client, user="admin@a.example.test", team="payments", repo=None
        )
        assert port.check(scope).action == "run"
        made = client.set_switch(
            {"scope_kind": "agent", "scope_value": "developer", "reason": "bad release"}
        )
        fresh = ControlPlaneKillSwitch(client, user=None, team="payments", repo=None)
        verdict = fresh.check(scope)
        assert verdict.action == "kill" and "bad release" in verdict.describe()
        body = {
            "run_id": "contract-reg",
            "team": "payments",
            "pipeline": "deliver",
            "policy_version": version,
            "agents": ["developer@1.0.0"],
            "dry_run": True,
        }
        answer = client.register_run(body)
        failed = {c.check for c in answer.checks if not c.passed}
        assert "killswitch" in failed and not answer.registered
        assert answer.team_budget_remaining_usd is None
        assert not client.clear_switch(str(made.id), "fixed").active
    with ControlPlaneClient(base, b.admin_token) as client:
        assert client.kill_state(team="payments", repo=None, pipeline="deliver").switches == []


async def test_ledger_forward_to_control_plane_end_to_end(live, tmp_path):
    base, a, b = live
    ledger = RunLedger(tmp_path / "repo", "contract-run")
    ledger.open(
        identity=RunIdentity(team="payments", policy_version="git:" + "c" * 40),
        kind="feature",
        project="demo",
        owner="payments",
        criticality="low",
    )
    with ControlPlaneClient(base, a.admin_token) as client:
        recorder = ForwardingRecorder(ledger, [ControlPlaneSink(client)])
        recorder.flush()
        for n in range(3):
            recorder.record("step_started", phase="requirements", attempt=n + 1)
        local = ledger.events()
        summary = client.run("contract-run")
        assert (summary.head.seq, summary.head.hash) == (len(local), local[-1]["hash"])
        cursor = ledger.run_dir / "sinks" / "control-plane.json"
        cursor.unlink()  # a lost cursor: everything goes again, as duplicates
        result = client.ingest("contract-run", local)
        assert result.accepted == 0 and result.duplicates == len(local)
        assert recorder.flush() == {"control-plane": len(local)}
        assert recorder.pending() == {"control-plane": 0}
    ledger.release()
    with httpx2.Client(base_url=base, headers={"Authorization": f"Bearer {b.admin_token}"}) as http:
        assert http.get(f"{API_PREFIX}/runs/contract-run").status_code == 404


async def test_provisioning_end_to_end(db, monkeypatch):
    """M2-14/M2-18/M2-25: the CLI's client assigns (an invite is mailed), the invitee
    redeems it without a bearer token, an import dry run and a service account work, and
    offboarding ends the invitee's access."""
    from sqlalchemy import select

    from agentic_cli.control_plane import ControlPlaneRefused
    from agentic_cp.tables import notifications
    from agentic_cp.tenancy.context import tenant_tx

    clock = FakeClock()
    a = await bootstrap(db, clock, "tenant-a", "Tenant A")
    settings = settings_for(db).model_copy(update={"public_url": "https://cp.example.test"})
    with LiveServer(create_app(settings)) as base:
        with ControlPlaneClient(base, a.admin_token) as admin:
            made = admin.assign("dev@a.example.test", "developer", "payments")
            assert made.invited
            async with tenant_tx(db.app, a.id) as conn:
                body = (await conn.execute(select(notifications.c.body_text))).scalar_one()
            token = body.split("invite#", 1)[1].split('"', 1)[0]
            with ControlPlaneClient(base, None) as anonymous:
                redeemed = anonymous.redeem_invite(token)
            assert redeemed.tenant.slug == "tenant-a" and redeemed.gateway_key is None
            with ControlPlaneClient(base, redeemed.control_plane_token) as dev:
                assert dev.me().user.email == "dev@a.example.test"
                with pytest.raises(ControlPlaneRefused) as caught:
                    dev.rotate_proxy_key()
                assert caught.value.code_name == "proxy_key.none"
            dry = admin.import_roles(
                "email,role,team\nann@a.example.test,developer,payments\n", dry_run=True
            )
            assert (dry.applied, dry.added) == (False, 1)
            service = admin.create_service_account("nightly", "payments", 30)
            with ControlPlaneClient(base, service.token) as bot:
                assert bot.check("run.trigger", team="payments").allowed
                assert not bot.check("gate.approve", team="payments").allowed
            # review F4: the admin's list and key commands reach their routes
            assert [s.email for s in admin.service_accounts(team="payments")] == [service.email]
            [queued] = admin.notifications(status="pending")
            assert (queued.kind, queued.recipient) == ("invite", "dev@a.example.test")
            keys = admin.revoke_proxy_keys("dev@a.example.test")
            assert (keys.email, keys.revoked, keys.pending) == ("dev@a.example.test", [], [])
            result = admin.offboard("dev@a.example.test", "left the company")
            assert result.tokens_revoked == 1
            with ControlPlaneClient(base, redeemed.control_plane_token) as dev:
                with pytest.raises(ControlPlaneRefused) as caught:
                    dev.me()
                assert caught.value.status == 401


async def test_tampered_control_plane_record_is_located(live, db):
    """M2-26: the CLI pulls tenant a's chain from the real server and checks it itself; a
    record edited in the database (the owner role, as an attacker with database access
    would) is named by its seq and class, and tenant b's chain stays intact."""
    from sqlalchemy import update

    from agentic_cli.audit import verify_control_plane
    from agentic_cp.tables import audit_log
    from agentic_cp.tenancy.context import tenant_tx

    base, a, b = live
    with ControlPlaneClient(base, a.admin_token) as client:
        for n in range(3):
            client.set_switch({"scope_kind": "tenant", "reason": f"drill {n}"})
        intact = verify_control_plane(client, "tenant-a")
        assert intact.intact and intact.records >= 4
        async with tenant_tx(db.owner, a.id) as conn:
            await conn.execute(
                update(audit_log).where(audit_log.c.seq == 2).values(reason="quietly changed")
            )
        broken = verify_control_plane(client, "tenant-a")
    assert [(f["seq"], f["field_class"]) for f in broken.findings] == [(2, "content")]
    with ControlPlaneClient(base, b.admin_token) as client:
        assert verify_control_plane(client, "tenant-b").intact


async def test_audit_export_end_to_end(live, tmp_path):
    """M2-13: the CLI's client streams a team export over HTTP from the real server; the
    auditor's copied stdlib verifier accepts it and rejects one changed byte."""
    import shutil
    import subprocess
    import sys

    from agentic import auditproof
    from agentic_cli.audit import download_export

    base, a, _ = live
    with ControlPlaneClient(base, a.admin_token) as client:
        for team in ("payments", "billing", "payments"):
            client.set_switch(
                {"scope_kind": "team", "scope_value": team, "mode": "pause", "reason": team}
            )
        report = download_export(
            client, {"format": "jsonl", "team": "payments"}, tmp_path / "team.jsonl"
        )
        csv_report = download_export(client, {"format": "csv"}, tmp_path / "org.csv")
    assert report.ok and report.team == "payments" and report.links >= 1, report.findings
    assert csv_report.ok and csv_report.entries >= 3, csv_report.findings
    copy = tmp_path / "verify.py"
    shutil.copyfile(auditproof.__file__, copy)
    check = [sys.executable, "-I", str(copy), str(tmp_path / "team.jsonl")]
    assert subprocess.run(check, capture_output=True).returncode == 0
    data = (tmp_path / "team.jsonl").read_bytes()
    (tmp_path / "team.jsonl").write_bytes(
        data.replace(b'"reason":"payments"', b'"reason":"paymentz"', 1)
    )
    assert subprocess.run(check, capture_output=True).returncode == 3


async def test_learning_decisions_end_to_end(live, db):
    """M3-15, M3-16: the CLI's client reads, edits, endorses, approves and reports a shared
    proposal against the real routes; another tenant does not see it."""
    from agentic_cp.tables import learning_proposals
    from agentic_cp.tenancy.context import tenant_tx

    base, a, b = live
    pid = "lp-" + "d" * 20
    async with tenant_tx(db.owner, a.id) as conn:
        await conn.execute(
            learning_proposals.insert().values(
                tenant_id=a.id,
                id=pid,
                team="payments",
                agent="developer",
                scope="repo:github.com/acme/app",
                lesson="Wrap HTTP calls in with_retry().",
                evidence=["fb-" + "1" * 20, "fb-" + "2" * 20],
                actors=["alice", "bob"],
            )
        )
    draft = {"kind": "repo-memory", "path": "AGENTS.md", "diff": "+ - with_retry()\n"}
    with ControlPlaneClient(base, a.admin_token) as client:
        assert client.proposal(pid).target == "repo"
        assert client.proposal(pid).may_decide  # asked before a change is made (review F4)
        assert client.edit_proposal(pid, None, "Use with_retry().").edited_lesson == (
            "Use with_retry()."
        )
        assert client.endorse_proposal(pid).endorsements == ["admin@a.example.test"]
        assert client.decide_proposal(pid, "approve", "", draft).status == "approved"
        assert client.proposal(pid).draft == draft
        applied = client.report_applied(pid, {"kind": "pull-request", "number": 7})
        assert (applied.status, applied.applied_ref["number"]) == ("applied", 7)
        assert [p.id for p in client.proposals(status="applied")] == [pid]
        assert client.report_reverted(pid, {"kind": "pull-request"}).status == "reverted"
    with httpx2.Client(base_url=base, headers={"Authorization": f"Bearer {b.admin_token}"}) as http:
        assert http.get(f"{API_PREFIX}/learning/proposals/{pid}").status_code == 404
