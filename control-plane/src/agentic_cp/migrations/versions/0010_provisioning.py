"""The mail outbox (M2-07), invites (M2-18) and proxy-key records (M2-17), tenant-owned
under forced RLS; the run budget and spend (M2-16); a tenant's own mail sender; and
`resolve_invite`.

`resolve_invite(sha256)` maps an invite token's hash to its tenant, invite and user before
any tenant is known, exactly as `resolve_token` does for bearer tokens (migration 0004): a
registered SECURITY DEFINER function with a pinned search_path that opens the owner-only,
SELECT-only `credential_lookup` policy on `invites` (and `users`) while its query runs.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import BYTEA, UUID

from agentic_cp.tenancy.rls import enable_tenant_rls, schema_of

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

LOOKUP = "current_setting('app.credential_lookup', true) = 'on'"


def _tenant_id() -> sa.Column:
    return sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True)


def _id() -> sa.Column:
    return sa.Column(
        "id", UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()
    )


def _at(name: str, *, nullable: bool = False) -> sa.Column:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _user_fkey(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "user_id"],
        ["users.tenant_id", "users.id"],
        name=f"{table}_tenant_id_user_id_fkey",
    )


def upgrade() -> None:
    schema = schema_of(op)
    op.add_column("tenants", sa.Column("mail_from", sa.Text, nullable=True))
    op.add_column("tenants", sa.Column("mail_from_name", sa.Text, nullable=True))
    op.add_column("runs", sa.Column("budget_usd", sa.Numeric, nullable=True))
    op.add_column("runs", sa.Column("spent_usd", sa.Numeric, nullable=False, server_default="0"))
    op.create_table(
        "notifications",
        _tenant_id(),
        _id(),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("recipient", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("body_text", sa.Text, nullable=False),
        sa.Column("body_html", sa.Text, nullable=False),
        sa.Column("dedupe_key", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        _at("next_attempt_at"),
        sa.Column("last_error", sa.Text, nullable=True),
        _at("created_at"),
        _at("sent_at", nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "dedupe_key", name="notifications_tenant_id_dedupe_key_key"
        ),
        sa.CheckConstraint(
            "status in ('pending', 'sent', 'failed')", name="notifications_status_known"
        ),
    )
    op.create_table(
        "invites",
        _tenant_id(),
        _id(),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("token_sha256", BYTEA, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        _at("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _at("used_at", nullable=True),
        _at("revoked_at", nullable=True),
        sa.UniqueConstraint("token_sha256", name="invites_token_sha256_key"),
        _user_fkey("invites"),
    )
    op.create_table(
        "proxy_keys",
        _tenant_id(),
        sa.Column("key_id", sa.Text, primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("team", sa.Text, nullable=True),
        sa.Column("created_by", sa.Text, nullable=False),
        _at("created_at"),
        _at("revoke_requested_at", nullable=True),
        _at("revoked_at", nullable=True),
        _user_fkey("proxy_keys"),
        sa.CheckConstraint("key_id ~ '^[0-9a-f]{16,64}$'", name="proxy_keys_key_id_format"),
    )
    for table in ("notifications", "invites", "proxy_keys"):
        enable_tenant_rls(op, table)
    op.execute(
        "CREATE POLICY credential_lookup ON invites AS PERMISSIVE FOR SELECT "
        f"TO CURRENT_USER USING ({LOOKUP})"
    )
    op.execute(
        "CREATE FUNCTION resolve_invite(invite_hash bytea) RETURNS TABLE ("
        "tenant_id uuid, tenant_status text, invite_id uuid, user_id uuid, email text, "
        "user_kind text, user_status text, expires_at timestamptz, used_at timestamptz, "
        "revoked_at timestamptz) "
        f"LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, {schema} "
        "AS $$\n"
        "#variable_conflict use_column\n"
        "BEGIN\n"
        "  PERFORM set_config('app.credential_lookup', 'on', true);\n"
        "  RETURN QUERY SELECT t.id, t.status, i.id, u.id, u.email, u.kind, u.status, "
        "i.expires_at, i.used_at, i.revoked_at "
        f"FROM {schema}.invites i "
        f"JOIN {schema}.users u ON u.tenant_id = i.tenant_id AND u.id = i.user_id "
        f"JOIN {schema}.tenants t ON t.id = i.tenant_id "
        "WHERE i.token_sha256 = invite_hash LIMIT 1;\n"
        "  PERFORM set_config('app.credential_lookup', '', true);\n"
        "END\n"
        "$$"
    )
    op.execute("REVOKE ALL ON FUNCTION resolve_invite(bytea) FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP FUNCTION resolve_invite(bytea)")
    for table in ("proxy_keys", "invites", "notifications"):
        op.drop_table(table)
    op.drop_column("runs", "spent_usd")
    op.drop_column("runs", "budget_usd")
    op.drop_column("tenants", "mail_from_name")
    op.drop_column("tenants", "mail_from")
