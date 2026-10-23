"""Policy sync (M2-02): `policy_versions` (insert-only evidence) and `policy_active`, both
tenant-owned under forced RLS, and `resolve_webhook`.

`resolve_webhook(sha256)` maps a policy-repo webhook token's hash to its tenant (ND-14):
a registered SECURITY DEFINER function with a pinned search_path, exact match, active
tenants only, returning the id and nothing else. `tenants` is not FORCEd, so the owner
running the function reads it without a lookup switch.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, UUID

from agentic_cp.tenancy.rls import enable_tenant_rls, schema_of

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _tenant_id() -> sa.Column:
    return sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True)


def upgrade() -> None:
    schema = schema_of(op)
    op.create_table(
        "policy_versions",
        _tenant_id(),
        sa.Column("version", sa.Text, primary_key=True),
        sa.Column("commit", sa.Text, nullable=False),
        sa.Column("branch", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("problems", ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("teams", ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("bundle", BYTEA, nullable=True),
        sa.Column("digest", sa.Text, nullable=True),
        sa.Column("tree_digest", sa.Text, nullable=True),
        sa.Column("commit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status in ('accepted', 'rejected')", name="policy_versions_status_known"
        ),
        sa.CheckConstraint(
            "(status = 'accepted') = (bundle is not null and digest is not null)",
            name="policy_versions_bundle_iff_accepted",
        ),
    )
    op.create_table(
        "policy_active",
        _tenant_id(),
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("branch", sa.Text, nullable=False),
        sa.Column(
            "activated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "version"],
            ["policy_versions.tenant_id", "policy_versions.version"],
            name="policy_active_tenant_id_version_fkey",
        ),
    )
    for table in ("policy_versions", "policy_active"):
        enable_tenant_rls(op, table)
    op.execute(
        "CREATE FUNCTION resolve_webhook(hook_hash bytea) RETURNS uuid "
        f"LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, {schema} "
        f"AS $$ SELECT id FROM {schema}.tenants "
        "WHERE webhook_token_sha256 = hook_hash AND status = 'active' LIMIT 1 $$"
    )
    op.execute("REVOKE ALL ON FUNCTION resolve_webhook(bytea) FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP FUNCTION resolve_webhook(bytea)")
    for table in ("policy_active", "policy_versions"):
        op.drop_table(table)
