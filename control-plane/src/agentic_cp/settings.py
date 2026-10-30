"""Deployment-wide settings, read from `AGENTIC_CP_*` environment variables.

Only what is the same for every tenant lives here; per-tenant values (policy repo, branch,
gateway tenant) live in the `tenants` table. An unknown `AGENTIC_CP_*` name is refused with
its name, so a typo never silently falls back to a default. Secrets are `SecretStr`: they
never appear in a repr, a log line or an error message."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from packaging.version import InvalidVersion, Version
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
)
from sqlalchemy.engine import make_url

PREFIX = "AGENTIC_CP_"
#: Names that share the prefix but are not settings: the test harness reads them.
NOT_SETTINGS = frozenset({"AGENTIC_CP_TEST_DSN", "AGENTIC_CP_REQUIRE_PG"})
#: Retired names, each with what replaced it.
RETIRED = {
    "AGENTIC_CP_TENANT": (
        "one deployment serves many tenants; create each with "
        "`python -m agentic_cp admin tenant create`"
    ),
    "AGENTIC_CP_REQUIRE_AUDIT_ENFORCEMENT": (
        "replaced by AGENTIC_CP_REQUIRE_DB_ENFORCEMENT, which covers insert-only audit and "
        "row-level security"
    ),
}
#: A Postgres identifier we are willing to interpolate into DDL.
SCHEMA_PATTERN = r"^[a-z_][a-z0-9_]{0,62}$"


class SettingsError(Exception):
    """The environment does not describe a valid deployment."""


def _pep440(value: str) -> str:
    try:
        Version(value)
    except InvalidVersion:
        raise ValueError("not a PEP 440 version (for example 1.4.0)") from None
    return value


def _http_url(value: str) -> str:
    stripped = value.strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", stripped):
        raise ValueError("not an http(s) URL (for example https://cp.example.com)")
    return stripped


#: The shortest bearer secret a setting accepts (a blank one is no secret at all).
MIN_TOKEN_CHARS = 16


def _blank_is_none(value: object) -> object:
    """An empty or all-space secret (a blank Helm or Kubernetes value) is unset."""
    text = value.get_secret_value() if isinstance(value, SecretStr) else value
    if isinstance(text, str) and not text.strip():
        return None
    return value


def _long_secret(value: SecretStr | None) -> SecretStr | None:
    if value is not None and len(value.get_secret_value().strip()) < MIN_TOKEN_CHARS:
        raise ValueError(f"shorter than {MIN_TOKEN_CHARS} characters")
    return value


def checked_address(value: str) -> str:
    """A mail address as a sender may use it, or ValueError."""
    stripped = value.strip()
    if not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", stripped):
        raise ValueError("not an email address")
    return stripped


class Settings(BaseModel):
    """The control plane's deployment settings (frozen)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    database_url: SecretStr
    """The runtime role's DSN (no table ownership, no BYPASSRLS)."""
    owner_database_url: SecretStr | None = None
    """The owner role's DSN, for `migrate`, `db grant-runtime` and `admin` commands only."""
    db_schema: Annotated[str, Field(pattern=SCHEMA_PATTERN)] = "agentic"
    policy_poll_seconds: Annotated[int, Field(ge=10)] = 60
    policy_workdir: Path = Path(tempfile.gettempdir()) / "agentic-cp" / "policy"
    git_config_dir: Path | None = None
    """`<dir>/<tenant-slug>.gitconfig`, when present, is that tenant's GIT_CONFIG_GLOBAL
    (a mounted secret naming a credential helper; ND-15)."""
    git_timeout_seconds: Annotated[int, Field(ge=5, le=3600)] = 120
    allow_local_repos: bool = False
    """Accept `file://` and path repository URLs (R12). Development and tests only: in a
    shared deployment a tenant admin could otherwise read the server's disk."""
    codeowners_refresh_minutes: Annotated[int, Field(ge=1)] = 60
    require_db_enforcement: bool = True
    """Fail closed (readyz 503, serve refuses) unless the database enforces insert-only
    evidence and tenant isolation (DR-4). False only for local development."""
    max_ingest_entries: Annotated[int, Field(ge=1, le=5000)] = 500
    max_body_bytes: Annotated[int, Field(ge=1024)] = 1_048_576
    user_token_days: Annotated[int, Field(ge=1, le=90)] = 90
    service_token_max_days: Annotated[int, Field(ge=1, le=365)] = 365
    enforcer_cache_tenants: Annotated[int, Field(ge=1)] = 256
    kill_ttl_seconds: Annotated[int, Field(ge=1, le=30)] = 10
    """The longest a CLI may cache the kill-switch state (`KillState.ttl_seconds`, ruling R1).
    The CLI caches for the smaller of this and its own 10 s, so a value above 10 never
    lengthens propagation; it can only shorten it (DF-707). The ceiling stays 30 (ruling R5)."""
    min_cli_version: Annotated[str, AfterValidator(_pep440)] | None = None
    """The deployment's minimum CLI version (M2-08); a tenant's `org.yaml: min_cli_version`
    can only raise it (ruling R7). A value that is not a PEP 440 version refuses startup
    rather than enforcing no minimum (review F4)."""
    run_slot_hours: Annotated[int, Field(ge=1, le=720)] = 24
    """A registered run holds a concurrency slot while open and active this recently (R8)."""
    public_url: Annotated[str, AfterValidator(_http_url)] | None = None
    """Where people reach this control plane (invite links, M2-18); no invite mail without."""
    invite_hours: Annotated[int, Field(ge=1, le=336)] = 72
    """How long an invite link works (ruling P8)."""
    smtp_host: str | None = None
    """The org's SMTP relay (M2-07); None: mails stay queued, nothing is sent."""
    smtp_port: Annotated[int, Field(ge=1, le=65535)] = 587
    smtp_security: Literal["starttls", "tls", "none"] = "starttls"
    """`starttls` (default: never sent in clear), `tls` (implicit, port 465) or `none` (a
    relay on the same host or cluster only)."""
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: Annotated[str, AfterValidator(checked_address)] | None = None
    """The deployment's sender; a tenant's `mail_from` replaces it for that tenant."""
    smtp_from_name: str = "agentic"
    smtp_timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 30
    notify_max_attempts: Annotated[int, Field(ge=1, le=50)] = 8
    """Sends of one mail before it is marked failed (ruling P2); a relay that cannot be used
    at all does not count (review F3)."""
    gateway_admin_url: Annotated[str, AfterValidator(_http_url)] | None = None
    """The gateway's admin API (TokenLean `/admin/keys`, M2-17); None: no proxy keys."""
    gateway_admin_key: SecretStr | None = None
    gateway_url: Annotated[str, AfterValidator(_http_url)] | None = None
    """The gateway URL people's CLIs use; handed out with a redeemed invite."""
    gateway_team_rate_limits: bool = False
    """Write teams' `rate_limit` into the gateway's `rate_limit.per_team` (M2-09). TokenLean
    keys that section by team name across every tenant, so turn it on only when team names
    are unique across this deployment's tenants; an entry another writer set is never
    overwritten either way."""
    routing_max_attempts: Annotated[int, Field(ge=1, le=50)] = 8
    """Tries of one routing apply (M2-09) before it is marked failed; an admin queues it
    again with `POST /v1/routing/apply`, and the next accepted policy version does too."""
    personal_max_devices: Annotated[int, Field(ge=1, le=1000)] = 20
    """Laptops (device chains) one user backs up personal skills and notes from (M2-20,
    review F4); a new one past this is refused with 429 `personal.quota`."""
    personal_max_bytes: Annotated[int, Field(ge=1)] = 64 * 1024 * 1024
    """What one user's personal backups may hold over every version and device: content
    bytes plus 1 KiB per event. Backups are insert-only, so this never shrinks until
    retention (OR-01) prunes them."""
    personal_daily_bytes: Annotated[int, Field(ge=1)] = 16 * 1024 * 1024
    """What one user may add to their personal backups in any 24 hours, counted the same
    way (a client stuck in a loop, or a stolen token, cannot fill the database)."""
    rate_limit_per_minute: Annotated[int, Field(ge=0, le=1_000_000)] = 1200
    """Requests per minute per token holder, in each process (DF-309); 0: no limit."""
    tenant_rate_limit_per_minute: Annotated[int, Field(ge=0, le=10_000_000)] = 0
    """Requests per minute for all of one tenant's callers together, per process; 0: none."""
    metrics_token: Annotated[
        SecretStr | None, BeforeValidator(_blank_is_none), AfterValidator(_long_secret)
    ] = None
    """The bearer token Prometheus scrapes `/metrics` with (M2-12); None: `/metrics` is 404.
    A blank value is no token (review F1); a shorter one than `MIN_TOKEN_CHARS` refuses
    startup."""
    metrics_max_tenants: Annotated[int, Field(ge=1, le=1000)] = 100
    """Tenants labelled by slug in the per-tenant gauges; the rest are summed as `_other`."""
    metrics_refresh_seconds: Annotated[int, Field(ge=1, le=3600)] = 30
    """The per-tenant gauges are read from the database at most this often."""
    report_timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 30
    """The longest a cost report's queries may run (M3-01 review F1): they read the gateway's
    usage through the shared connection pool, so a slow read stops with a 503 instead of
    holding a connection that ingest and audit need."""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        """Settings from `AGENTIC_CP_<FIELD>` variables; anything else in `env` is ignored."""
        values: dict[str, Any] = {}
        for name, value in env.items():
            if not name.startswith(PREFIX) or name in NOT_SETTINGS:
                continue
            if name in RETIRED:
                raise SettingsError(f"{name} is no longer read: {RETIRED[name]}")
            field = name[len(PREFIX) :].lower()
            if field not in cls.model_fields:
                raise SettingsError(
                    f"unknown setting {name} (known: "
                    f"{', '.join(PREFIX + f.upper() for f in sorted(cls.model_fields))})"
                )
            values[field] = value
        if "database_url" not in values:
            raise SettingsError(f"{PREFIX}DATABASE_URL is required (the runtime role's DSN)")
        try:
            return cls(**values)
        except ValidationError as error:
            # Name each field and the rule it broke, never the submitted value.
            problems = "; ".join(
                f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}"
                for item in error.errors(include_input=False, include_url=False)
            )
            raise SettingsError(f"invalid settings - {problems}") from None

    @property
    def runtime_role(self) -> str:
        """The runtime role's name: the user in `database_url`."""
        return make_url(self.database_url.get_secret_value()).username or ""
