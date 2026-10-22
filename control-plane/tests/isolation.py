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

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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

    `make_object_in_b(app_ctx)` creates an object in tenant b and returns the values for
    the path template (e.g. `{"id": ...}`); the route, called with tenant a's token, must
    answer exactly like a missing object (DR-5). `must_not_list` routes are list routes:
    the object's id must not appear in their body."""

    family: str
    method: str
    path: str
    make_object_in_b: Callable[[Any], Awaitable[dict[str, str]]]
    body: dict[str, Any] | None = None
    must_not_list: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def id(self) -> str:
        return f"{self.family}-{self.method} {self.path}"


#: filled by the tasks that add object routes
ENDPOINT_CASES: list[EndpointCase] = []
