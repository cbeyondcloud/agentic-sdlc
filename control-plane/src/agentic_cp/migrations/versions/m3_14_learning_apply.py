"""The learning loop L4-L6 (M3-14..M3-16): a proposal's owner, a person's edits, its
endorsements, the draft as approved, how it was applied and reverted, and the regression
case before and after; the statuses approved, deferred, applied and reverted; and the
dashboard view of each applied learning's correction rate before and after
(`dash_learning_effect`, created here because it reads the M3-11 tables).

Revision ID: m3_14_learning_apply
Revises: m3_07_run_health
Create Date: 2026-10-29
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from agentic_cp.dashboards.views import LEARNING_VIEWS, learning_view_statements
from agentic_cp.tenancy.rls import schema_of

revision = "m3_14_learning_apply"
down_revision = "m3_07_run_health"
branch_labels = None
depends_on = None

TABLE = "learning_proposals"
CHECK = "learning_proposals_status_known"
STATUSES = "('pending', 'approved', 'rejected', 'deferred', 'applied', 'reverted')"


def upgrade() -> None:
    for name in ("owner", "kind", "edited_lesson"):
        op.add_column(TABLE, sa.Column(name, sa.Text, nullable=True))
    op.add_column(
        TABLE, sa.Column("endorsements", ARRAY(sa.Text), nullable=False, server_default="{}")
    )
    op.add_column(TABLE, sa.Column("draft", JSONB, nullable=True))
    op.add_column(TABLE, sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("applied_ref", JSONB, nullable=True))
    op.add_column(TABLE, sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("reverted_ref", JSONB, nullable=True))
    op.add_column(TABLE, sa.Column("measurement", JSONB, nullable=True))
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.create_check_constraint(CHECK, TABLE, f"status in {STATUSES}")
    for statement in learning_view_statements(schema_of(op)):
        op.execute(statement)


def downgrade() -> None:
    for name in reversed(LEARNING_VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {name}")
    op.execute(f"UPDATE {TABLE} SET status = 'pending' WHERE status IN ('approved', 'deferred')")
    op.execute(f"UPDATE {TABLE} SET status = 'rejected' WHERE status IN ('applied', 'reverted')")
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.create_check_constraint(CHECK, TABLE, "status in ('pending', 'rejected')")
    for name in (
        "measurement",
        "reverted_ref",
        "reverted_at",
        "applied_ref",
        "applied_at",
        "draft",
        "endorsements",
        "edited_lesson",
        "kind",
        "owner",
    ):
        op.drop_column(TABLE, name)
