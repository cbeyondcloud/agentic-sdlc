"""Tenant-bound Grafana reader roles (M2-37, ruling T1).

`bind_reader` (owner role, inside the tenant's transaction) makes one login role the
tenant's reader: it may `SELECT` the `dash_*` views and nothing else (no table, no gateway
link), its role default `app.tenant_id` is the tenant (in this database only), and
`tenants.dashboard_role` names it, which is what `dash_scope` checks. A reader that sets
`app.tenant_id` to another tenant therefore reads nothing. Re-binding a tenant to another
role takes the grants and the default away from the old one. Every bind is on the tenant's
audit chain (`dashboard.reader_bound`).

The views run with their owner's rights, so the owner must be subject to row-level security:
not a superuser and without BYPASSRLS (checked here). The owner sets the reader's role
default, for which a superuser grants once `GRANT SET ON PARAMETER app.tenant_id TO
<owner>` (Postgres 15+)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.audit import OPERATOR, AuditEvent, append
from agentic_cp.dashboards.views import DASH_VIEWS, LINK_SUFFIX
from agentic_cp.db_roles import scram_verifier
from agentic_cp.tables import tenants
from agentic_cp.tenancy.context import SETTING, require_tenant_id

log = logging.getLogger("agentic_cp.dashboards.readers")

_IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}")


class ReaderError(Exception):
    """The reader role could not be bound; nothing changed."""


def checked_role(name: str) -> str:
    """A reader role name: a lower-case Postgres identifier (it is interpolated into DDL)."""
    if not _IDENT.fullmatch(name):
        raise ReaderError(
            f"{name!r} is not a role name: lower-case letters, digits and '_', starting "
            "with a letter or '_', at most 63"
        )
    return name


async def _execute(conn: AsyncConnection, statement: str, shown: str | None = None) -> None:
    log.info("readers: %s", shown or statement)
    await conn.exec_driver_sql(statement)


async def _one(conn: AsyncConnection, statement: str, *params):
    return (await conn.exec_driver_sql(statement, params)).first()


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _check_views_owner(conn: AsyncConnection, schema: str) -> None:
    row = await _one(
        conn,
        "select r.rolname, r.rolsuper, r.rolbypassrls from pg_views v "
        "join pg_roles r on r.rolname = v.viewowner "
        "where v.schemaname = $1 and v.viewname = 'dash_scope'",
        schema,
    )
    if row is None:
        raise ReaderError("the dashboard views are missing: run `python -m agentic_cp migrate`")
    owner, superuser, bypass = row
    if superuser or bypass:
        what = "a superuser" if superuser else "a role with BYPASSRLS"
        raise ReaderError(
            f"the dashboard views are owned by {owner}, {what}: views run with their "
            "owner's rights, so row-level security would not apply (migrate as an owner "
            "role that is neither)"
        )


async def _check_role(conn: AsyncConnection, role: str, runtime_role: str) -> bool:
    """Refuse a role that is not a plain login; True when the role exists already."""
    me = (await _one(conn, "select current_user"))[0]
    if role in (me, runtime_role):
        raise ReaderError(f"{role} is the control plane's own role, never a dashboard reader")
    row = await _one(
        conn,
        "select oid, rolsuper or rolbypassrls or rolcreaterole or rolcreatedb or "
        "rolreplication from pg_roles where rolname = $1",
        role,
    )
    if row is None:
        return False
    oid, privileged = row
    if privileged:
        raise ReaderError(f"{role} holds role attributes a dashboard reader must not have")
    owned = await _one(
        conn,
        "select (select count(*) from pg_class where relowner = $1) "
        "+ (select count(*) from pg_proc where proowner = $1) "
        "+ (select count(*) from pg_namespace where nspowner = $1)",
        oid,
    )
    if owned[0]:
        raise ReaderError(f"{role} owns database objects; a dashboard reader owns nothing")
    return True


async def _revoke(conn: AsyncConnection, schema: str, role: str, database: str) -> None:
    await _execute(conn, f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role}")
    await _execute(conn, f"REVOKE ALL ON SCHEMA {schema} FROM {role}")
    await _execute(conn, f"ALTER ROLE {role} IN DATABASE {database} RESET ALL")


async def bind_reader(
    conn: AsyncConnection,
    schema: str,
    role: str,
    password: str | None,
    *,
    runtime_role: str,
    clock: Callable[[], datetime],
) -> None:
    """Make `role` the reader of the transaction's tenant (see the module docstring).
    `password=None` keeps an existing role's password; a new role needs one."""
    tenant_id = await require_tenant_id(conn)
    role = checked_role(role)
    if not _IDENT.fullmatch(schema):
        raise ReaderError(f"unsafe schema name {schema!r}")
    await _check_views_owner(conn, schema)
    exists = await _check_role(conn, role, runtime_role)
    if not exists and not password:
        raise ReaderError(f"{role} is a new role: give it a password")
    other = await _one(
        conn, "select slug from tenants where dashboard_role = $1 and id <> $2", role, tenant_id
    )
    if other is not None:
        raise ReaderError(f"{role} is already the dashboard reader of tenant {other[0]}")
    old = (
        await conn.execute(select(tenants.c.dashboard_role).where(tenants.c.id == tenant_id))
    ).scalar_one()
    database = _quoted((await _one(conn, "select current_database()"))[0])

    verb = "ALTER" if exists else "CREATE"
    statement = f"{verb} ROLE {role} LOGIN NOINHERIT"
    shown = statement
    if password:
        statement += f" PASSWORD '{scram_verifier(password)}'"
        shown += " PASSWORD <scram verifier>"
    await _execute(conn, statement, shown)
    parents = await conn.exec_driver_sql(
        "select r.rolname from pg_auth_members m join pg_roles r on r.oid = m.roleid "
        "join pg_roles me on me.oid = m.member where me.rolname = $1",
        (role,),
    )
    for (parent,) in parents.all():
        await _execute(conn, f"REVOKE {_quoted(parent)} FROM {role}")
    if old and old != role and await _one(conn, "select 1 from pg_roles where rolname = $1", old):
        await _revoke(conn, schema, checked_role(old), database)
    await _revoke(conn, schema, role, database)
    link = schema + LINK_SUFFIX
    if await _one(conn, "select 1 from pg_namespace where nspname = $1", link):
        await _execute(conn, f"REVOKE ALL ON SCHEMA {link} FROM {role}")
    await _execute(conn, f"GRANT USAGE ON SCHEMA {schema} TO {role}")
    for view in DASH_VIEWS:
        await _execute(conn, f"GRANT SELECT ON {view} TO {role}")
    try:
        await _execute(
            conn, f"ALTER ROLE {role} IN DATABASE {database} SET {SETTING} = '{tenant_id}'"
        )
    except DBAPIError:
        raise ReaderError(
            f"the owner role may not set {SETTING} for other roles: a superuser runs "
            f"`GRANT SET ON PARAMETER {SETTING} TO <owner role>` once"
        ) from None
    await _execute(conn, f"ALTER ROLE {role} IN DATABASE {database} SET search_path = {schema}")
    await conn.execute(update(tenants).where(tenants.c.id == tenant_id).values(dashboard_role=role))
    await append(
        conn,
        AuditEvent(
            event="dashboard.reader_bound",
            actor=OPERATOR,
            actor_kind="system",
            target=f"role:{role}",
            before={"role": old} if old else None,
            after={"role": role},
        ),
        clock=clock,
    )
