"""The control plane's tables (SQLAlchemy Core), mirrored exactly by the Alembic migrations
(`test_migrations_match_metadata`).

Names are unqualified: every engine pins `search_path` to the configured schema. `tenants`
is the only table without `tenant_id`; every other table is tenant-owned, listed in
`TENANT_TABLES`, keyed by `tenant_id` first and protected by forced row-level security."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB, UUID

#: Constraint names follow Postgres's own defaults, so migrations and metadata agree.
metadata = MetaData(
    naming_convention={
        "pk": "%(table_name)s_pkey",
        "uq": "%(table_name)s_%(column_0_N_name)s_key",
        "fk": "%(table_name)s_%(column_0_N_name)s_fkey",
    }
)

#: Every tenant-owned table, in creation order. A table in the schema that is neither
#: `tenants` nor listed here fails the catalogue test and `enforcement_status`.
TENANT_TABLES: tuple[str, ...] = ("audit_log",)

#: A tenant's slug: lower-case, starts with a letter, 2-63 characters.
SLUG_PATTERN = "^[a-z][a-z0-9-]{1,62}$"

tenants = Table(
    "tenants",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("slug", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="active"),
    Column("policy_repo_url", Text, nullable=True),
    Column("policy_branch", Text, nullable=False, server_default="main"),
    Column("webhook_token_sha256", BYTEA, nullable=True, unique=True),
    Column("gateway_tenant", Text, nullable=True),
    Column("rbac_revision", BigInteger, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="tenants_slug_format"),
    CheckConstraint("status in ('active', 'suspended')", name="tenants_status_known"),
)


def _tenant_id() -> Column:
    return Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True)


#: The per-tenant audit chain (M2-05): insert-only for the runtime role. `at` is the exact
#: text that was hashed; `before`/`after` hold JSON without floats.
audit_log = Table(
    "audit_log",
    metadata,
    _tenant_id(),
    Column("seq", BigInteger, primary_key=True, autoincrement=False),
    Column("at", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Column("actor_kind", Text, nullable=False),
    Column("event", Text, nullable=False),
    Column("team", Text, nullable=True),
    Column("target", Text, nullable=False),
    Column("before", JSONB, nullable=True),
    Column("after", JSONB, nullable=True),
    Column("reason", Text, nullable=False),
    Column("run_id", Text, nullable=True),
    Column("trace_id", Text, nullable=True),
    Column("policy_version", Text, nullable=True),
    Column("prev", Text, nullable=False),
    Column("hash", Text, nullable=False),
    CheckConstraint("actor_kind in ('human', 'service', 'system')", name="audit_actor_kind"),
    CheckConstraint("seq > 0", name="audit_seq_positive"),
)
