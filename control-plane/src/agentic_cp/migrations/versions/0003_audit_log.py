"""The per-tenant audit chain (M2-05), tenant-owned under forced RLS.

The runtime role gets SELECT and INSERT only (grant_runtime, INSERT_ONLY).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from agentic_cp.tenancy.rls import enable_tenant_rls

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("seq", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("at", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("actor_kind", sa.Text, nullable=False),
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("team", sa.Text, nullable=True),
        sa.Column("target", sa.Text, nullable=False),
        sa.Column("before", JSONB, nullable=True),
        sa.Column("after", JSONB, nullable=True),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("run_id", sa.Text, nullable=True),
        sa.Column("trace_id", sa.Text, nullable=True),
        sa.Column("policy_version", sa.Text, nullable=True),
        sa.Column("prev", sa.Text, nullable=False),
        sa.Column("hash", sa.Text, nullable=False),
        sa.CheckConstraint("actor_kind in ('human', 'service', 'system')", name="audit_actor_kind"),
        sa.CheckConstraint("seq > 0", name="audit_seq_positive"),
    )
    enable_tenant_rls(op, "audit_log")


def downgrade() -> None:
    op.drop_table("audit_log")
