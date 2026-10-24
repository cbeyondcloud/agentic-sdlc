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
    Index,
    Integer,
    MetaData,
    Numeric,
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
    "repos",
    "repo_maintainers",
    "runs",
    "run_events",
    "kill_switches",
    "notifications",
    "invites",
    "proxy_keys",
    "auto_trips",
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
    # the tenant's own sender for mails (M2-07, ruling P1); None: the deployment's
    Column("mail_from", Text, nullable=True),
    Column("mail_from_name", Text, nullable=True),
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
    Column("seed_permissions", ARRAY(Text), nullable=True),
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
    Column("repo_patterns", ARRAY(Text), nullable=False, server_default="{}"),
    # what run registration reads from this version (M2-08, M2-21): min_cli_version,
    # concurrency, allow_repo_pipelines and pipelines (policy-repo path -> sha256)
    Column("settings", JSONB, nullable=False, server_default="{}"),
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

#: Repos registered in a tenant (M2-03): `owners` are the CODEOWNERS owners of the agentic
#: paths (host-qualified handles and emails), `unresolved` the `@org/team` entries (ND-6).
repos = Table(
    "repos",
    metadata,
    _tenant_id(),
    Column("repo_key", Text, primary_key=True),
    Column("remote", Text, nullable=False),
    Column("team", Text, nullable=False),
    Column("owners", ARRAY(Text), nullable=False, server_default="{}"),
    Column("unresolved", ARRAY(Text), nullable=False, server_default="{}"),
    Column("codeowners_path", Text, nullable=True),
    Column("codeowners_commit", Text, nullable=True),
    Column("refreshed_at", DateTime(timezone=True), nullable=True),
    Column("registered_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

#: Derived maintainers: users of the tenant whose handle or email CODEOWNERS names.
repo_maintainers = Table(
    "repo_maintainers",
    metadata,
    _tenant_id(),
    Column("repo_key", Text, primary_key=True),
    Column("user_id", UUID(as_uuid=True), primary_key=True),
    Column("source_entry", Text, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "repo_key"], ["repos.tenant_id", "repos.repo_key"], ondelete="CASCADE"
    ),
    ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
)

#: Runs reported by CLIs (M2-04): one row per run, holding its identity and chain head.
#: The key includes the tenant, so one run id in two tenants is two runs.
runs = Table(
    "runs",
    metadata,
    _tenant_id(),
    Column("run_id", Text, primary_key=True),
    Column("team", Text, nullable=False),
    Column("started_by", UUID(as_uuid=True), nullable=False),
    Column("policy_version", Text, nullable=False),
    Column("policy_known", Boolean, nullable=False),
    Column("status", Text, nullable=False, server_default="open"),
    Column("head_seq", BigInteger, nullable=False, server_default="0"),
    Column("head_hash", Text, nullable=False, server_default=""),
    Column("opened_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_event_at", DateTime(timezone=True), nullable=True),
    # the run budget sent at registration and the spend ingested so far (M2-16)
    Column("budget_usd", Numeric, nullable=True),
    Column("spent_usd", Numeric, nullable=False, server_default="0"),
    ForeignKeyConstraint(["tenant_id", "started_by"], ["users.tenant_id", "users.id"]),
)

#: Each run's chained events exactly as received (insert-only evidence; `entry` is the
#: whole entry, so the stored chain re-verifies with `sdlc_commons.ledger.verify_entries`).
run_events = Table(
    "run_events",
    metadata,
    _tenant_id(),
    Column("run_id", Text, primary_key=True),
    Column("seq", BigInteger, primary_key=True, autoincrement=False),
    Column("hash", Text, nullable=False),
    Column("prev", Text, nullable=False),
    Column("event", Text, nullable=False),
    Column("step", Text, nullable=False),
    Column("at", Text, nullable=False),
    Column("entry", JSONB, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(["tenant_id", "run_id"], ["runs.tenant_id", "runs.run_id"]),
    CheckConstraint("seq > 0", name="run_events_seq_positive"),
    # auto-trip reads the spend and errors received in its window (M2-11)
    Index("run_events_tenant_id_received_at_idx", "tenant_id", "received_at"),
)

#: Kill and pause switches (M2-06, M2-10). Clearing one sets `cleared_by`/`cleared_at`; the
#: audit chain (`killswitch.set` / `.cleared`) is the evidence. `team` limits a switch of any
#: kind to that team's runs (a team lead's switches always carry it).
kill_switches = Table(
    "kill_switches",
    metadata,
    _tenant_id(),
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("scope_kind", Text, nullable=False),
    Column("scope_value", Text, nullable=False, server_default=""),
    Column("team", Text, nullable=True),
    Column("mode", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("cleared_by", Text, nullable=True),
    Column("cleared_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "scope_kind in ('tenant', 'team', 'user', 'repo', 'pipeline', 'agent', 'model', 'backend')",
        name="kill_switches_scope_known",
    ),
    CheckConstraint("mode in ('kill', 'pause')", name="kill_switches_mode_known"),
    CheckConstraint(
        "(cleared_by is null) = (cleared_at is null)", name="kill_switches_cleared_pair"
    ),
)

#: The mail outbox (M2-07): one row per mail, created in the transaction of the event that
#: causes it; `dedupe_key` makes a second enqueue a no-op (ruling P2). Bodies are cleared
#: once a mail is sent or failed (P3: an invite body holds its one-time link).
notifications = Table(
    "notifications",
    metadata,
    _tenant_id(),
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("kind", Text, nullable=False),
    Column("recipient", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("body_text", Text, nullable=False),
    Column("body_html", Text, nullable=False),
    Column("dedupe_key", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_error", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("sent_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("tenant_id", "dedupe_key"),
    CheckConstraint("status in ('pending', 'sent', 'failed')", name="notifications_status_known"),
)

#: One-time invites (M2-18): only sha256(token) is stored, unique across tenants because a
#: redemption is resolved before the tenant is known (`resolve_invite`, a definer function).
invites = Table(
    "invites",
    metadata,
    _tenant_id(),
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("token_sha256", BYTEA, nullable=False, unique=True),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("used_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
)

#: Which gateway (TokenLean) proxy key belongs to which user (M2-17): the key itself lives
#: only at the gateway. `revoke_requested_at` without `revoked_at` is a revocation the
#: gateway has not confirmed yet; the worker retries it.
proxy_keys = Table(
    "proxy_keys",
    metadata,
    _tenant_id(),
    Column("key_id", Text, primary_key=True),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("team", Text, nullable=True),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("revoke_requested_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
    CheckConstraint("key_id ~ '^[0-9a-f]{16,64}$'", name="proxy_keys_key_id_format"),
)

#: Auto-trip state per team and kind (M2-11): `armed` until the team's spend or error rate
#: passes its threshold, `tripped` while the pause switch it set (`switch_id`) holds, and
#: `disarmed` after a person cleared that switch, until the rate falls below the reset level.
#: `value`/`threshold` are decimal text (None: unmeasured).
auto_trips = Table(
    "auto_trips",
    metadata,
    _tenant_id(),
    Column("team", Text, primary_key=True),
    Column("kind", Text, primary_key=True),
    Column("state", Text, nullable=False, server_default="armed"),
    Column("switch_id", UUID(as_uuid=True), nullable=True),
    Column("value", Text, nullable=True),
    Column("threshold", Text, nullable=True),
    Column("changed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["tenant_id", "switch_id"], ["kill_switches.tenant_id", "kill_switches.id"]
    ),
    CheckConstraint("kind in ('spend', 'errors')", name="auto_trips_kind_known"),
    CheckConstraint("state in ('armed', 'tripped', 'disarmed')", name="auto_trips_state_known"),
)

#: Unique constraints that deliberately span tenants: credential hashes, looked up before
#: the tenant is known (exact match only, through a registered definer function).
GLOBAL_UNIQUE: frozenset[tuple[str, str]] = frozenset(
    {("tokens", "token_sha256"), ("invites", "token_sha256")}
)
