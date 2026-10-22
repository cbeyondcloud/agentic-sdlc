"""The control plane's wire models (SP15): one definition shared by the server and the CLI.

Pure pydantic. No HTTP client, no server library and no rule engine is imported here, so
the CLI can depend on it without pulling the server in (import contract)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentic.orgapi.rbac import EffectivePermissions

#: Every route lives under this prefix.
API_PREFIX = "/v1"
#: Control-plane bearer tokens start with this (opaque; only their sha256 is stored).
TOKEN_PREFIX = "acp_"
#: Policy-repo webhook tokens start with this (slice 1b).
WEBHOOK_TOKEN_PREFIX = "acw_"
#: The CLI sends its version in this header on every call (M2-08 reads it).
CLI_VERSION_HEADER = "X-Agentic-CLI-Version"
#: Errors are RFC 9457 problem documents with this media type.
PROBLEM_MEDIA_TYPE = "application/problem+json"


class RequestModel(BaseModel):
    """Base of every request body: an unknown field is refused, never ignored (a body can
    never smuggle a tenant or a role in under a name the server does not read)."""

    model_config = ConfigDict(extra="forbid")


class TenantRef(BaseModel):
    """The caller's tenant, as the server derived it from the token."""

    slug: str
    name: str


class UserRef(BaseModel):
    id: UUID
    email: str
    display_name: str = ""


class AssignmentRef(BaseModel):
    """A role held at org scope or within one team."""

    role: str
    scope: Literal["org", "team"]
    team: str | None = None


class Me(BaseModel):
    """`GET /v1/me`: who the token belongs to, in which tenant, and what they hold."""

    user: UserRef
    kind: Literal["human", "service"]
    tenant: TenantRef
    token_expires_at: datetime
    assignments: list[AssignmentRef] = Field(default_factory=list)
    permissions: EffectivePermissions = Field(default_factory=EffectivePermissions)


class TokenRequest(RequestModel):
    """`POST /v1/tokens`: a new token for the caller (rotation). `days` defaults to the
    longest lifetime the caller's kind allows (90 for people, 365 for service accounts)."""

    name: str = Field(min_length=1, max_length=64)
    days: int | None = Field(default=None, ge=1)
    revoke_current: bool = False


class TokenIssued(BaseModel):
    """The one response that carries a token value; it is never shown again."""

    id: UUID
    name: str
    token: str = Field(repr=False)
    expires_at: datetime


class TokenInfo(BaseModel):
    """A token without its value."""

    id: UUID
    name: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    current: bool = False


#: A team slug, as policy files name teams.
TEAM_SLUG = r"^[a-z][a-z0-9-]{0,62}$"
ROLE_NAME = r"^[a-z][a-z0-9-]{1,62}$"


class RoleInfo(BaseModel):
    name: str
    permissions: list[str]
    builtin: bool
    team_assignable: bool


class RoleUpdate(RequestModel):
    """`PUT /v1/rbac/roles/{name}`: create or replace a role's permissions."""

    permissions: list[str]
    team_assignable: bool = False


class Assignment(BaseModel):
    id: UUID
    email: str
    role: str
    scope: Literal["org", "team"]
    team: str | None = None


class AssignmentRequest(RequestModel):
    """`POST /v1/rbac/assignments`: give `email` (created in the caller's tenant if new)
    `role` at org scope, or within `team`."""

    email: str = Field(min_length=3, max_length=254)
    role: str = Field(pattern=ROLE_NAME)
    team: str | None = Field(default=None, pattern=TEAM_SLUG)


class CheckRequest(RequestModel):
    """`POST /v1/rbac/check`: may the caller (or `user`, needing `rbac.assign`) do
    `permission` for the org, a team or a repo?"""

    permission: str
    team: str | None = Field(default=None, pattern=TEAM_SLUG)
    repo: str | None = Field(default=None, max_length=300)
    user: str | None = Field(default=None, max_length=254)


class CheckResult(BaseModel):
    allowed: bool
    reason: str
    role: str | None = None
    scope: str | None = None


class AuditEntry(BaseModel):
    """One audit record exactly as hashed (spec §14.5)."""

    seq: int
    tenant: str
    team: str | None
    actor: str
    actor_kind: Literal["human", "service", "system"]
    event: str
    target: str
    before: Any = None
    after: Any = None
    reason: str
    run_id: str | None
    trace_id: str | None
    policy_version: str | None
    at: str
    prev: str
    hash: str


class AuditVerification(BaseModel):
    problems: list[str]


class Problem(BaseModel):
    """An RFC 9457 problem document. `code` is a stable machine name
    (`auth.invalid_token`, ...); `hint` is a next step the CLI prints as is. A problem never
    echoes a submitted secret and never names another tenant."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    code: str
    hint: str | None = None
