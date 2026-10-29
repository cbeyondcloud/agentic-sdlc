"""The tenant-isolation harness (SP15 Task 3, owner row 76).

Every tenant-owned table registers a row factory here; `test_tenant_isolation` runs every
operation in `OPERATIONS` against every registered table as the runtime role, and
`test_every_tenant_table_has_an_isolation_factory` fails when a table in `TENANT_TABLES` has
none. Every `/v1` route that takes an object id registers an `EndpointCase`
(`test_api_isolation`).

A factory is `async (conn, tenant_id) -> row`: it may insert the parent rows the new row
needs (the connection is the owner role inside that tenant's context) and returns a fresh
row for its table, not yet inserted, with unique keys on every call."""

from __future__ import annotations

import itertools
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from pg import APP_ROLE
from sqlalchemy import Column, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.tenancy.rls import tenant_rls_statements

RowFactory = Callable[[AsyncConnection, UUID], Awaitable[dict[str, Any]]]

#: table name -> row factory, filled by the tasks that create the tables
ROW_FACTORIES: dict[str, RowFactory] = {}

OPERATIONS = (
    "select_other",
    "select_crafted_where",
    "select_no_context",
    "insert_other_tenant_id",
    "insert_no_context",
    "update_other",
    "delete_other",
)

#: A tenant-owned table made by the probe fixture only (never by a migration), so the
#: harness is proven before any real table exists.
PROBE = "isolation_probe"
probe_metadata = MetaData()
probe_table = Table(
    PROBE,
    probe_metadata,
    Column("tenant_id", PgUUID(as_uuid=True), primary_key=True),
    Column("id", PgUUID(as_uuid=True), primary_key=True),
    Column("note", Text, nullable=False),
)


async def probe_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    return {"tenant_id": tenant_id, "id": uuid.uuid4(), "note": f"row of {tenant_id}"}


async def create_probe(db) -> None:
    """Make the probe table in `db` the way a migration makes a tenant-owned table, and
    give the runtime role the DML a tenant table gets."""
    async with db.owner.begin() as conn:
        await conn.run_sync(lambda sync: probe_metadata.create_all(sync))
        await conn.exec_driver_sql(
            f"ALTER TABLE {PROBE} ADD FOREIGN KEY (tenant_id) REFERENCES tenants (id)"
        )
        for statement in tenant_rls_statements(PROBE):
            await conn.exec_driver_sql(statement)
        await conn.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {PROBE} TO {APP_ROLE}")


def register(table: str) -> Callable[[RowFactory], RowFactory]:
    """Decorator: `@register("audit_log") async def _(conn, tenant_id): ...`."""

    def decorate(factory: RowFactory) -> RowFactory:
        ROW_FACTORIES[table] = factory
        return factory

    return decorate


@dataclass(frozen=True)
class EndpointCase:
    """One `/v1` route checked against another tenant's object.

    `make_object_in_b(api)` creates an object in tenant b and returns the values for the
    path and body templates (e.g. `{"id": ...}`); the route, called with tenant a's token,
    must answer exactly like it does for a missing object (DR-5) - `expect_status` (404,
    or 201 for a create-or-replace route, which must then create a's own object).
    `must_not_list` routes are list routes: the values must not appear in their body."""

    family: str
    method: str
    path: str
    make_object_in_b: Callable[[Any], Awaitable[dict[str, str]]]
    body: dict[str, Any] | None = None
    must_not_list: bool = False
    expect_status: int = 404
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def id(self) -> str:
        return f"{self.family}-{self.method} {self.path}"

    def filled_body(self, values: dict[str, str]) -> dict[str, Any] | None:
        if self.body is None:
            return None
        return {
            key: value.format(**values) if isinstance(value, str) else value
            for key, value in self.body.items()
        }


#: filled by the tasks that add object routes
ENDPOINT_CASES: list[EndpointCase] = []


# ── row factories (one per table in TENANT_TABLES) ────────────────────────────

_sequence = itertools.count(1)


@register("audit_log")
async def _audit_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    seq = next(_sequence)
    return {
        "tenant_id": tenant_id,
        "seq": seq,
        "at": "2026-10-22T09:30:00.000Z",
        "actor": "system:test",
        "actor_kind": "system",
        "event": "test.row",
        "target": "",
        "reason": "",
        "prev": "",
        "hash": f"sha256:{seq:064x}",
    }


@register("users")
async def _user_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "id": uuid.uuid4(),
        "email": f"user-{next(_sequence)}@example.test",
        "kind": "human",
    }


async def _insert(conn: AsyncConnection, table: str, row: dict[str, Any]) -> dict[str, Any]:
    from agentic_cp.tables import metadata

    await conn.execute(metadata.tables[table].insert().values(row))
    return row


@register("tokens")
async def _token_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    user = await _insert(conn, "users", await _user_row(conn, tenant_id))
    return {
        "tenant_id": tenant_id,
        "id": uuid.uuid4(),
        "user_id": user["id"],
        "name": "isolation",
        "token_sha256": uuid.uuid4().bytes + uuid.uuid4().bytes,
        "created_by": "system:test",
        "expires_at": datetime.now(UTC) + timedelta(days=1),
    }


@register("roles")
async def _role_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "name": f"custom-{next(_sequence)}",
        "permissions": ["run.trigger"],
    }


@register("assignments")
async def _assignment_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    user = await _insert(conn, "users", await _user_row(conn, tenant_id))
    role = await _insert(conn, "roles", await _role_row(conn, tenant_id))
    return {
        "tenant_id": tenant_id,
        "id": uuid.uuid4(),
        "user_id": user["id"],
        "role": role["name"],
        "scope_kind": "team",
        "scope_value": "payments",
        "created_by": "system:test",
    }


def _version_row(tenant_id: UUID) -> dict[str, Any]:
    commit = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    return {
        "tenant_id": tenant_id,
        "version": f"git:{commit}",
        "commit": commit,
        "branch": "main",
        "status": "accepted",
        "problems": [],
        "teams": ["payments"],
        "bundle": b"bundle",
        "digest": f"sha256:{commit:0>64}",
        "tree_digest": f"sha256:{commit:0>64}",
    }


@register("policy_versions")
async def _policy_version_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    return _version_row(tenant_id)


def _repo_row(tenant_id: UUID) -> dict[str, Any]:
    name = f"app-{next(_sequence)}"
    return {
        "tenant_id": tenant_id,
        "repo_key": f"git.example.test/acme/{name}",
        "remote": f"https://git.example.test/acme/{name}.git",
        "team": "payments",
        "registered_by": "system:test",
    }


@register("repos")
async def _repos_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    return _repo_row(tenant_id)


@register("repo_maintainers")
async def _repo_maintainer_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    repo = await _insert(conn, "repos", _repo_row(tenant_id))
    user = await _insert(conn, "users", await _user_row(conn, tenant_id))
    return {
        "tenant_id": tenant_id,
        "repo_key": repo["repo_key"],
        "user_id": user["id"],
        "source_entry": "line 1: /.agentic/",
    }


def _run_row(tenant_id: UUID, user_id: UUID) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "run_id": f"run-{next(_sequence)}",
        "team": "payments",
        "started_by": user_id,
        "policy_version": "git:" + "c" * 40,
        "policy_known": False,
        "head_seq": 1,
        "head_hash": "sha256:" + "a" * 64,
    }


@register("runs")
async def _runs_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    user = await _insert(conn, "users", await _user_row(conn, tenant_id))
    return _run_row(tenant_id, user["id"])


@register("run_events")
async def _run_event_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    user = await _insert(conn, "users", await _user_row(conn, tenant_id))
    run = await _insert(conn, "runs", _run_row(tenant_id, user["id"]))
    return {
        "tenant_id": tenant_id,
        "run_id": run["run_id"],
        "seq": 1,
        "hash": "sha256:" + "a" * 64,
        "prev": "",
        "event": "run_opened",
        "step": "",
        "at": "2026-10-22T09:30:00.000+00:00",
        "entry": {"seq": 1, "event": "run_opened"},
    }


@register("policy_active")
async def _policy_active_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    version = await _insert(conn, "policy_versions", _version_row(tenant_id))
    return {"tenant_id": tenant_id, "version": version["version"], "branch": "main"}


@register("kill_switches")
async def _kill_switch_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "id": uuid.uuid4(),
        "scope_kind": "team",
        "scope_value": "payments",
        "team": "payments",
        "mode": "kill",
        "reason": f"incident {next(_sequence)}",
        "created_by": "lead@example.test",
    }


@register("notifications")
async def _notification_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    n = next(_sequence)
    return {
        "tenant_id": tenant_id,
        "id": uuid.uuid4(),
        "kind": "invite",
        "recipient": f"user-{n}@example.test",
        "subject": "subject",
        "body_text": "text",
        "body_html": "<p>html</p>",
        "dedupe_key": f"test:{n}",
    }


@register("invites")
async def _invite_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    user = await _insert(conn, "users", await _user_row(conn, tenant_id))
    return {
        "tenant_id": tenant_id,
        "id": uuid.uuid4(),
        "user_id": user["id"],
        "token_sha256": uuid.uuid4().bytes + uuid.uuid4().bytes,
        "created_by": "system:test",
        "expires_at": datetime.now(UTC) + timedelta(hours=72),
    }


@register("proxy_keys")
async def _proxy_key_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    user = await _insert(conn, "users", await _user_row(conn, tenant_id))
    return {
        "tenant_id": tenant_id,
        "key_id": uuid.uuid4().hex[:16],
        "user_id": user["id"],
        "team": "payments",
        "created_by": "system:test",
    }


@register("auto_trips")
async def _auto_trip_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    switch = await _insert(conn, "kill_switches", await _kill_switch_row(conn, tenant_id))
    return {
        "tenant_id": tenant_id,
        "team": f"team-{next(_sequence)}",
        "kind": "spend",
        "state": "tripped",
        "switch_id": switch["id"],
        "value": "12.5",
        "threshold": "10",
    }


@register("routing_applies")
async def _routing_apply_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    version = await _insert(conn, "policy_versions", _version_row(tenant_id))
    return {
        "tenant_id": tenant_id,
        "id": uuid.uuid4(),
        "policy_version": version["version"],
        "status": "applied",
        "gateway_version": next(_sequence),
        "plan_sha256": "sha256:" + "b" * 64,
        "requested_by": "system:test",
    }


@register("personal_events")
async def _personal_event_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    user = await _insert(conn, "users", await _user_row(conn, tenant_id))
    return {
        "tenant_id": tenant_id,
        "user_id": user["id"],
        "device": uuid.uuid4().hex,
        "seq": 1,
        "hash": "sha256:" + "a" * 64,
        "prev": "",
        "event": "personal.saved",
        "kind": "memory",
        "path": "memory/notes.md",
        "sha256": "sha256:" + "c" * 64,
        "content": "a note",
        "entry": {"seq": 1, "event": "personal.saved"},
    }


@register("feedback_events")
async def _feedback_event_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    user = await _insert(conn, "users", await _user_row(conn, tenant_id))
    run = await _insert(conn, "runs", _run_row(tenant_id, user["id"]))
    return {
        "tenant_id": tenant_id,
        "run_id": run["run_id"],
        "seq": 1,
        "hash": "sha256:" + "b" * 64,
        "prev": "",
        "event": "learning.feedback_captured",
        "entry": {"seq": 1, "event": "learning.feedback_captured"},
    }


def _feedback_values(tenant_id: UUID, run_id: str, author_id: UUID) -> dict[str, Any]:
    n = next(_sequence)
    return {
        "tenant_id": tenant_id,
        "id": f"fb-{n:020x}",
        "run_id": run_id,
        "team": "payments",
        "repo": "github.com/acme/app",
        "signal": "gate-rejection",
        "actor": "lead@example.test",
        "given_at": datetime.now(UTC),
        "author_id": author_id,
        "record": {"text": f"use the existing queue {n}", "source": f"gate:g:reject:{n}"},
        "agents": ["design"],
        "lesson": f"Reuse the existing queue {n}.",
    }


@register("feedback")
async def _feedback_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    user = await _insert(conn, "users", await _user_row(conn, tenant_id))
    run = await _insert(conn, "runs", _run_row(tenant_id, user["id"]))
    return _feedback_values(tenant_id, run["run_id"], user["id"])


@register("learning_proposals")
async def _proposal_row(conn: AsyncConnection, tenant_id: UUID) -> dict[str, Any]:
    n = next(_sequence)
    return {
        "tenant_id": tenant_id,
        "id": f"lp-{n:020x}",
        "team": "payments",
        "agent": "design",
        "scope": "repo:github.com/acme/app",
        "lesson": f"Reuse the existing queue {n}.",
        "evidence": [f"fb-{n:020x}"],
    }


# ── endpoint cases ────────────────────────────────────────────────────────────


async def _routing_apply_in_b(api) -> dict[str, str]:
    """A routing apply of a policy version in tenant b only (written as the owner role)."""
    from agentic_cp.tenancy.context import tenant_tx

    async with tenant_tx(api.db.owner, api.b.id) as conn:
        row = await _routing_apply_row(conn, api.b.id)
        await _insert(conn, "routing_applies", row)
    return {"id": str(row["id"]), "version": row["policy_version"]}


async def _personal_backup_in_b(api) -> dict[str, str]:
    """A personal backup event of tenant b's admin, on its own device (written as owner)."""
    from agentic_cp.tenancy.context import tenant_tx

    async with tenant_tx(api.db.owner, api.b.id) as conn:
        row = await _personal_event_row(conn, api.b.id)
        row["user_id"] = api.b.admin_id
        row["content"] = f"b-only note {uuid.uuid4().hex}"
        await _insert(conn, "personal_events", row)
    return {"device": row["device"], "content": row["content"]}


async def _token_in_b(api) -> dict[str, str]:
    issued = await api.post("/v1/tokens", api.b.admin_token, {"name": "b-only"})
    assert issued.status_code == 201, issued.text
    return {"id": issued.json()["id"]}


async def _role_in_b(api) -> dict[str, str]:
    name = f"b-only-{next(_sequence)}"
    made = await api.put(
        f"/v1/rbac/roles/{name}", api.b.admin_token, {"permissions": ["run.trigger"]}
    )
    assert made.status_code == 201, made.text
    return {"name": name}


async def _assignment_in_b(api) -> dict[str, str]:
    email = f"b-only-{next(_sequence)}@b.example.test"
    made = await api.post(
        "/v1/rbac/assignments",
        api.b.admin_token,
        {"email": email, "role": "developer", "team": "payments"},
    )
    assert made.status_code == 201, made.text
    return {"id": made.json()["id"], "email": email}


async def _user_in_b(api) -> dict[str, str]:
    values = await _assignment_in_b(api)
    return {"email": values["email"]}


async def _audit_record_in_b(api) -> dict[str, str]:
    values = await _role_in_b(api)  # writes role.updated into b's chain
    return values


async def _policy_version_in_b(api) -> dict[str, str]:
    from policy_repos import make_policy_repo, url

    repo = make_policy_repo(Path(tempfile.mkdtemp(prefix="cp-iso-")) / "policy")
    made = await api.put(
        "/v1/tenant/policy-source", api.b.admin_token, {"repo": url(repo), "branch": "main"}
    )
    assert made.status_code == 200 and made.json()["sync"]["kind"] == "activated", made.text
    return {"version": made.json()["sync"]["version"]}


async def _repo_in_b(api) -> dict[str, str]:
    """A repo registered in tenant b only (its git mapped onto a local repo)."""
    from policy_repos import (
        APP_KEY,
        APP_REMOTE,
        LocalHosts,
        make_app_repo,
        make_org_policy_repo,
        url,
    )

    from agentic_cp.gitops import GitRunner

    class MappedGit(LocalHosts, GitRunner):
        pass

    scratch = Path(tempfile.mkdtemp(prefix="cp-iso-"))
    policy = make_org_policy_repo(scratch / "policy")
    app = make_app_repo(scratch / "app", "/.agentic/ @alice\n")
    api.app.state.git = MappedGit(api.settings, {APP_REMOTE: app})
    source = {"repo": url(policy), "branch": "main"}
    assert (await api.put("/v1/tenant/policy-source", api.b.admin_token, source)).status_code == 200
    made = await api.post(
        "/v1/repos", api.b.admin_token, {"remote": APP_REMOTE, "team": "payments"}
    )
    assert made.status_code == 201, made.text
    return {"repo_key": APP_KEY}


async def _run_in_b(api) -> dict[str, str]:
    """A run ingested in tenant b only (by b's org admin, who holds run.trigger)."""
    from sdlc_commons import ledger as ledger_module
    from sdlc_commons.ledger import RunIdentity, RunLedger

    run_id = f"iso-run-{next(_sequence)}"
    ledger = RunLedger(Path(tempfile.mkdtemp(prefix="cp-iso-")), run_id)
    ledger.open(
        identity=RunIdentity(team="payments", policy_version="git:" + "c" * 40),
        kind="feature",
        project="demo",
        owner="payments",
        criticality="low",
    )
    entries = ledger.events()
    ledger.release()
    ledger_module._IDENTITIES.pop(run_id, None)
    made = await api.post(f"/v1/runs/{run_id}/events", api.b.admin_token, {"entries": entries})
    assert made.status_code == 200, made.text
    return {"run_id": run_id}


async def _switch_in_b(api) -> dict[str, str]:
    """A tenant-wide kill switch in tenant b only (it would match every run in a)."""
    made = await api.post(
        "/v1/killswitch",
        api.b.admin_token,
        {"scope_kind": "tenant", "reason": f"b-only {next(_sequence)}"},
    )
    assert made.status_code == 201, made.text
    return {"id": made.json()["id"], "reason": made.json()["reason"]}


async def _notification_in_b(api) -> dict[str, str]:
    """A mail queued in tenant b only (to an address only b's users table holds)."""
    from agentic_cp.notify.outbox import enqueue
    from agentic_cp.notify.templates import render
    from agentic_cp.tenancy.context import tenant_tx

    recipient = f"b-only-{next(_sequence)}@b.example.test"
    rendered = render(
        "gate.waiting",
        {"run_id": "r", "team": "payments", "gate": "g", "role": "x"},
        tenant_name="B",
    )
    async with tenant_tx(api.db.app, api.b.id) as conn:
        made = await enqueue(
            conn,
            kind="gate.waiting",
            recipient=recipient,
            rendered=rendered,
            dedupe_key=recipient,
            actor="system:test",
            clock=api.clock,
        )
    return {"id": str(made), "recipient": recipient}


async def _feedback_in_b(api) -> dict[str, str]:
    """A feedback record (and a proposal) of a run in tenant b only (written as owner)."""
    from agentic_cp.tenancy.context import tenant_tx

    run = await _run_in_b(api)
    async with tenant_tx(api.db.owner, api.b.id) as conn:
        row = _feedback_values(api.b.id, run["run_id"], api.b.admin_id)
        await _insert(conn, "feedback", row)
        proposal = await _proposal_row(conn, api.b.id)
        proposal["lesson"] = f"b-only lesson {uuid.uuid4().hex}"
        await _insert(conn, "learning_proposals", proposal)
    return {
        "feedback_id": row["id"],
        "run_id": run["run_id"],
        "lesson": row["lesson"],
        "proposal": proposal["id"],
        "proposal_lesson": proposal["lesson"],
    }


async def _service_account_in_b(api) -> dict[str, str]:
    name = f"bonly{next(_sequence)}"
    made = await api.post(
        "/v1/service-accounts", api.b.admin_token, {"name": name, "team": "payments", "days": 5}
    )
    assert made.status_code == 201, made.text
    return {"id": made.json()["id"], "email": made.json()["email"]}


ENDPOINT_CASES += [
    EndpointCase(
        # another tenant's run is unknown here: its feedback is refused as for any unknown run
        "learning",
        "POST",
        "/v1/runs/{run_id}/feedback",
        _feedback_in_b,
        body={"entries": [{"seq": 1}]},
    ),
    EndpointCase(
        "learning",
        "PUT",
        "/v1/learning/feedback/{feedback_id}/attribution",
        _feedback_in_b,
        body={"agents": ["developer"], "reason": "it was the code"},
    ),
    EndpointCase("learning", "GET", "/v1/learning/feedback", _feedback_in_b, must_not_list=True),
    EndpointCase("learning", "GET", "/v1/learning/proposals", _feedback_in_b, must_not_list=True),
    EndpointCase(
        "service-accounts",
        "GET",
        "/v1/service-accounts",
        _service_account_in_b,
        must_not_list=True,
    ),
    EndpointCase("notify", "GET", "/v1/notifications", _notification_in_b, must_not_list=True),
    EndpointCase("proxy-keys", "DELETE", "/v1/users/{email}/proxy-keys", _user_in_b),
    EndpointCase("provisioning", "POST", "/v1/users/{email}/invite", _user_in_b),
    EndpointCase(
        "provisioning",
        "POST",
        "/v1/users/{email}/offboard",
        _user_in_b,
        body={"reason": "left the company"},
    ),
    EndpointCase("killswitch", "DELETE", "/v1/killswitch/{id}", _switch_in_b),
    EndpointCase("killswitch", "GET", "/v1/killswitch", _switch_in_b, must_not_list=True),
    EndpointCase(
        "killswitch",
        "GET",
        "/v1/killswitch/state?team=payments",
        _switch_in_b,
        must_not_list=True,
    ),
    EndpointCase("tokens", "DELETE", "/v1/tokens/{id}", _token_in_b),
    EndpointCase("tokens", "GET", "/v1/tokens", _token_in_b, must_not_list=True),
    EndpointCase("rbac", "GET", "/v1/rbac/roles/{name}", _role_in_b),
    EndpointCase("rbac", "DELETE", "/v1/rbac/roles/{name}", _role_in_b),
    EndpointCase(
        "rbac",
        "PUT",
        "/v1/rbac/roles/{name}",
        _role_in_b,
        body={"permissions": ["dashboard.view.own"]},
        expect_status=201,
    ),
    EndpointCase("rbac", "DELETE", "/v1/rbac/assignments/{id}", _assignment_in_b),
    EndpointCase(
        "rbac",
        "POST",
        "/v1/rbac/check",
        _user_in_b,
        body={"permission": "run.trigger", "team": "payments", "user": "{email}"},
    ),
    EndpointCase("rbac", "GET", "/v1/rbac/assignments", _assignment_in_b, must_not_list=True),
    EndpointCase("rbac", "GET", "/v1/rbac/roles", _role_in_b, must_not_list=True),
    EndpointCase("audit", "GET", "/v1/audit", _audit_record_in_b, must_not_list=True),
    EndpointCase("audit", "GET", "/v1/audit/export", _audit_record_in_b, must_not_list=True),
    EndpointCase(
        "audit",
        "GET",
        "/v1/audit/export?format=csv&team=payments",
        _audit_record_in_b,
        must_not_list=True,
    ),
    EndpointCase("policy", "GET", "/v1/policy/versions/{version}/bundle", _policy_version_in_b),
    EndpointCase("policy", "GET", "/v1/policy/versions", _policy_version_in_b, must_not_list=True),
    EndpointCase("repos", "GET", "/v1/repos/{repo_key}/maintainers", _repo_in_b),
    EndpointCase("repos", "POST", "/v1/repos/{repo_key}/refresh", _repo_in_b),
    EndpointCase(
        "users",
        "PUT",
        "/v1/users/{email}/handles",
        _user_in_b,
        body={"handles": ["git.example.test:someone"]},
    ),
    EndpointCase("routing", "GET", "/v1/routing", _routing_apply_in_b, must_not_list=True),
    EndpointCase(
        # another tenant's device is a new, unknown chain here: the same refusal as for a
        # device nobody used (an entry that is not an event)
        "personal",
        "POST",
        "/v1/personal/devices/{device}/events",
        _personal_backup_in_b,
        body={"entries": [{"seq": 2}]},
        expect_status=422,
    ),
    EndpointCase(
        "personal", "GET", "/v1/personal/files", _personal_backup_in_b, must_not_list=True
    ),
    EndpointCase("runs", "GET", "/v1/runs/{run_id}", _run_in_b),
    EndpointCase("runs", "GET", "/v1/runs/{run_id}/events", _run_in_b),
    EndpointCase(
        # another tenant's run is unknown here: its heartbeat is a 404 (M3-07)
        "runs",
        "POST",
        "/v1/runs/{run_id}/heartbeat",
        _run_in_b,
        body={"beat_at": "2026-10-29T10:00:00Z", "sent_at": "2026-10-29T10:00:00Z"},
    ),
    EndpointCase(
        # another tenant's run id is a new, unknown run here: a batch not starting at
        # seq 1 is a gap at head 0, exactly as for a run id nobody used
        "runs",
        "POST",
        "/v1/runs/{run_id}/events",
        _run_in_b,
        body={"entries": [{"seq": 2}]},
        expect_status=409,
    ),
]


async def _chargeback_in_b(api) -> dict[str, str]:
    """A run of a team only tenant b has, opened in the previous calendar month (the
    chargeback's default month), written as the owner role."""
    from agentic.orgapi.cost import previous_month
    from agentic_cp.tables import runs
    from agentic_cp.tenancy.context import tenant_tx

    team = f"b-only-team-{next(_sequence)}"
    year, month = (int(part) for part in previous_month(api.clock().date()).split("-"))
    async with tenant_tx(api.db.owner, api.b.id) as conn:
        await conn.execute(
            runs.insert().values(
                tenant_id=api.b.id,
                run_id=f"iso-cost-{uuid.uuid4().hex[:10]}",
                team=team,
                started_by=api.b.admin_id,
                policy_version="git:" + "c" * 40,
                policy_known=True,
                opened_at=datetime(year, month, 15, tzinfo=UTC),
            )
        )
    return {"team": team}


ENDPOINT_CASES += [
    EndpointCase("cost", "GET", "/v1/cost/chargeback", _chargeback_in_b, must_not_list=True),
    EndpointCase(
        "cost", "GET", "/v1/cost/chargeback?format=csv", _chargeback_in_b, must_not_list=True
    ),
]
