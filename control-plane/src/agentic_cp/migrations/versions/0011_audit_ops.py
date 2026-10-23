"""Auto-trip state per team (M2-11), tenant-owned under forced RLS, and an index for the
spend and error rates it reads from recently received run events.

Revision ID: 0011_audit_ops
Revises: 0010
Create Date: 2026-10-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

from agentic_cp.tenancy.rls import enable_tenant_rls

revision = "0011_audit_ops"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "run_events_tenant_id_received_at_idx", "run_events", ["tenant_id", "received_at"]
    )
    op.create_table(
        "auto_trips",
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("team", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, primary_key=True),
        sa.Column("state", sa.Text, nullable=False, server_default="armed"),
        sa.Column("switch_id", UUID(as_uuid=True), nullable=True),
        sa.Column("value", sa.Text, nullable=True),
        sa.Column("threshold", sa.Text, nullable=True),
        sa.Column(
            "changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "switch_id"],
            ["kill_switches.tenant_id", "kill_switches.id"],
            name="auto_trips_tenant_id_switch_id_fkey",
        ),
        sa.CheckConstraint("kind in ('spend', 'errors')", name="auto_trips_kind_known"),
        sa.CheckConstraint(
            "state in ('armed', 'tripped', 'disarmed')", name="auto_trips_state_known"
        ),
    )
    enable_tenant_rls(op, "auto_trips")


def downgrade() -> None:
    op.drop_table("auto_trips")
    op.drop_index("run_events_tenant_id_received_at_idx", table_name="run_events")
