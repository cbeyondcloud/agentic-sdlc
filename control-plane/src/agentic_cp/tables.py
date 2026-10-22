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
    MetaData,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import BYTEA, UUID

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
TENANT_TABLES: tuple[str, ...] = ()

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
