"""The control plane's wire models (SP15): one definition shared by the server and the CLI.

Pure pydantic. No HTTP client, no server library and no rule engine is imported here, so
the CLI can depend on it without pulling the server in (import contract)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

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
    """A role held at org scope, within one team, or - derived from CODEOWNERS, never
    assigned by hand - for one repo (`derived` is then true)."""

    role: str
    scope: Literal["org", "team", "repo"]
    team: str | None = None
    repo: str | None = None
    derived: bool = False


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
    invited: bool = False
    """The person was new: an invite was created (and mailed when the relay is set up)."""


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


#: A git branch name a tenant may track (a subset of `git check-ref-format --branch`).
BRANCH_NAME = re.compile(
    r"(?!-)(?!.*\.\.)(?!.*//)(?!.*@\{)(?!.*\.lock$)(?!.*/\.)(?!\.)[A-Za-z0-9._/-]{1,200}(?<![./])"
)


def checked_branch(value: str) -> str:
    if not BRANCH_NAME.fullmatch(value):
        raise ValueError("not a branch name git accepts (letters, digits, '.', '_', '-', '/')")
    return value


class TenantInfo(BaseModel):
    """`GET /v1/tenant`: the caller's tenant (credentials never shown)."""

    slug: str
    name: str
    policy_repo: str | None = None
    policy_branch: str
    webhook_configured: bool = False


class PolicySource(RequestModel):
    """`PUT /v1/tenant/policy-source`: where the tenant's policy repo lives. A URL with
    credentials is refused (git credentials are mounted per tenant, ND-15)."""

    repo: str = Field(min_length=1, max_length=500)
    branch: Annotated[str, AfterValidator(checked_branch)] = "main"


class SyncOutcome(BaseModel):
    """What one policy sync did. `failed`: the repo could not be fetched (`problems` says
    why, credentials scrubbed)."""

    kind: Literal["unchanged", "activated", "rejected", "busy", "unconfigured", "failed"]
    version: str | None = None
    problems: list[str] = Field(default_factory=list)


class PolicySourceChanged(TenantInfo):
    """The tenant after a policy-source change, and the sync it triggered."""

    sync: SyncOutcome


class WebhookIssued(BaseModel):
    """The webhook path, carrying the new hook token: shown once, never again."""

    path: str = Field(repr=False)


class ActivePolicy(BaseModel):
    """`GET /v1/policy/active`: the tenant's validated policy and where its bundle is."""

    version: str
    branch: str
    digest: str
    tree_digest: str
    activated_at: datetime
    commit_at: datetime | None = None
    bundle_path: str


class PolicyVersionInfo(BaseModel):
    version: str
    branch: str
    status: Literal["accepted", "rejected"]
    problems: list[str] = Field(default_factory=list)
    digest: str | None = None
    received_at: datetime
    active: bool = False


#: A host-qualified handle as CODEOWNERS names people: `github.com:alice`.
HANDLE = r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}:[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"


class RepoRegistration(RequestModel):
    """`POST /v1/repos`: register a repo for a team (`agent.edit.repo` in that team)."""

    remote: str = Field(min_length=1, max_length=500)
    team: str = Field(pattern=TEAM_SLUG)


class RepoInfo(BaseModel):
    """A registered repo and the maintainers its CODEOWNERS names (ND-6)."""

    repo_key: str
    remote: str
    team: str
    maintainers: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    codeowners_path: str | None = None
    commit: str | None = None
    refreshed_at: datetime | None = None


class RepoMaintainers(BaseModel):
    users: list[str]
    unresolved: list[str] = Field(default_factory=list)
    commit: str | None = None


class HandlesUpdate(RequestModel):
    """`PUT /v1/users/{email}/handles`: the person's host-qualified handles (`rbac.assign`)."""

    handles: list[Annotated[str, Field(pattern=HANDLE)]] = Field(max_length=20)


class HandlesInfo(BaseModel):
    email: str
    handles: list[str]


class ChainHead(BaseModel):
    """The last event a chain holds: `seq` 0 and `hash` "" before the first."""

    seq: int = 0
    hash: str = ""


class IngestRequest(RequestModel):
    """`POST /v1/runs/{run_id}/events`: run ledger entries exactly as chained (Zyroflo's
    format), in order; the server caps the count (`max_ingest_entries`)."""

    entries: list[dict[str, Any]] = Field(min_length=1, max_length=5000)


class IngestResult(BaseModel):
    """What an ingest stored: new entries, replayed ones (no-ops), and the head after."""

    accepted: int
    duplicates: int
    head: ChainHead


class RunSummary(BaseModel):
    """`GET /v1/runs/{run_id}`: a run as the control plane holds it (ND-11: PR links point
    here until a web UI exists)."""

    run_id: str
    team: str
    started_by: str
    policy_version: str
    policy_known: bool
    status: str
    head: ChainHead
    opened_at: datetime
    last_event_at: datetime | None = None


ScopeKind = Literal["tenant", "team", "user", "repo", "pipeline", "agent", "model", "backend"]


class KillSwitchRequest(RequestModel):
    """`POST /v1/killswitch`: stop (`kill`) or hold (`pause`) the runs a scope names.
    `scope_kind` `tenant` is the spec's global switch (no `scope_value`); `team` limits a
    switch of any kind to that team's runs (a team lead must give their own team)."""

    scope_kind: ScopeKind
    scope_value: str = Field(default="", max_length=300)
    team: str | None = Field(default=None, pattern=TEAM_SLUG)
    mode: Literal["kill", "pause"] = "kill"
    reason: str = Field(min_length=3, max_length=500)


class KillSwitchInfo(BaseModel):
    id: UUID
    scope_kind: ScopeKind
    scope_value: str
    team: str | None = None
    mode: Literal["kill", "pause"]
    reason: str
    created_by: str
    created_at: datetime
    cleared_by: str | None = None
    cleared_at: datetime | None = None
    active: bool


class KillState(BaseModel):
    """`GET /v1/killswitch/state`: the active switches that may apply to a run of the
    caller's (the CLI decides per step), and how long the answer may be cached."""

    switches: list[KillSwitchInfo] = Field(default_factory=list)
    checked_at: datetime
    ttl_seconds: int


class RunRegistration(RequestModel):
    """`POST /v1/runs`: the org checks before a run starts (spec §8.2), and the run's
    concurrency slot when they pass. `pipeline_file` is the pipeline's path in the policy
    repo (None for a repo-layer pipeline) and `pipeline_sha256` the hash of its bytes (D33);
    `agents` (name@version), `models` and `backends` are what the run's steps resolved to.
    `overlay_file` and `overlay_sha256` are the team's overlay applied to the pipeline, by
    its policy repo path and the hash of its bytes (M2-23; None: no overlay), and
    `pipeline_version` the version of the pipeline the team gets (M2-15).
    `dry_run` runs every check and registers nothing."""

    run_id: str = Field(min_length=1, max_length=128)
    team: str = Field(pattern=TEAM_SLUG)
    repo: str | None = Field(default=None, max_length=300)
    pipeline: str = Field(min_length=1, max_length=128)
    pipeline_file: str | None = Field(default=None, max_length=300)
    pipeline_sha256: str | None = Field(default=None, max_length=80)
    pipeline_version: str | None = Field(default=None, max_length=64)
    overlay_file: str | None = Field(default=None, max_length=300)
    overlay_sha256: str | None = Field(default=None, max_length=80)
    policy_version: str = Field(min_length=1, max_length=80)
    agents: list[str] = Field(default_factory=list, max_length=200)
    models: list[str] = Field(default_factory=list, max_length=200)
    backends: list[str] = Field(default_factory=list, max_length=50)
    dry_run: bool = False
    budget_usd: str | None = Field(default=None, pattern=r"^\d{1,9}(\.\d{1,6})?$")
    """The run's budget (a decimal string): budget alerts at 80 % and 100 % (M2-16)."""


class OrgCheck(BaseModel):
    """One org check: `check` is rbac, killswitch, policy, pipeline, versions, concurrency
    or budget."""

    check: str
    passed: bool
    detail: str
    value: Any = None


class RunPreflight(BaseModel):
    """The org checks' report. `registered`: every check passed and the run holds a slot.
    `team_budget_remaining_usd` is None while the gateway does not measure it - never 0."""

    registered: bool
    checks: list[OrgCheck]
    team_budget_remaining_usd: str | None = None
    min_cli_version: str | None = None


#: A service account's name: lower-case letters, digits and '-' (M2-14).
SERVICE_NAME = r"^[a-z][a-z0-9-]{1,40}$"


class ServiceAccountRequest(RequestModel):
    """`POST /v1/service-accounts`: a team-scoped account that may only trigger runs. Its
    token expires after `days` (required, at most 365: owner decision 75)."""

    name: str = Field(pattern=SERVICE_NAME)
    team: str = Field(pattern=TEAM_SLUG)
    days: int = Field(ge=1, le=365)


class ServiceAccountIssued(BaseModel):
    """The one response that carries the service account's token."""

    id: UUID
    email: str
    team: str
    token: str = Field(repr=False)
    token_id: UUID
    expires_at: datetime


class ServiceAccountInfo(BaseModel):
    id: UUID
    email: str
    team: str
    status: str
    created_at: datetime


class RoleImportRequest(RequestModel):
    """`POST /v1/rbac/import`: role assignments as CSV text (`email,role,team,handle`).
    A dry run (the default) only reports what applying would do."""

    csv: str = Field(max_length=2_000_000)
    dry_run: bool = True


class ImportRow(BaseModel):
    """One CSV row's outcome: `add`, `unchanged` or `error` (with `error` saying why).
    `line` is the row's first line in the file (1 is the header)."""

    line: int
    email: str = ""
    role: str = ""
    team: str | None = None
    handle: str | None = None
    action: Literal["add", "unchanged", "error"]
    error: str | None = None


class RoleImportResult(BaseModel):
    applied: bool
    added: int
    unchanged: int
    errors: int
    rows: list[ImportRow]


class InviteInfo(BaseModel):
    """An invite without its token (only the invitee's mail carries the link)."""

    id: UUID
    email: str
    expires_at: datetime
    mailed: bool


#: Invite tokens start with this (opaque; only their sha256 is stored).
INVITE_PREFIX = "aci_"


class InviteRedeem(RequestModel):
    """`POST /v1/invites/redeem`: the token from the invite link (its `#` fragment)."""

    token: str = Field(min_length=10, max_length=200, repr=False)


class InviteRedeemed(BaseModel):
    """What a redeemed invite gives, once: a control-plane token and, when the deployment
    issues them, a gateway proxy key."""

    tenant: TenantRef
    email: str
    control_plane_url: str | None = None
    control_plane_token: str = Field(repr=False)
    token_expires_at: datetime
    gateway_url: str | None = None
    gateway_key: str | None = Field(default=None, repr=False)
    gateway_key_id: str | None = None
    notice: str | None = None


class OffboardRequest(RequestModel):
    """`POST /v1/users/{email}/offboard`: why (recorded in the audit chain)."""

    reason: str = Field(min_length=1, max_length=500)


class OffboardResult(BaseModel):
    email: str
    tokens_revoked: int
    invites_revoked: int
    proxy_keys_revoked: list[str]
    proxy_keys_pending: list[str]
    kill_switch: UUID | None = None


class ProxyKeyIssued(BaseModel):
    """A new gateway proxy key, in this response only."""

    key: str = Field(repr=False)
    key_id: str
    gateway_url: str | None = None


class ProxyKeysRevoked(BaseModel):
    email: str
    revoked: list[str]
    pending: list[str]


class NotificationInfo(BaseModel):
    """A queued, sent or failed mail (never its body)."""

    id: UUID
    kind: str
    recipient: str
    subject: str
    status: Literal["pending", "sent", "failed"]
    attempts: int
    next_attempt_at: datetime
    last_error: str | None = None
    created_at: datetime
    sent_at: datetime | None = None


class RoutingApplyInfo(BaseModel):
    """One routing apply (M2-09): which gateway (TokenLean) routing version applied which
    policy version, or why it did not."""

    id: UUID
    policy_version: str
    status: Literal["pending", "applied", "invalid", "failed", "superseded", "skipped"]
    attempts: int
    gateway_version: int | None = None
    previous_gateway_version: int | None = None
    plan_sha256: str | None = None
    problems: list[str] = []
    warnings: list[str] = []
    last_error: str | None = None
    requested_by: str
    created_at: datetime
    next_attempt_at: datetime
    applied_at: datetime | None = None


#: A personal backup's device id (M2-20): 32 lowercase hex characters, made per laptop.
DEVICE_ID = r"^[0-9a-f]{32}$"


class PersonalIngestRequest(RequestModel):
    """A batch of one device's chained personal-backup events (M2-20)."""

    entries: list[dict[str, Any]] = Field(min_length=1, max_length=5000)


#: The most files one `GET /v1/personal/files` page returns (each up to 256 KiB of text);
#: a shorter page is the last.
MAX_FILES_PAGE = 50


class PersonalFile(BaseModel):
    """The newest backed-up copy of one personal skill file or note (M2-20). A removed file
    is `deleted`; a held-back one (`held`: the publish scan found a secret or personal
    data) has no content. Only its owner ever reads this."""

    kind: Literal["skill", "memory"]
    path: str
    sha256: str
    content: str | None = None
    deleted: bool = False
    held: bool = False
    device: str
    seq: int
    at: str


def invite_url(public_url: str, token: str) -> str:
    """The link an invite mail carries: the token in the fragment, which no server and no
    log ever sees, and which a mail scanner opening the link cannot consume (ruling P8)."""
    return f"{public_url.rstrip('/')}/invite#{token}"


def parse_invite(link: str) -> tuple[str | None, str]:
    """(control-plane base URL or None, token) from an invite link or a bare token."""
    text = link.strip()
    if "#" not in text:
        return None, text
    base, token = text.split("#", 1)
    base = base.rstrip("/")
    base = base.removesuffix("/invite").rstrip("/")
    return base or None, token.strip()


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
    head: ChainHead | None = None
    """`ingest.gap` / `ingest.fork`: the chain head the server holds (resend from it)."""
    problems: list[str] | None = None
    """`ingest.chain_broken`: each problem, naming the event by its seq."""
