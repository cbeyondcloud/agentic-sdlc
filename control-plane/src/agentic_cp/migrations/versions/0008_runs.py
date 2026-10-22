"""Run event ingest (M2-04): `runs` and `run_events` (insert-only evidence), tenant-owned
under forced RLS.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from agentic_cp.tenancy.rls import enable_tenant_rls

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _tenant_id() -> sa.Column:
    return sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True)


def upgrade() -> None:
    op.create_table(
        "runs",
        _tenant_id(),
        sa.Column("run_id", sa.Text, primary_key=True),
        sa.Column("team", sa.Text, nullable=False),
        sa.Column("started_by", UUID(as_uuid=True), nullable=False),
        sa.Column("policy_version", sa.Text, nullable=False),
        sa.Column("policy_known", sa.Boolean, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("head_seq", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("head_hash", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "started_by"],
            ["users.tenant_id", "users.id"],
            name="runs_tenant_id_started_by_fkey",
        ),
    )
    op.create_table(
        "run_events",
        _tenant_id(),
        sa.Column("run_id", sa.Text, primary_key=True),
        sa.Column("seq", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("hash", sa.Text, nullable=False),
        sa.Column("prev", sa.Text, nullable=False),
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("step", sa.Text, nullable=False),
        sa.Column("at", sa.Text, nullable=False),
        sa.Column("entry", JSONB, nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.run_id"],
            name="run_events_tenant_id_run_id_fkey",
        ),
        sa.CheckConstraint("seq > 0", name="run_events_seq_positive"),
    )
    for table in ("runs", "run_events"):
        enable_tenant_rls(op, table)


def downgrade() -> None:
    op.drop_table("run_events")
    op.drop_table("runs")
