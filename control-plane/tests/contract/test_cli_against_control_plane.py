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
