"""Deployment-wide settings, read from `AGENTIC_CP_*` environment variables.

Only what is the same for every tenant lives here; per-tenant values (policy repo, branch,
gateway tenant) live in the `tenants` table. An unknown `AGENTIC_CP_*` name is refused with
its name, so a typo never silently falls back to a default. Secrets are `SecretStr`: they
never appear in a repr, a log line or an error message."""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
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
    """How long a CLI may cache the kill-switch state (`KillState.ttl_seconds`, ruling R1)."""
    min_cli_version: str | None = None
    """The deployment's minimum CLI version (M2-08); a tenant's `org.yaml: min_cli_version`
    can only raise it (ruling R7)."""
    run_slot_hours: Annotated[int, Field(ge=1, le=720)] = 24
    """A registered run holds a concurrency slot while open and active this recently (R8)."""

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
