"""Repos and their CODEOWNERS-derived maintainers (M2-03), tenant-owned under forced RLS, and
the active policy's repo patterns kept with each version (repo registration checks them).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from agentic_cp.tenancy.rls import enable_tenant_rls

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _tenant_id() -> sa.Column:
    return sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True)


def upgrade() -> None:
    op.add_column(
        "policy_versions",
        sa.Column("repo_patterns", ARRAY(sa.Text), nullable=False, server_default="{}"),
    )
    op.create_table(
        "repos",
        _tenant_id(),
        sa.Column("repo_key", sa.Text, primary_key=True),
        sa.Column("remote", sa.Text, nullable=False),
        sa.Column("team", sa.Text, nullable=False),
        sa.Column("owners", ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("unresolved", ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("codeowners_path", sa.Text, nullable=True),
        sa.Column("codeowners_commit", sa.Text, nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registered_by", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "repo_maintainers",
        _tenant_id(),
        sa.Column("repo_key", sa.Text, primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source_entry", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "repo_key"],
            ["repos.tenant_id", "repos.repo_key"],
            name="repo_maintainers_tenant_id_repo_key_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="repo_maintainers_tenant_id_user_id_fkey",
        ),
    )
    for table in ("repos", "repo_maintainers"):
        enable_tenant_rls(op, table)


def downgrade() -> None:
    op.drop_table("repo_maintainers")
    op.drop_table("repos")
    op.drop_column("policy_versions", "repo_patterns")
