"""The control plane's tables (SQLAlchemy Core), mirrored exactly by the Alembic migrations
(`test_migrations_match_metadata`).

Names are unqualified: every engine pins `search_path` to the configured schema. `tenants`
is the only table without `tenant_id`; every other table is tenant-owned, listed in
`TENANT_TABLES`, keyed by `tenant_id` first and protected by forced row-level security."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, UUID

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
TENANT_TABLES: tuple[str, ...] = (
    "audit_log",
    "users",
    "tokens",
    "roles",
    "assignments",
    "policy_versions",
    "policy_active",
)

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

#: People and service accounts of a tenant. The same email in two tenants is two records
#: with separate tokens (ND-17).
users = Table(
    "users",
    metadata,
    _tenant_id(),
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("email", Text, nullable=False),
    Column("display_name", Text, nullable=False, server_default=""),
    Column("kind", Text, nullable=False),
    Column("handles", ARRAY(Text), nullable=False, server_default="{}"),
    Column("status", Text, nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "email"),
    CheckConstraint("email = lower(email)", name="users_email_lower"),
    CheckConstraint("kind in ('human', 'service')", name="users_kind_known"),
    CheckConstraint("status in ('active', 'disabled')", name="users_status_known"),
)

#: Control-plane bearer tokens: only sha256(value) is stored. `token_sha256` is unique
#: across tenants because resolution happens before the tenant is known.
tokens = Table(
    "tokens",
    metadata,
    _tenant_id(),
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("name", Text, nullable=False),
    Column("token_sha256", BYTEA, nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("created_by", Text, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("last_used_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
)

#: Named permission sets per tenant (spec §12.3); builtin roles come from `DEFAULT_ROLES`.
roles = Table(
    "roles",
    metadata,
    _tenant_id(),
    Column("name", Text, primary_key=True),
    Column("permissions", ARRAY(Text), nullable=False),
    Column("builtin", Boolean, nullable=False, server_default="false"),
    Column("team_assignable", Boolean, nullable=False, server_default="false"),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("name ~ '^[a-z][a-z0-9-]{1,62}$'", name="roles_name_format"),
)

#: user -> role at org scope (`scope_value` '') or within one team.
assignments = Table(
    "assignments",
    metadata,
    _tenant_id(),
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("role", Text, nullable=False),
    Column("scope_kind", Text, nullable=False),
    Column("scope_value", Text, nullable=False, server_default=""),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("tenant_id", "user_id", "role", "scope_kind", "scope_value"),
    ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
    ForeignKeyConstraint(["tenant_id", "role"], ["roles.tenant_id", "roles.name"]),
    CheckConstraint("scope_kind in ('org', 'team')", name="assignments_scope_known"),
    CheckConstraint(
        "(scope_kind = 'org' and scope_value = '') or (scope_kind = 'team' and scope_value <> '')",
        name="assignments_scope_value",
    ),
)

#: Every policy commit a tenant's sync has seen (M2-02): insert-only for the runtime role.
#: An accepted version carries its bundle (`git archive`, gzipped, `mtime=0`) and digests;
#: a rejected one its problems. `teams` are the policy's teams (the assignment guard, R14).
policy_versions = Table(
    "policy_versions",
    metadata,
    _tenant_id(),
    Column("version", Text, primary_key=True),
    Column("commit", Text, nullable=False),
    Column("branch", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("problems", ARRAY(Text), nullable=False, server_default="{}"),
    Column("teams", ARRAY(Text), nullable=False, server_default="{}"),
    Column("bundle", BYTEA, nullable=True),
    Column("digest", Text, nullable=True),
    Column("tree_digest", Text, nullable=True),
    Column("commit_at", DateTime(timezone=True), nullable=True),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("status in ('accepted', 'rejected')", name="policy_versions_status_known"),
    CheckConstraint(
        "(status = 'accepted') = (bundle is not null and digest is not null)",
        name="policy_versions_bundle_iff_accepted",
    ),
)

#: The tenant's active policy version: the one updatable pointer into `policy_versions`.
policy_active = Table(
    "policy_active",
    metadata,
    _tenant_id(),
    Column("version", Text, nullable=False),
    Column("branch", Text, nullable=False),
    Column("activated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["tenant_id", "version"], ["policy_versions.tenant_id", "policy_versions.version"]
    ),
)

#: Unique constraints that deliberately span tenants: credential hashes, looked up before
#: the tenant is known (exact match only, through a registered definer function).
GLOBAL_UNIQUE: frozenset[tuple[str, str]] = frozenset({("tokens", "token_sha256")})
