"""The org stack of the M2 acceptance scenarios (M2-53, M2-55), in this process.

The merged control plane, unchanged, as `deploy/org-local` brings it up - without docker
compose (ruling R1 of docs/superpowers/plans/2026-10-24-m2-org-acceptance.md):

  * **Postgres** - the one the control-plane tests use (`control-plane/tests/pg.py`): the
    Linux check stage's sidecar (`AGENTIC_CP_TEST_DSN`, where `AGENTIC_CP_REQUIRE_PG=1` makes
    a missing database a failure), else a throw-away container through docker or podman;
    with neither the scenarios skip, naming both. Each scenario gets a fresh database, set
    up as `cp-postgres/init.sh` and `cp-init/init.sh` do: an owner role, then the operator
    commands `migrate`, `db grant-runtime` and `admin tenant create`. The roles are
    `acc_owner` and `acc_app` (ruling R2: role passwords are cluster-wide, and the
    control-plane tests own `cp_owner` and `cp_app` on the shared CI server);
  * **the control plane** - `create_app(settings)` after `serve_preflight` (what `serve`
    refuses, this refuses), served by uvicorn on a loopback port in a thread: forced RLS
    with the runtime role, tenant-bound tokens, the per-tenant audit chain. Settings:
    `file://` policy repos allowed (as in the control-plane tests, R12) and a gateway admin
    named, so an activated policy queues a routing apply;
  * **the gateway's routing API** - `FakeRouting` (control-plane/tests), driven by the
    worker's own `apply_due`; model calls go to the cassette server (the scenarios);
  * **people** - the org admin acts through the API with the token `admin tenant create`
    printed; everyone else gets a role from the admin (`POST /v1/rbac/assignments`) and a
    token from `admin issue-token`, then signs in with `agentic login`.

Nothing here calls a model, TokenLean or the internet."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2

ROOT = Path(__file__).resolve().parents[3]
CP_TESTS = ROOT / "control-plane" / "tests"
if str(CP_TESTS) not in sys.path:  # pg.py and fake_routing.py, shared with those tests
    sys.path.append(str(CP_TESTS))  # last: it never shadows a module of these tests

import pg  # noqa: E402
from fake_routing import FakeRouting  # noqa: E402

from agentic_cli.standalone import assemble  # noqa: E402
from agentic_cp.__main__ import main as operator  # noqa: E402
from agentic_cp.app import create_app, serve_preflight, utc_now  # noqa: E402
from agentic_cp.db import engine_for  # noqa: E402
from agentic_cp.routing.apply import ApplyReport, apply_due  # noqa: E402
from agentic_cp.settings import Settings  # noqa: E402

OWNER_ROLE, APP_ROLE, SCHEMA = "acc_owner", "acc_app", "agentic"
TENANT, GATEWAY_TENANT, TEAM = "acme", "gw-acme", "payments"
ADMIN = "admin@acme.example"
#: what the deployment names as the gateway's admin API; FakeRouting stands behind it
GATEWAY_ADMIN_URL = "https://gateway.example.test"
GATEWAY_ADMIN_KEY = "tl-admin-" + "acceptance-only-key"  # gitleaks:allow (made up)
PIPELINE = "org-plan"
PIPELINE_YAML = """\
# The org's planning pipeline (M2 acceptance): requirements, the test baseline, then a
# design. It lives in the policy repo, so registration checks its bytes against the
# approved file (D33).
name: org-plan
version: 1.0.0
regime: team
inputs:
  request: { from: [text] }
steps:
  - id: requirements
    agent: business-analyst
  - id: baseline
    agent: qa-baseline
  - id: design
    agent: design
"""
_GIT = [
    "git",
    "-c",
    "core.autocrlf=false",
    "-c",
    "user.name=Org Admin",
    "-c",
    f"user.email={ADMIN}",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "init.defaultBranch=main",
]


def postgres() -> pg.Server:
    """The test Postgres (or a skip naming what is missing; a failure where it is required)."""
    found = pg.find_source()
    if isinstance(found, str):
        pg.unavailable(found)
    return pg.Server(admin_dsn=found.dsn) if found.dsn else pg.start_container(found.engine)


def policy_git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [*_GIT, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def edit(path: Path, old: str, new: str) -> None:
    """Replace `old` (which must be there) with `new` in a policy file, LF bytes."""
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert old in text, f"{old!r} is not in {path}"
    path.write_bytes(text.replace(old, new).encode("utf-8"))


def org_policy_repo(path: Path, models: dict[str, str], *, sandbox: bool, hosts: str) -> Path:
    """The tenant's policy repo as an org admin starts it: the framework's template (the
    library agents, pipelines and skills; the standalone policy files) with real models for
    each tier, the org's repositories (`hosts`, a pattern), team `payments` and the org
    pipeline; committed on main. Without an OS sandbox on this machine, team runs may go
    unsandboxed (decision 66, as the M1 scenarios' own policy copy)."""
    assemble(path)
    for placeholder, model in models.items():
        edit(path / "policy" / "tiers.yaml", placeholder, model)
    org = path / "policy" / "org.yaml"
    edit(org, "allowed_repo_patterns: []", f'allowed_repo_patterns: ["{hosts}"]')
    if not sandbox:
        edit(org, "required_for: [team, regulated]", "required_for: []")
    (path / "policy" / "teams").mkdir()
    (path / "policy" / "teams" / f"{TEAM}.yaml").write_bytes(b"{}\n")
    (path / "pipelines" / f"{PIPELINE}.yaml").write_bytes(PIPELINE_YAML.encode("utf-8"))
    policy_git(path, "init", "-q")
    commit(path, "org policy from the framework template")
    return path


def commit(repo: Path, message: str) -> str:
    policy_git(repo, "add", "-A")
    policy_git(repo, "commit", "-q", "-m", message)
    return policy_git(repo, "rev-parse", "HEAD")


@contextlib.contextmanager
def _environment(values: dict[str, str]) -> Iterator[None]:
    """`values` in os.environ for an operator command (they read their settings there)."""
    before = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@dataclass
class OrgStack:
    """One scenario's control plane on its own database (see the module docstring)."""

    server: pg.Server
    tmp: Path
    url: str = ""
    tenant_id: UUID | None = None
    admin_token: str = ""
    routing: FakeRouting = field(default_factory=FakeRouting)
    _env: dict[str, str] = field(default_factory=dict)
    _database: str = ""
    _uvicorn: Any = None
    _thread: threading.Thread | None = None

    # ── bring-up ─────────────────────────────────────────────────────────────

    def start(self) -> OrgStack:
        self._database = pg.new_database_name("acc")
        owner_password, app_password = secrets.token_hex(16), secrets.token_hex(16)
        asyncio.run(self._owner_role(owner_password))
        dsn = self.server.dsn
        workdir = self.tmp / "cp-policy"
        self._env = {
            "AGENTIC_CP_DATABASE_URL": dsn(
                user=APP_ROLE, password=app_password, database=self._database
            ),
            "AGENTIC_CP_OWNER_DATABASE_URL": dsn(
                user=OWNER_ROLE, password=owner_password, database=self._database
            ),
            "AGENTIC_CP_ALLOW_LOCAL_REPOS": "true",
            "AGENTIC_CP_POLICY_WORKDIR": str(workdir),
            "AGENTIC_CP_GATEWAY_ADMIN_URL": GATEWAY_ADMIN_URL,
            "AGENTIC_CP_GATEWAY_ADMIN_KEY": GATEWAY_ADMIN_KEY,
        }
        self.operator("migrate")
        self.operator("db", "grant-runtime")
        created = self.operator(
            "admin",
            "tenant",
            "create",
            "--slug",
            TENANT,
            "--name",
            "Acme",
            "--admin-email",
            ADMIN,
            "--gateway-tenant",
            GATEWAY_TENANT,
        )
        self.tenant_id = UUID(re.search(r"\(id ([0-9a-f-]{36})\)", created).group(1))
        self.admin_token = created.strip().splitlines()[-1]
        self._serve()
        return self

    async def _owner_role(self, password: str) -> None:
        """What `cp-postgres/init.sh` does: the owner role (it creates the runtime role) and
        the database it owns."""
        admin = await pg.connect_admin(self.server.admin_dsn)
        try:
            exists = await admin.fetchval("select 1 from pg_roles where rolname = $1", OWNER_ROLE)
            verb = "ALTER" if exists else "CREATE"
            # a random test-only password; Postgres takes no bind parameter in role DDL
            await admin.execute(f"{verb} ROLE {OWNER_ROLE} LOGIN CREATEROLE PASSWORD '{password}'")
            await admin.execute(
                f"CREATE DATABASE {pg.quote_ident(self._database)} OWNER {OWNER_ROLE}"
            )
        finally:
            await admin.close()

    def operator(self, *argv: str) -> str:
        """`python -m agentic_cp <argv>` in this process; its output (exit 0 or it fails)."""
        out, err = io.StringIO(), io.StringIO()
        with (
            _environment(self._env),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = operator(list(argv))
        assert code == 0, f"agentic_cp {' '.join(argv)} exited {code}: {err.getvalue()}"
        return out.getvalue()

    @property
    def settings(self) -> Settings:
        return Settings.from_env(self._env)

    def _serve(self) -> None:
        import uvicorn

        settings = self.settings
        refusal = asyncio.run(serve_preflight(settings))
        assert refusal is None, f"serve would refuse: {refusal}"
        app = create_app(settings)
        config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on")
        self._uvicorn = uvicorn.Server(config)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        self._thread = threading.Thread(
            target=self._uvicorn.run, kwargs={"sockets": [listener]}, daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + 60
        while not self._uvicorn.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the control plane did not start")
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=30)
        if self._database:
            asyncio.run(self._drop())
        if (self.tmp / "cp-policy").exists():
            shutil.rmtree(self.tmp / "cp-policy", onexc=_writable_and_retry)

    async def _drop(self) -> None:
        admin = await pg.connect_admin(self.server.admin_dsn)
        try:
            name = pg.quote_ident(self._database)
            await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        finally:
            await admin.close()

    # ── the API, as people call it ───────────────────────────────────────────

    def api(self, method: str, path: str, token: str, body: Any = None) -> httpx2.Response:
        with httpx2.Client(base_url=self.url, timeout=60, trust_env=False) as http:
            return http.request(
                method, path, json=body, headers={"Authorization": f"Bearer {token}"}
            )

    def ok(self, method: str, path: str, token: str | None = None, body: Any = None) -> Any:
        reply = self.api(method, path, token or self.admin_token, body)
        assert reply.status_code in (200, 201), f"{method} {path}: {reply.status_code} {reply.text}"
        return reply.json()

    def person(self, email: str, role: str, team: str | None = None) -> str:
        """The admin gives `email` a role (the person is created when new); the operator
        issues their token (`admin issue-token`). Returns the token."""
        body = {"email": email, "role": role, **({"team": team} if team else {})}
        self.ok("POST", "/v1/rbac/assignments", body=body)
        issued = self.operator("admin", "issue-token", "--tenant", TENANT, "--email", email)
        return issued.strip().splitlines()[-1]

    def connect_policy(self, repo: Path) -> dict:
        """The admin points the tenant at its policy repo; the control plane syncs it."""
        body = {"repo": repo.resolve().as_uri(), "branch": "main"}
        return self.ok("PUT", "/v1/tenant/policy-source", body=body)["sync"]

    def sync(self) -> dict:
        """The admin's manual sync (ruling R3: the webhook and the worker call the same
        `sync_once`)."""
        return self.ok("POST", "/v1/policy/sync")

    def apply_routing(self) -> ApplyReport | None:
        """The worker's routing job for this tenant, against `FakeRouting`."""
        return asyncio.run(self._apply())

    async def _apply(self) -> ApplyReport | None:
        settings = self.settings
        engine = engine_for(settings.database_url.get_secret_value(), SCHEMA)
        try:
            return await apply_due(engine, self.tenant_id, self.routing, settings, utc_now)
        finally:
            await engine.dispose()

    def tier_models(self) -> dict[str, str]:
        """What the gateway's overlay routes each policy tier to now (the tier rules)."""
        groups = self.routing.current["tenants"][GATEWAY_TENANT]["groups"]
        rules = groups["G6_routing"]["rules"]
        return {
            rule["match"]["params"]["x_agentic_tier"][0]: rule["action"]["model"]
            for rule in rules
            if set(rule["match"]["params"]) == {"x_agentic_tier"}
        }

    def audit(self) -> list[dict]:
        """The tenant's audit chain, oldest first (the admin holds audit.read.org)."""
        return self.ok("GET", "/v1/audit?limit=1000")


# ── the people's side ────────────────────────────────────────────────────────


def org_mode(scenario, stack: OrgStack) -> None:
    """The person's `~/.agentic/config.yaml` in org mode, as their onboarding mail says: the
    org, its control plane and their team (beside the git host the scenario serves)."""
    config = scenario.agentic_home / "config.yaml"
    text = config.read_text(encoding="utf-8")
    text += f"org: {TENANT}\ncontrol_plane_url: {stack.url}\nteam: {TEAM}\n"
    config.write_text(text, encoding="utf-8")


def sign_in(scenario, token: str) -> dict:
    """`agentic login --for control-plane --key-stdin`: the token is verified against
    `/v1/me` and kept in the (test) keyring for that URL only."""
    scenario.monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
    return scenario.ok("--json", "login", "--for", "control-plane", "--key-stdin").json()


def _writable_and_retry(func, path, _error) -> None:
    os.chmod(path, 0o700)  # git makes pack files read-only (Windows needs them writable)
    func(path)
