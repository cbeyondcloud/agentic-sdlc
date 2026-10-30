"""Is a restored database usable? (M3-02, the restore drill and `db verify-restore`.)

After a point-in-time recovery or a `pg_restore`, three things must hold before the control
plane may serve it: the schema is at this release's migration revision, the runtime role's
grants and every tenant-isolation and insert-only guarantee are in place
(`enforcement_status`), and every tenant's audit chain is intact (`audit.verify`). A
restore that silently lost rows in the middle of a chain fails the last check. A restore
with no tenant at all fails too, unless the caller allows it (a first install). A chain
cannot show that its newest records are missing: compare the tenant and record counts with
those taken when the backup was made (the runbook).

`filtered_toc` prepares a `pg_restore -l` listing of a `pg_dump -Fc --schema=<schema>` file
for a fresh database: views are left out and re-created by this release's own DDL
(`dashboards.views.view_statements`), because a deployment linked to the gateway
(`db link-usage`) has views over foreign tables whose server is not in a schema dump. After
the restore, `db link-usage` and `db dashboard-role` are run again (the runbook)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncEngine

from agentic_cp.audit import read_records, verify
from agentic_cp.db import current_revision, head_revision, revision_is_known
from agentic_cp.db_roles import enforcement_status
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import list_tenants

#: A `pg_restore -l` entry: `<id>; <catalog oid> <oid> <type> <schema> <name> <owner>`.
_ENTRY = re.compile(r"^(?P<id>\d+); \d+ \d+ (?P<rest>.*)$")
#: Entry types left to this release's DDL instead of the dump.
_SKIPPED_TYPES = ("VIEW ", "MATERIALIZED VIEW ")


def filtered_toc(toc: str) -> str:
    """`toc` with every view entry commented out (`;`), everything else as it was."""
    lines = []
    for line in toc.splitlines():
        found = _ENTRY.match(line)
        if found and found["rest"].startswith(_SKIPPED_TYPES):
            line = f";{line}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def schema_owner(toc: str, schema: str) -> str | None:
    """The role that owns `schema` in the dump (its SCHEMA entry), or None."""
    for line in toc.splitlines():
        found = _ENTRY.match(line)
        if found:
            parts = found["rest"].split()
            if parts[:3] == ["SCHEMA", "-", schema] and len(parts) == 4:
                return parts[3]
    return None


@dataclass
class TenantCheck:
    slug: str
    records: int
    problems: list[str] = field(default_factory=list)


@dataclass
class RestoreReport:
    """What a restored database looks like to this release."""

    revision: str | None
    head: str
    enforced: bool
    reasons: list[str] = field(default_factory=list)
    tenants: list[TenantCheck] = field(default_factory=list)
    allow_empty: bool = False
    """A restore without tenants passes only with this (a first install); otherwise an
    empty or wrong dump would read as verified (review F2)."""

    @property
    def revision_state(self) -> str:
        if self.revision is None:
            return "missing"
        if self.revision == self.head:
            return "current"
        return "older" if revision_is_known(self.revision) else "newer"

    @property
    def ok(self) -> bool:
        return (
            self.revision_state == "current"
            and self.enforced
            and (bool(self.tenants) or self.allow_empty)
            and all(not tenant.problems for tenant in self.tenants)
        )

    def lines(self) -> list[str]:
        """The report for people, one finding per line (no secret is ever in it)."""
        state = self.revision_state
        out = [f"migration revision: {self.revision or '-'} ({state}; this release: {self.head})"]
        if state == "older":
            out.append("  run `python -m agentic_cp migrate` before serving it")
        elif state == "newer":
            out.append("  verify it with the release that wrote it")
        out.append(f"database guarantees: {'enforced' if self.enforced else 'MISSING'}")
        out += [f"  - {reason}" for reason in self.reasons]
        if not self.tenants and self.allow_empty:
            out.append("tenants: none (allowed by --allow-empty: a first install)")
        elif not self.tenants:
            out.append(
                "tenants: none - an empty or wrong dump; pass --allow-empty only for a "
                "deployment that has no tenant yet"
            )
        for tenant in self.tenants:
            verdict = "intact" if not tenant.problems else f"BROKEN ({len(tenant.problems)})"
            out.append(f"tenant {tenant.slug}: audit chain {verdict}, {tenant.records} records")
            out += [f"  - {problem}" for problem in tenant.problems]
        out.append("restore verified" if self.ok else "restore NOT usable")
        return out


async def verify_restored(
    owner: AsyncEngine, schema: str, runtime_role: str, *, allow_empty: bool = False
) -> RestoreReport:
    """Check the database behind `owner` (an engine acting as the schema's owner role).
    Without `allow_empty`, a database with no tenant is not usable."""
    async with owner.connect() as conn:
        revision = await current_revision(conn)
        status = await enforcement_status(conn, runtime_role, schema=schema)
    report = RestoreReport(
        revision=revision,
        head=head_revision(),
        enforced=status.enforced,
        reasons=status.reasons,
        allow_empty=allow_empty,
    )
    if revision is None:
        return report
    async with owner.begin() as conn:
        found = await list_tenants(conn)
    for tenant in found:
        async with tenant_tx(owner, tenant.id) as conn:
            problems = await verify(conn)
            records = len(await read_records(conn))
        report.tenants.append(TenantCheck(tenant.slug, records, problems))
    return report
