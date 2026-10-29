"""Executive cost (M3-01): `runs.pipeline` (the pipeline named at registration) and the
executive cost views (`ROI_VIEWS`).

Only the new views are created: the M2-37 views, the contract views a gateway link points
at, and every reader's grants stay as they are (ruling R5). A reader bound before this
revision is granted the new views here; one bound later gets them from `bind_reader`.

Revision ID: m3_01_roi
Revises: m3_07_run_health
Create Date: 2026-10-29
"""

import sqlalchemy as sa
from alembic import op

from agentic_cp.dashboards.readers import checked_role
from agentic_cp.dashboards.views import ROI_VIEWS, drop_statements, view_statements
from agentic_cp.tenancy.rls import schema_of

revision = "m3_01_roi"
down_revision = "m3_07_run_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("pipeline", sa.Text, nullable=True))
    for statement in view_statements(schema_of(op), ROI_VIEWS):
        op.execute(statement)
    bound = op.get_bind().execute(
        sa.text("select dashboard_role from tenants where dashboard_role is not null")
    )
    for (role,) in bound.all():
        for view in ROI_VIEWS:
            op.execute(f"GRANT SELECT ON {view} TO {checked_role(role)}")


def downgrade() -> None:
    for statement in drop_statements(ROI_VIEWS):
        op.execute(statement)
    op.drop_column("runs", "pipeline")
