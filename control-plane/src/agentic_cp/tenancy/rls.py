"""Row-level security for tenant-owned tables: the one way a migration makes a table
tenant-owned.

`ENABLE` and `FORCE` (so the owner role is bound too) and one `tenant_isolation` policy
whose `USING` and `WITH CHECK` compare `tenant_id` with the transaction's tenant. A missing
setting makes the predicate NULL: no rows, no inserts."""

from __future__ import annotations

import re

TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
OWN_TENANT_PREDICATE = "id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
POLICY = "tenant_isolation"
_IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def _ident(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise ValueError(f"unsafe identifier {name!r}")
    return name


def tenant_rls_statements(table: str) -> list[str]:
    """The DDL that makes `table` tenant-owned under forced RLS."""
    table = _ident(table)
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {POLICY} ON {table} AS PERMISSIVE FOR ALL "
        f"USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})",
    ]


def schema_of(op) -> str:
    """The schema a migration runs in (the connection's `search_path`), checked."""
    return _ident(op.get_bind().exec_driver_sql("select current_schema()").scalar())


def enable_tenant_rls(op, table: str) -> None:
    """Called by a migration right after it creates a tenant-owned table."""
    for statement in tenant_rls_statements(table):
        op.execute(statement)
