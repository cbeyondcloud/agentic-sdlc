"""What the seed last gave each builtin role (DF-419, ruling R22), so a release's new
permissions reach existing tenants without undoing a tenant's removals. NULL for a role
seeded before this column existed (taken as seeded with the running release's defaults).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("roles", sa.Column("seed_permissions", ARRAY(sa.Text), nullable=True))


def downgrade() -> None:
    op.drop_column("roles", "seed_permissions")
