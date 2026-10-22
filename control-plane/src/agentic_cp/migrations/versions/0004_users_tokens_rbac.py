"""Users, tokens, roles and assignments (tenant-owned, forced RLS), and `resolve_token`.

`resolve_token(sha256)` maps a token hash to its tenant and principal before any tenant is
known. It is a registered SECURITY DEFINER function with a pinned search_path; while its
query runs, `app.credential_lookup` is 'on' (set and cleared inside the body), which opens
the owner-only, SELECT-only `credential_lookup` policy on `tokens` and `users` (ruling R1:
FORCE RLS binds the owner too). The runtime role never matches that policy.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, UUID

from agentic_cp.tenancy.rls import enable_tenant_rls, schema_of

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

LOOKUP = "current_setting('app.credential_lookup', true) = 'on'"


def _tenant_id() -> sa.Column:
    return sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True)


def _id() -> sa.Column:
    return sa.Column(
        "id", UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()
    )


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    schema = schema_of(op)
    op.create_table(
        "users",
        _tenant_id(),
        _id(),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False, server_default=""),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("handles", ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "email", name="users_tenant_id_email_key"),
        sa.CheckConstraint("email = lower(email)", name="users_email_lower"),
        sa.CheckConstraint("kind in ('human', 'service')", name="users_kind_known"),
        sa.CheckConstraint("status in ('active', 'disabled')", name="users_status_known"),
    )
    op.create_table(
        "tokens",
        _tenant_id(),
        _id(),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("token_sha256", BYTEA, nullable=False),
        _created_at(),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_sha256", name="tokens_token_sha256_key"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="tokens_tenant_id_user_id_fkey",
        ),
    )
    op.create_table(
        "roles",
        _tenant_id(),
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("permissions", ARRAY(sa.Text), nullable=False),
        sa.Column("builtin", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("team_assignable", sa.Boolean, nullable=False, server_default="false"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("name ~ '^[a-z][a-z0-9-]{1,62}$'", name="roles_name_format"),
    )
    op.create_table(
        "assignments",
        _tenant_id(),
        _id(),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("scope_kind", sa.Text, nullable=False),
        sa.Column("scope_value", sa.Text, nullable=False, server_default=""),
        sa.Column("created_by", sa.Text, nullable=False),
        _created_at(),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "role",
            "scope_kind",
            "scope_value",
            name="assignments_tenant_id_user_id_role_scope_kind_scope_value_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="assignments_tenant_id_user_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "role"],
            ["roles.tenant_id", "roles.name"],
            name="assignments_tenant_id_role_fkey",
        ),
        sa.CheckConstraint("scope_kind in ('org', 'team')", name="assignments_scope_known"),
        sa.CheckConstraint(
            "(scope_kind = 'org' and scope_value = '') "
            "or (scope_kind = 'team' and scope_value <> '')",
            name="assignments_scope_value",
        ),
    )
    for table in ("users", "tokens", "roles", "assignments"):
        enable_tenant_rls(op, table)
    for table in ("tokens", "users"):
        op.execute(
            f"CREATE POLICY credential_lookup ON {table} AS PERMISSIVE FOR SELECT "
            f"TO CURRENT_USER USING ({LOOKUP})"
        )
    # The switch is set inside the body, not as a function-level SET: Postgres lets only a
    # superuser attach a placeholder setting to a function. It is transaction-local and
    # cleared before returning; an error aborts the transaction, which undoes it too.
    op.execute(
        "CREATE FUNCTION resolve_token(token_hash bytea) RETURNS TABLE ("
        "tenant_id uuid, tenant_slug text, tenant_status text, rbac_revision bigint, "
        "token_id uuid, user_id uuid, email text, kind text, user_status text, "
        "expires_at timestamptz, revoked_at timestamptz) "
        f"LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, {schema} "
        "AS $$\n"
        "#variable_conflict use_column\n"
        "BEGIN\n"
        "  PERFORM set_config('app.credential_lookup', 'on', true);\n"
        "  RETURN QUERY SELECT t.id, t.slug, t.status, t.rbac_revision, k.id, u.id, u.email, "
        "u.kind, u.status, k.expires_at, k.revoked_at "
        f"FROM {schema}.tokens k "
        f"JOIN {schema}.users u ON u.tenant_id = k.tenant_id AND u.id = k.user_id "
        f"JOIN {schema}.tenants t ON t.id = k.tenant_id "
        "WHERE k.token_sha256 = token_hash LIMIT 1;\n"
        "  PERFORM set_config('app.credential_lookup', '', true);\n"
        "END\n"
        "$$"
    )
    op.execute("REVOKE ALL ON FUNCTION resolve_token(bytea) FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP FUNCTION resolve_token(bytea)")
    for table in ("assignments", "roles", "tokens", "users"):
        op.drop_table(table)
