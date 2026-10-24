"""The learning loop L1-L3 (M3-11..M3-13): each run's feedback journal as received
(`feedback_events`, insert-only evidence), one row per feedback record with its current
attribution and lesson (`feedback`), and the proposals made from repeated corrections
(`learning_proposals`). All three are tenant-owned under forced RLS.

Revision ID: m3_11_learning
Revises: m2_09_routing
Create Date: 2026-10-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from agentic_cp.tenancy.rls import enable_tenant_rls

revision = "m3_11_learning"
down_revision = "m2_09_routing"
branch_labels = None
depends_on = None

SIGNALS = "('gate-rejection', 'gate-edit', 'pr-review', 'review-miss')"
EVENTS = (
    "('learning.feedback_captured', 'learning.feedback_analysed', 'learning.attribution_corrected')"
)


def _tenant_id() -> sa.Column:
    return sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True)


def _at(name: str, *, nullable: bool = False) -> sa.Column:
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _texts(name: str) -> sa.Column:
    return sa.Column(name, ARRAY(sa.Text), nullable=False, server_default="{}")


def _run_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "run_id"],
        ["runs.tenant_id", "runs.run_id"],
        name=f"{table}_tenant_id_run_id_fkey",
    )


def upgrade() -> None:
    op.create_table(
        "feedback_events",
        _tenant_id(),
        sa.Column("run_id", sa.Text, primary_key=True),
        sa.Column("seq", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("hash", sa.Text, nullable=False),
        sa.Column("prev", sa.Text, nullable=False),
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("entry", JSONB, nullable=False),
        _at("received_at"),
        _run_fk("feedback_events"),
        sa.CheckConstraint("seq > 0", name="feedback_events_seq_positive"),
        sa.CheckConstraint(f"event in {EVENTS}", name="feedback_events_event_known"),
    )
    op.create_table(
        "feedback",
        _tenant_id(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("team", sa.Text, nullable=False),
        sa.Column("repo", sa.Text, nullable=True),
        sa.Column("signal", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("given_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("author_id", UUID(as_uuid=True), nullable=False),
        sa.Column("record", JSONB, nullable=False),
        sa.Column("attribution", JSONB, nullable=True),
        _texts("agents"),
        sa.Column("lesson", sa.Text, nullable=False, server_default=""),
        sa.Column("corrected_by", sa.Text, nullable=True),
        _at("corrected_at", nullable=True),
        _at("captured_at"),
        _run_fk("feedback"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "author_id"],
            ["users.tenant_id", "users.id"],
            name="feedback_tenant_id_author_id_fkey",
        ),
        sa.CheckConstraint(f"signal in {SIGNALS}", name="feedback_signal_known"),
        sa.Index("feedback_tenant_id_team_given_at_idx", "tenant_id", "team", "given_at"),
    )
    op.create_table(
        "learning_proposals",
        _tenant_id(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("team", sa.Text, nullable=False),
        sa.Column("agent", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("lesson", sa.Text, nullable=False),
        _texts("evidence"),
        _texts("sources"),
        _texts("actors"),
        sa.Column("authors", ARRAY(UUID(as_uuid=True)), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        _at("created_at"),
        _at("decided_at", nullable=True),
        sa.Column("decided_by", sa.Text, nullable=True),
        sa.CheckConstraint(
            "status in ('pending', 'rejected')", name="learning_proposals_status_known"
        ),
    )
    for table in ("feedback_events", "feedback", "learning_proposals"):
        enable_tenant_rls(op, table)


def downgrade() -> None:
    op.drop_table("learning_proposals")
    op.drop_table("feedback")
    op.drop_table("feedback_events")
