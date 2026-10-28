"""The schema and the tenants table.

Revision ID: 0001
Revises:
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import BYTEA, UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()
        ),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("policy_repo_url", sa.Text, nullable=True),
        sa.Column("policy_branch", sa.Text, nullable=False, server_default="main"),
        sa.Column("webhook_token_sha256", BYTEA, nullable=True, unique=True),
        sa.Column("gateway_tenant", sa.Text, nullable=True),
        sa.Column("rbac_revision", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("slug ~ '^[a-z][a-z0-9-]{1,62}$'", name="tenants_slug_format"),
        sa.CheckConstraint("status in ('active', 'suspended')", name="tenants_status_known"),
    )


def downgrade() -> None:
    op.drop_table("tenants")
