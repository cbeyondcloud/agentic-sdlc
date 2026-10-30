"""Dashboard views (M2-37): the usage contract views (empty until `db link-usage`), the
tenant-scoped `dash_*` views Grafana reads, and `tenants.dashboard_role`, the one Grafana
reader role bound to a tenant (`db dashboard-role`).

The view DDL lives in `agentic_cp.dashboards.views`; a later change to it ships as a new
migration that drops and re-creates the views (and `db link-usage` is run again).

Revision ID: m2_37_usage_views
Revises: 0011_audit_ops
Create Date: 2026-10-24
"""

import sqlalchemy as sa
from alembic import op

from agentic_cp.dashboards.views import M2_37_VIEWS, drop_statements, view_statements
from agentic_cp.tenancy.rls import schema_of

revision = "m2_37_usage_views"
down_revision = "0011_audit_ops"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("dashboard_role", sa.Text, nullable=True))
    op.create_unique_constraint("tenants_dashboard_role_key", "tenants", ["dashboard_role"])
    for statement in view_statements(schema_of(op), M2_37_VIEWS):
        op.execute(statement)


def downgrade() -> None:
    for statement in drop_statements(M2_37_VIEWS):
        op.execute(statement)
    op.drop_constraint("tenants_dashboard_role_key", "tenants", type_="unique")
    op.drop_column("tenants", "dashboard_role")
