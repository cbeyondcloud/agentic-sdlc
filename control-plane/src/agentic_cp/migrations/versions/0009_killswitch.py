"""Kill and pause switches (M2-06, M2-10), tenant-owned under forced RLS, and the settings
run registration reads from each policy version (M2-08, M2-21).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from agentic_cp.tenancy.rls import enable_tenant_rls

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "policy_versions",
        sa.Column("settings", JSONB, nullable=False, server_default="{}"),
    )
    op.create_table(
        "kill_switches",
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()
        ),
        sa.Column("scope_kind", sa.Text, nullable=False),
        sa.Column("scope_value", sa.Text, nullable=False, server_default=""),
        sa.Column("team", sa.Text, nullable=True),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("cleared_by", sa.Text, nullable=True),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope_kind in ('tenant', 'team', 'user', 'repo', 'pipeline', 'agent', 'model', "
            "'backend')",
            name="kill_switches_scope_known",
        ),
        sa.CheckConstraint("mode in ('kill', 'pause')", name="kill_switches_mode_known"),
        sa.CheckConstraint(
            "(cleared_by is null) = (cleared_at is null)", name="kill_switches_cleared_pair"
        ),
    )
    enable_tenant_rls(op, "kill_switches")


def downgrade() -> None:
    op.drop_table("kill_switches")
    op.drop_column("policy_versions", "settings")
