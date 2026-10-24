"""Fixtures for the kill-switch propagation E2E (M2-54): the real control plane serving a
test database with tenant a (an org admin and a developer of team `payments`) and a synced
org policy; an org-mode CLI workspace signed in as the developer, holding the policy the
CLI fetched from the control plane; and the CLI run in this process on the main thread.

The tests are synchronous (the CLI runs its own event loop) and ask for `anyio_backend`,
so the async database fixtures run on the anyio runner. Without Postgres they skip with
the database fixture's reason (a failure where AGENTIC_CP_REQUIRE_PG=1, as in CI)."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx2
import keyring
import pytest
from _killswitch_e2e import (
    MODEL_KEY,
    TEAM,
    TIMINGS,
    LiveServer,
    Plane,
    ScriptedModel,
    StateLog,
    make_e2e_policy_repo,
    make_target_repo,
    report,
)
from apiclient import FakeClock, bootstrap, settings_for
from keyring.backend import KeyringBackend
from policy_repos import url

from agentic.orgapi import API_PREFIX
from agentic.sandbox.readiness import Readiness
from agentic_cli import runtime
from agentic_cli.control_plane import record_login
from agentic_cli.main import main
from agentic_cli.policy_source import resolve_policy
from agentic_cp.app import create_app
from agentic_cp.auth.tokens import issue_token
from agentic_cp.auth.users import create_user
from agentic_cp.tenancy.context import tenant_tx
from sdlc_commons import ledger as ledger_module

DEV_EMAIL = "dev@a.example.test"
ORG = "acme"
#: what must never leak in from the machine running the tests
MACHINE_VARS = (
    "AGENTIC_POLICY_REPO",
    "AGENTIC_USER",
    "AGENTIC_CONTROL_PLANE_URL",
    "AGENTIC_GITHUB_TOKEN",
    "CI",
    "GITHUB_ACTIONS",
    "TF_BUILD",
    "JENKINS_URL",
    "GITLAB_CI",
    "BUILDKITE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def pytest_terminal_summary(terminalreporter):
    """Every run's timing, passed or failed (M2-54 reports them, with the worst case)."""
    if not TIMINGS:
        return
    terminalreporter.section("kill-switch propagation (M2-54)")
    terminalreporter.write_line(report(TIMINGS))
    for scenario in dict.fromkeys(t.scenario for t in TIMINGS):
        runs = [t for t in TIMINGS if t.scenario == scenario]
        stopped = max(t.to_stopped_s for t in runs)
        gone = [t.to_gone_s for t in runs if t.to_gone_s is not None]
        terminalreporter.write_line(
            f"{scenario:<10} worst of {len(runs)}: stopped {stopped:.1f} s"
            + (f", processes gone {max(gone):.1f} s" if gone else "")
        )


class EmptyKeyring(KeyringBackend):
    """A keyring with nothing in it: no test reads (or writes) the developer's own."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.stored: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.stored.get((service, username))

    def set_password(self, service, username, password):
        self.stored[(service, username)] = password

    def delete_password(self, service, username):
        self.stored.pop((service, username), None)


@dataclass
class Tenant:
    admin_email: str
    admin_token: str
    dev_token: str


@pytest.fixture
async def tenant(db) -> Tenant:
    """Tenant a: its org admin (bootstrap) and a developer with a token."""
    clock = FakeClock()
    a = await bootstrap(db, clock, "tenant-a", "Tenant A")
    async with tenant_tx(db.owner, a.id) as conn:
        user = await create_user(conn, DEV_EMAIL)
        _, dev_token = await issue_token(
            conn,
            user_id=user.id,
            name="e2e",
            expires_at=clock.now + timedelta(days=30),
            created_by="system:test",
        )
    return Tenant("admin@a.example.test", a.admin_token, dev_token)


@pytest.fixture
def plane(anyio_backend, db, tenant, tmp_path, monkeypatch) -> Plane:
    """The control plane serving `db` over HTTP, with the e2e policy synced and the
    developer holding `developer` in team payments."""
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})
    log = StateLog(create_app(settings_for(db)))
    server = LiveServer(log)
    base = server.start()
    try:
        policy = make_e2e_policy_repo(tmp_path / "org-policy")
        admin = {"Authorization": f"Bearer {tenant.admin_token}"}
        with httpx2.Client(base_url=base, headers=admin, timeout=60) as http:
            made = http.put(
                f"{API_PREFIX}/tenant/policy-source", json={"repo": url(policy), "branch": "main"}
            )
            assert made.status_code == 200, made.text
            sync = made.json()["sync"]
            assert sync["kind"] == "activated", sync
            role = {"email": DEV_EMAIL, "role": "developer", "team": TEAM}
            assigned = http.post(f"{API_PREFIX}/rbac/assignments", json=role)
            assert assigned.status_code == 201, assigned.text
        yield Plane(
            base=base,
            admin_email=tenant.admin_email,
            admin_token=tenant.admin_token,
            dev_email=DEV_EMAIL,
            dev_token=tenant.dev_token,
            log=log,
            server=server,
            version=sync["version"],
        )
    finally:
        server.stop()


@dataclass
class Model:
    """The scripted model the CLI calls now (each run gets a fresh script)."""

    current: ScriptedModel | None = None

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        assert self.current is not None, "no model script for this run"
        return self.current.handler(request)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handler)


@dataclass
class Workspace:
    repo: Path
    home: Path
    model: Model

    def run_dir(self, run_id: str) -> Path:
        return self.repo / ".agentic" / "local" / "runs" / run_id


@pytest.fixture
def workspace(plane: Plane, tmp_path, monkeypatch) -> Workspace:
    """An org-mode CLI workspace signed in to `plane` as the developer: the policy fetched
    from the control plane (what `agentic init` does), the target repository as the current
    folder, the scripted model as the gateway, no machine settings."""
    previous = keyring.get_keyring()
    keyring.set_keyring(EmptyKeyring())
    for name in MACHINE_VARS:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    config = {"org": ORG, "control_plane_url": plane.base}
    (home / "config.yaml").write_bytes(f"org: {ORG}\ncontrol_plane_url: {plane.base}\n".encode())
    env = {
        "AGENTIC_HOME": str(home),
        "AGENTIC_CONTROL_PLANE_TOKEN": plane.dev_token,
        "AGENTIC_TEAM": TEAM,
        "AGENTIC_TRACES": "off",  # one global tracer provider per process
        "AGENTIC_GATEWAY_URL": "https://gateway.example.test",
        "AGENTIC_TOKENLEAN_API_KEY": MODEL_KEY,
        # the agent's `python` is this interpreter, however the tests were started
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    record_login(home, plane.base, "tenant-a", plane.dev_email)
    loaded = resolve_policy(
        standalone=False, policy_path=None, home=home, config=config, env=dict(os.environ)
    )
    try:
        assert loaded.policy.version == plane.version
        loaded.commit()
    finally:
        loaded.discard()
    repo = make_target_repo(tmp_path / "app")
    monkeypatch.chdir(repo)
    model = Model()
    monkeypatch.setattr(runtime, "network_transport", model.transport)
    monkeypatch.setattr(runtime, "machine_readiness", lambda: Readiness("windows", ()))
    try:
        yield Workspace(repo=repo, home=home, model=model)
    finally:
        keyring.set_keyring(previous)


@dataclass
class Result:
    code: int
    out: str
    err: str

    def json(self) -> dict:
        return json.loads(self.out)


@pytest.fixture
def cli(capsys):
    """cli(*args) -> Result: `agentic` in this process, on this (the main) thread."""

    def run(*args: str) -> Result:
        code = main(list(args))
        captured = capsys.readouterr()
        return Result(int(code), captured.out, captured.err)

    return run
