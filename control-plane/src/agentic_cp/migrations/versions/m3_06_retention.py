"""Retention (M3-06, DF-568): a tenant may be `deleted` - its data erased by `admin tenant
delete`, its audit chain kept until the retention period ends. Expand only (decision 110):
the status check gains one value; a release before this one never sees a deleted tenant
as active (`active_tenant_ids` and every credential lookup want `active`).

Revision ID: m3_06_retention
Revises: m3_01_roi
Create Date: 2026-10-30
"""

from alembic import op

revision = "m3_06_retention"
down_revision = "m3_01_roi"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("tenants_status_known", "tenants", type_="check")
    op.create_check_constraint(
        "tenants_status_known", "tenants", "status in ('active', 'suspended', 'deleted')"
    )


def downgrade() -> None:
    op.drop_constraint("tenants_status_known", "tenants", type_="check")
    op.create_check_constraint(
        "tenants_status_known", "tenants", "status in ('active', 'suspended')"
    )
