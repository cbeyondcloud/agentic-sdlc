"""Database roles and the guarantees the running service depends on.

Two roles (pattern from TokenLean `src/proxy/audit/enforcement.py`, Apache-2.0, same
owner; re-implemented here and extended with the row-level-security checks):

* the **owner** role runs migrations and grants and owns every object;
* the **runtime** role, the API's, owns nothing, is `NOSUPERUSER NOBYPASSRLS NOCREATEROLE
  NOINHERIT`, belongs to no role, may only `SELECT, INSERT` on the evidence tables, sees
  only its own row of `tenants` and may change only its `rbac_revision`, and may execute
  only the registered `SECURITY DEFINER` functions.

`enforcement_status` checks all of that from the catalogue, failing closed: a check that
cannot run counts against. `/readyz` and `serve` refuse to work on a database that does not
enforce it (ND-5, DR-4)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.tables import TENANT_TABLES
from agentic_cp.tenancy.rls import POLICY

log = logging.getLogger("agentic_cp.db_roles")

#: The only SECURITY DEFINER functions allowed in the schema: name -> signature. Each maps
#: a credential (or nothing) to tenant ids only, by exact match (DR-3).
DEFINER_FUNCTIONS: dict[str, str] = {
    "active_tenant_ids": "active_tenant_ids()",
    "resolve_token": "resolve_token(bytea)",
}

#: Extra policies a tenant-owned table may carry besides `tenant_isolation`: table ->
#: policy name. Each is `FOR SELECT`, granted to the owner role only, and active only while
#: a registered definer function runs (ruling R1).
LOOKUP_POLICIES: dict[str, str] = {
    "tokens": "credential_lookup",
    "users": "credential_lookup",
}

#: Evidence tables: the runtime role may only SELECT and INSERT (M2-05).
INSERT_ONLY: tuple[str, ...] = ("audit_log", "run_events", "policy_versions")

TENANCY = "tenancy.unenforced"
AUDIT = "audit.unenforced"
_IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def _ident(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise ValueError(f"unsafe identifier {name!r}")
    return name


def scram_verifier(password: str, *, iterations: int = 4096, salt: bytes | None = None) -> str:
    """The SCRAM-SHA-256 verifier Postgres stores for `password` (RFC 5802/7677), so the
    password itself is never sent to the server or written to its logs."""
    salt = salt or secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


async def _execute(conn: AsyncConnection, statement: str, shown: str | None = None) -> None:
    log.info("db_roles: %s", shown or statement)
    await conn.exec_driver_sql(statement)


async def _existing_tables(conn: AsyncConnection, schema: str) -> set[str]:
    rows = await conn.execute(
        text("select tablename from pg_tables where schemaname = :s"), {"s": schema}
    )
    return {row[0] for row in rows}


async def _existing_functions(conn: AsyncConnection, schema: str) -> set[str]:
    rows = await conn.execute(
        text(
            "select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
            "where n.nspname = :s"
        ),
        {"s": schema},
    )
    return {row[0] for row in rows}


async def runtime_role_exists(conn: AsyncConnection, role: str) -> bool:
    found = await conn.execute(text("select 1 from pg_roles where rolname = :r"), {"r": role})
    return found.first() is not None


async def grant_runtime(
    owner_conn: AsyncConnection,
    role: str,
    password: str | None,
    schema: str,
    *,
    tenant_tables: tuple[str, ...] | None = None,
) -> None:
    """Create or re-align the runtime role (idempotent; `migrate` re-runs it after every
    upgrade). `password=None` leaves the password as it is. The password is sent as a
    SCRAM verifier and never logged."""
    role, schema = _ident(role), _ident(schema)
    tables = TENANT_TABLES if tenant_tables is None else tenant_tables
    found = await owner_conn.execute(
        text("select rolcreaterole, rolcreatedb from pg_roles where rolname = :r"), {"r": role}
    )
    existing_role = found.first()
    if existing_role is None:
        # The defaults are NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION.
        statement = f"CREATE ROLE {role} LOGIN NOINHERIT"
    else:
        # A non-superuser owner may only name attributes it may change; SUPERUSER,
        # BYPASSRLS and REPLICATION on an existing role are reported by enforcement_status.
        attributes = ["LOGIN", "NOINHERIT"]
        attributes += ["NOCREATEROLE"] if existing_role[0] else []
        attributes += ["NOCREATEDB"] if existing_role[1] else []
        statement = f"ALTER ROLE {role} {' '.join(attributes)}"
    shown = statement
    if password:
        statement += f" PASSWORD '{scram_verifier(password)}'"
        shown += " PASSWORD <scram verifier>"
    await _execute(owner_conn, statement, shown)
    parents = await owner_conn.execute(
        text(
            "select r.rolname from pg_auth_members m join pg_roles r on r.oid = m.roleid "
            "join pg_roles me on me.oid = m.member where me.rolname = :r"
        ),
        {"r": role},
    )
    for (parent,) in parents.all():
        await _execute(owner_conn, f'REVOKE "{parent}" FROM {role}')

    await _execute(owner_conn, f"GRANT USAGE ON SCHEMA {schema} TO {role}")
    await _execute(owner_conn, f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role}")
    existing = await _existing_tables(owner_conn, schema)
    for table in tables:
        if table not in existing:
            continue
        rights = "SELECT, INSERT" if table in INSERT_ONLY else "SELECT, INSERT, UPDATE, DELETE"
        await _execute(owner_conn, f"GRANT {rights} ON {_ident(table)} TO {role}")
    await _execute(owner_conn, f"GRANT SELECT ON tenants TO {role}")
    await _execute(owner_conn, f"GRANT UPDATE (rbac_revision) ON tenants TO {role}")
    if "alembic_version" in existing:
        await _execute(owner_conn, f"GRANT SELECT ON alembic_version TO {role}")

    await _execute(owner_conn, f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM {role}")
    functions = await _existing_functions(owner_conn, schema)
    for name, signature in DEFINER_FUNCTIONS.items():
        if name in functions:
            await _execute(owner_conn, f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
            await _execute(owner_conn, f"GRANT EXECUTE ON FUNCTION {signature} TO {role}")


# ── enforcement status ────────────────────────────────────────────────────────


@dataclass
class EnforcementStatus:
    """Whether the database enforces tenant isolation and insert-only evidence."""

    enforced: bool = True
    reasons: list[str] = field(default_factory=list)
    codes: set[str] = field(default_factory=set)

    def fail(self, code: str, reason: str) -> None:
        self.enforced = False
        self.codes.add(code)
        self.reasons.append(reason)


async def enforcement_status(
    conn: AsyncConnection,
    role: str | None = None,
    *,
    schema: str | None = None,
    tenant_tables: tuple[str, ...] | None = None,
) -> EnforcementStatus:
    """Check `role` (default: the connected role) and the schema (default: the current
    one) against every guarantee above. Never raises: a failed check is a reason."""
    status = EnforcementStatus()
    tables = TENANT_TABLES if tenant_tables is None else tenant_tables
    try:
        if schema is None:
            schema = (await conn.execute(text("select current_schema()"))).scalar_one()
        await _check(conn, status, role, _ident(schema), tables)
    except Exception as error:  # fail closed
        log.warning("enforcement check failed", exc_info=error)
        status.fail(TENANCY, f"the enforcement check could not run ({type(error).__name__})")
    return status


async def _check(
    conn: AsyncConnection,
    status: EnforcementStatus,
    role: str | None,
    schema: str,
    tables: tuple[str, ...],
) -> None:
    found = await conn.execute(
        text(
            "select oid, rolname, rolsuper, rolbypassrls from pg_roles "
            "where rolname = coalesce(:r, current_user)"
        ),
        {"r": role},
    )
    row = found.first()
    if row is None:
        status.fail(TENANCY, f"the runtime role {role!r} does not exist")
        return
    oid, name, superuser, bypass = row
    if superuser:
        status.fail(TENANCY, f"the runtime role {name} is a superuser")
    if bypass:
        status.fail(TENANCY, f"the runtime role {name} has BYPASSRLS")
    member_of = await _scalar(
        conn, "select count(*) from pg_auth_members where member = CAST(:o AS oid)", oid
    )
    if member_of:
        status.fail(TENANCY, f"the runtime role {name} is a member of another role")
    owned = await _scalar(
        conn,
        "select (select count(*) from pg_class where relowner = CAST(:o AS oid)) "
        "+ (select count(*) from pg_proc where proowner = CAST(:o AS oid)) "
        "+ (select count(*) from pg_namespace where nspowner = CAST(:o AS oid))",
        oid,
    )
    if owned:
        status.fail(TENANCY, f"the runtime role {name} owns database objects")

    rls = await conn.execute(
        text(
            "select c.relname, c.relrowsecurity, c.relforcerowsecurity from pg_class c "
            "join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = :s and c.relkind in ('r', 'p')"
        ),
        {"s": schema},
    )
    present = {r[0]: (r[1], r[2]) for r in rls}
    for stray in sorted(set(present) - {"tenants", "alembic_version"} - set(tables)):
        status.fail(TENANCY, f"table {stray} is neither tenants nor a registered tenant table")
    policies = await conn.execute(
        text(
            "select c.relname, p.polname, p.polcmd::text, p.polpermissive, p.polroles::oid[], "
            "coalesce(pg_get_expr(p.polqual, p.polrelid), ''), "
            "coalesce(pg_get_expr(p.polwithcheck, p.polrelid), '') "
            "from pg_policy p join pg_class c on c.oid = p.polrelid "
            "join pg_namespace n on n.oid = c.relnamespace where n.nspname = :s"
        ),
        {"s": schema},
    )
    by_table: dict[str, dict[str, tuple]] = {}
    for table, policy, command, permissive, roles, using, check in policies:
        by_table.setdefault(table, {})[policy] = (command, permissive, list(roles), using, check)
    for table in tables:
        if table not in present:
            continue  # a later migration creates it; grant_runtime runs again then
        enabled, forced = present[table]
        if not (enabled and forced):
            status.fail(TENANCY, f"table {table} does not force row-level security")
        table_policies = dict(by_table.get(table, {}))
        isolation = table_policies.pop(POLICY, None)
        if not _is_isolation(isolation, "tenant_id"):
            status.fail(TENANCY, f"table {table} lacks the {POLICY} policy")
        for extra, (command, permissive, roles, using, _) in table_policies.items():
            if LOOKUP_POLICIES.get(table) != extra:
                status.fail(TENANCY, f"table {table} has an unregistered policy {extra}")
            elif (
                command != "r"
                or not permissive
                or 0 in roles
                or oid in roles
                or "app.credential_lookup" not in using
            ):
                status.fail(TENANCY, f"lookup policy {extra} on {table} is wider than allowed")
    enabled, _ = present.get("tenants", (False, False))
    tenant_policies = dict(by_table.get("tenants", {}))
    if not enabled or not _is_isolation(tenant_policies.pop("own_tenant", None), "id"):
        status.fail(TENANCY, "table tenants lacks row-level security or its own_tenant policy")
    for extra in tenant_policies:
        status.fail(TENANCY, f"table tenants has an unregistered policy {extra}")
    if "tenants" in present:
        writable = await _scalar(
            conn,
            f"select has_column_privilege(CAST(:o AS oid), '{schema}.tenants', 'name', 'UPDATE') "
            f"or has_table_privilege(CAST(:o AS oid), '{schema}.tenants', 'INSERT') "
            f"or has_table_privilege(CAST(:o AS oid), '{schema}.tenants', 'DELETE') "
            f"or has_table_privilege(CAST(:o AS oid), '{schema}.tenants', 'TRUNCATE')",
            oid,
        )
        if writable:
            status.fail(TENANCY, f"the runtime role {name} may change tenants beyond its revision")

    for table in INSERT_ONLY:
        if table not in present:
            continue
        label = "audit" if table == "audit_log" else table
        changes = await _scalar(
            conn,
            f"select has_table_privilege(CAST(:o AS oid), '{schema}.{table}', 'UPDATE') "
            f"or has_table_privilege(CAST(:o AS oid), '{schema}.{table}', 'DELETE') "
            f"or has_table_privilege(CAST(:o AS oid), '{schema}.{table}', 'TRUNCATE')",
            oid,
        )
        if changes:
            status.fail(AUDIT, f"the runtime role {name} may change {label} rows directly")
        if table not in tables:
            status.fail(AUDIT, f"evidence table {table} is not a registered tenant table")

    definers = await conn.execute(
        text(
            "select p.proname, coalesce(p.proconfig, '{}'), p.proacl::text[] from pg_proc p "
            "join pg_namespace n on n.oid = p.pronamespace "
            "where n.nspname = :s and p.prosecdef"
        ),
        {"s": schema},
    )
    seen = set()
    pinned = f"search_path=pg_catalog, {schema}"
    for function, config, acl in definers:
        seen.add(function)
        if function not in DEFINER_FUNCTIONS:
            status.fail(TENANCY, f"SECURITY DEFINER function {function} is not registered")
        if pinned not in config:
            status.fail(TENANCY, f"SECURITY DEFINER function {function} has no pinned search_path")
        if acl is None or any(item.startswith("=") for item in acl):
            status.fail(TENANCY, f"SECURITY DEFINER function {function} is executable by PUBLIC")
    for missing in sorted(set(DEFINER_FUNCTIONS) - seen):
        status.fail(TENANCY, f"SECURITY DEFINER function {missing} is missing")


def _is_isolation(policy: tuple | None, column: str) -> bool:
    if policy is None:
        return False
    command, permissive, roles, using, check = policy
    return (
        command == "*"
        and permissive
        and roles == [0]
        and all("app.tenant_id" in expr and column in expr for expr in (using, check))
    )


async def _scalar(conn: AsyncConnection, statement: str, oid: int):
    return (await conn.execute(text(statement), {"o": oid})).scalar()
