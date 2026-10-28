"""The routing outbox (M2-09) and personal skill and note backups (M2-20), both tenant-owned
under forced RLS. `personal_events` is evidence: the runtime role may only SELECT and
INSERT it (`db_roles.INSERT_ONLY`).

Revision ID: m2_09_routing
Revises: m2_37_usage_views
Create Date: 2026-10-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from agentic_cp.tenancy.rls import enable_tenant_rls

revision = "m2_09_routing"
down_revision = "m2_37_usage_views"
branch_labels = None
depends_on = None


def _tenant_id() -> sa.Column:
    return sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True)


def _at(name: str, *, nullable: bool = False) -> sa.Column:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _texts(name: str) -> sa.Column:
    return sa.Column(name, ARRAY(sa.Text), nullable=False, server_default="{}")


def upgrade() -> None:
    op.create_table(
        "routing_applies",
        _tenant_id(),
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()
        ),
        sa.Column("policy_version", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        _at("next_attempt_at"),
        sa.Column("last_error", sa.Text, nullable=True),
        _texts("problems"),
        _texts("warnings"),
        sa.Column("plan_sha256", sa.Text, nullable=True),
        sa.Column("gateway_version", sa.BigInteger, nullable=True),
        sa.Column("previous_gateway_version", sa.BigInteger, nullable=True),
        _texts("per_team"),
        sa.Column("requested_by", sa.Text, nullable=False),
        _at("created_at"),
        _at("applied_at", nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "policy_version"],
            ["policy_versions.tenant_id", "policy_versions.version"],
            name="routing_applies_tenant_id_policy_version_fkey",
        ),
        sa.CheckConstraint(
            "status in ('pending', 'applied', 'invalid', 'failed', 'superseded', 'skipped')",
            name="routing_applies_status_known",
        ),
    )
    op.create_table(
        "personal_events",
        _tenant_id(),
        sa.Column("user_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("device", sa.Text, primary_key=True),
        sa.Column("seq", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("hash", sa.Text, nullable=False),
        sa.Column("prev", sa.Text, nullable=False),
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("entry", JSONB, nullable=False),
        _at("received_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="personal_events_tenant_id_user_id_fkey",
        ),
        sa.CheckConstraint("seq > 0", name="personal_events_seq_positive"),
        sa.CheckConstraint(
            "event in ('personal.saved', 'personal.removed', 'personal.held')",
            name="personal_events_event_known",
        ),
        sa.CheckConstraint("kind in ('skill', 'memory')", name="personal_events_kind_known"),
    )
    for table in ("routing_applies", "personal_events"):
        enable_tenant_rls(op, table)


def downgrade() -> None:
    op.drop_table("personal_events")
    op.drop_table("routing_applies")
