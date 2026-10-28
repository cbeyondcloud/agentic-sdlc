"""Tenancy: RLS on `tenants` (own row only for the runtime role) and the worker's
`active_tenant_ids()` definer function.

`tenants` gets ENABLE without FORCE, so the owner role (operator commands) can create
tenants; every tenant-owned table gets ENABLE and FORCE through `enable_tenant_rls`.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22
"""

from alembic import op

from agentic_cp.tenancy.rls import OWN_TENANT_PREDICATE, schema_of

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = schema_of(op)
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY own_tenant ON tenants AS PERMISSIVE FOR ALL "
        f"USING ({OWN_TENANT_PREDICATE}) WITH CHECK ({OWN_TENANT_PREDICATE})"
    )
    # Functions are not executable by PUBLIC unless granted (a definer function is a hole
    # through RLS; grant_runtime grants each registered one to the runtime role).
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    )
    op.execute(
        f"CREATE FUNCTION active_tenant_ids() RETURNS SETOF uuid "
        f"LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, {schema} "
        f"AS $$ SELECT id FROM {schema}.tenants WHERE status = 'active' $$"
    )
    op.execute("REVOKE ALL ON FUNCTION active_tenant_ids() FROM PUBLIC")


def downgrade() -> None:
    schema = schema_of(op)
    op.execute("DROP FUNCTION active_tenant_ids()")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT EXECUTE ON FUNCTIONS TO PUBLIC")
    op.execute("DROP POLICY own_tenant ON tenants")
    op.execute("ALTER TABLE tenants DISABLE ROW LEVEL SECURITY")
