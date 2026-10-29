"""Run health (M3-07): each run's last counted heartbeat (`last_heartbeat_at`, by the
server's clock), whether it waits for a person or a pause switch (`waiting`), and when the
worker marked it stale (`stale_at`). The columns are on `runs`, which is already
tenant-owned under forced RLS.

Revision ID: m3_07_run_health
Revises: m3_11_learning
Create Date: 2026-10-29
"""

import sqlalchemy as sa
from alembic import op

revision = "m3_07_run_health"
down_revision = "m3_11_learning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("waiting", sa.Boolean, nullable=False, server_default="false"))
    op.add_column("runs", sa.Column("stale_at", sa.DateTime(timezone=True), nullable=True))
    # the worker's zombie pass reads a tenant's open runs
    op.create_index("runs_tenant_id_status_idx", "runs", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_index("runs_tenant_id_status_idx", table_name="runs")
    for column in ("stale_at", "waiting", "last_heartbeat_at"):
        op.drop_column("runs", column)
