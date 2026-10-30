"""Permission decisions through pycasbin (owner decision row 77). The ONLY module that
imports casbin (an import contract enforces it); callers use `authorize` / `require`.

Model: RBAC with domains (`model.conf`). Policy rows come from our own tables (DR-1):
`p, <role>, <permission>` per role permission and `g, user:<id>, <role>, <domain>` per
grant, where the domain is `<tenant_id>|org`, `<tenant_id>|team:<slug>` or
`<tenant_id>|repo:<key>` (DR-2). A read-only adapter loads them; the enforcer is built per
tenant, cached by `rbac_revision`, rebuilt when the revision moves and never mutated."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import casbin
from casbin import persist
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic.orgapi.rbac import PERMISSIONS, EffectivePermissions
from agentic_cp.auth.tokens import TokenContext
from agentic_cp.errors import ApiProblem
from agentic_cp.rbac.defaults import SERVICE_PERMISSIONS
from agentic_cp.rbac.store import PolicyRows, load_rows

MODEL_TEXT = Path(__file__).with_name("model.conf").read_text(encoding="utf-8")


@dataclass(frozen=True)
class Org:
    """The whole tenant."""


@dataclass(frozen=True)
class Team:
    slug: str


@dataclass(frozen=True)
class Repo:
    key: str  # host/owner/name
    team: str | None = None  # the repo's team, when known


Resource = Org | Team | Repo


@dataclass(frozen=True)
class AuthzDecision:
    allowed: bool
    role: str | None = None
    scope: str | None = None


def domain(tenant_id: UUID, scope: str) -> str:
    return f"{tenant_id}|{scope}"


class _RowsAdapter(persist.Adapter):
    """Loads one tenant's rows into the model; every write is refused (casbin's memory
    can never drift from the tables, because nothing writes through it)."""

    def __init__(self, rows: PolicyRows) -> None:
        self.rows = rows

    def load_policy(self, model) -> None:
        for role, permissions in sorted(self.rows.roles.items()):
            for permission in sorted(permissions):
                model.add_policy("p", "p", [role, permission])
        for grant in self.rows.grants:
            tenant = grant.tenant or self.rows.tenant_id
            model.add_policy(
                "g", "g", [f"user:{grant.user_id}", grant.role, domain(tenant, grant.scope)]
            )

    def save_policy(self, model):
        raise NotImplementedError("RBAC changes go through the tables, never the enforcer")

    def add_policy(self, sec, ptype, rule):
        raise NotImplementedError("RBAC changes go through the tables, never the enforcer")

    def remove_policy(self, sec, ptype, rule):
        raise NotImplementedError("RBAC changes go through the tables, never the enforcer")

    def remove_filtered_policy(self, sec, ptype, field_index, *field_values):
        raise NotImplementedError("RBAC changes go through the tables, never the enforcer")


def build_enforcer(rows: PolicyRows) -> casbin.Enforcer:
    """A fresh, read-only enforcer for one tenant's rows."""
    model = casbin.Enforcer.new_model(text=MODEL_TEXT)
    return casbin.Enforcer(model, _RowsAdapter(rows))


def candidate_scopes(permission: str, resource: Resource) -> list[str]:
    """Where a grant of `permission` counts for `resource` (the catalogue kind)."""
    if permission not in PERMISSIONS:
        raise ValueError(f"unknown permission {permission!r}")
    kind = PERMISSIONS[permission]
    scopes = ["org"]
    if kind == "org":
        return scopes
    team = resource.slug if isinstance(resource, Team) else getattr(resource, "team", None)
    if team:
        scopes.append(f"team:{team}")
    if kind == "repo" and isinstance(resource, Repo):
        scopes.append(f"repo:{resource.key}")
    return scopes


def ask(
    enforcer: casbin.Enforcer, tenant_id: UUID, user_id: UUID, dom: str, permission: str
) -> tuple[bool, str | None]:
    """One casbin question. A domain of another tenant is refused before casbin is asked."""
    if not dom.startswith(f"{tenant_id}|"):
        return False, None
    allowed, explain = enforcer.enforce_ex(f"user:{user_id}", dom, permission)
    return bool(allowed), (explain[0] if allowed and explain else None)


def decide(
    enforcer: casbin.Enforcer,
    tenant_id: UUID,
    user_id: UUID,
    permission: str,
    resource: Resource,
) -> AuthzDecision:
    """The decision for one user, without a database (the matrix tests use it directly)."""
    for scope in candidate_scopes(permission, resource):
        allowed, role = ask(enforcer, tenant_id, user_id, domain(tenant_id, scope), permission)
        if allowed:
            return AuthzDecision(True, role=role, scope=scope)
    return AuthzDecision(False)


class EnforcerCache:
    """One enforcer per tenant, keyed by `rbac_revision`. A hit needs a revision at least
    as new as the caller's token saw; a miss reads the revision and the rows in the
    request's transaction and builds a new enforcer (never patches the old one)."""

    def __init__(self, max_tenants: int = 256) -> None:
        self.max_tenants = max_tenants
        self._entries: OrderedDict[UUID, tuple[int, casbin.Enforcer, dict[str, str]]] = (
            OrderedDict()
        )

    async def get(self, tenant_id: UUID, revision: int, conn: AsyncConnection) -> casbin.Enforcer:
        return (await self.entry(tenant_id, revision, conn))[0]

    async def entry(
        self, tenant_id: UUID, revision: int, conn: AsyncConnection
    ) -> tuple[casbin.Enforcer, dict[str, str]]:
        """The tenant's enforcer and the team of each repo someone maintains."""
        cached = self._entries.get(tenant_id)
        if cached is not None and cached[0] >= revision:
            self._entries.move_to_end(tenant_id)
            return cached[1], cached[2]
        current, rows = await load_rows(conn)
        if rows.tenant_id != tenant_id:  # the transaction is in another tenant
            raise ValueError("the enforcer was asked for a tenant other than the transaction's")
        enforcer = build_enforcer(rows)
        self._entries[tenant_id] = (current, enforcer, dict(rows.repo_teams))
        self._entries.move_to_end(tenant_id)
        while len(self._entries) > self.max_tenants:
            self._entries.popitem(last=False)
        return enforcer, dict(rows.repo_teams)


async def authorize(
    ctx: TokenContext,
    permission: str,
    resource: Resource,
    *,
    conn: AsyncConnection,
    cache: EnforcerCache,
    user_id: UUID | None = None,
    subject_kind: str | None = None,
) -> AuthzDecision:
    """May the caller (or `user_id`, another user of the caller's tenant, of
    `subject_kind`) do `permission` on `resource`? `conn` must be a transaction in the
    caller's tenant. A service principal never gets more than `SERVICE_PERMISSIONS`,
    whatever its grants say (M2-14: defence in depth)."""
    candidate_scopes(permission, resource)  # an unknown permission fails before any query
    kind = ctx.kind if user_id is None else subject_kind
    if kind == "service" and permission not in SERVICE_PERMISSIONS:
        return AuthzDecision(False)
    enforcer = await cache.get(ctx.tenant_id, ctx.rbac_revision, conn)
    return decide(enforcer, ctx.tenant_id, user_id or ctx.user_id, permission, resource)


def describe(resource: Resource) -> str:
    if isinstance(resource, Team):
        return f"team {resource.slug}"
    if isinstance(resource, Repo):
        return f"repo {resource.key}"
    return "the org"


async def require(
    ctx: TokenContext,
    permission: str,
    resource: Resource,
    *,
    conn: AsyncConnection,
    cache: EnforcerCache,
) -> AuthzDecision:
    """`authorize`, raising 403 `rbac.forbidden` (naming the permission and scope)."""
    decision = await authorize(ctx, permission, resource, conn=conn, cache=cache)
    if not decision.allowed:
        raise ApiProblem(
            403,
            "rbac.forbidden",
            f"you do not hold {permission} for {describe(resource)}",
            hint="ask an org admin or your team lead for a role that grants it",
        )
    return decision


async def effective_permissions(
    ctx: TokenContext, *, conn: AsyncConnection, cache: EnforcerCache
) -> EffectivePermissions:
    """The caller's permissions at org scope, in each team they hold a role in, and in each
    repo they maintain (with that repo's team)."""
    enforcer, repo_teams = await cache.entry(ctx.tenant_id, ctx.rbac_revision, conn)
    return permissions_for(enforcer, ctx.tenant_id, ctx.user_id, ctx.kind, repo_teams)


def held_in(enforcer: casbin.Enforcer, tenant_id: UUID, user_id: UUID, scope: str) -> set[str]:
    """What the user's grants in one scope give: the permissions of every role casbin's role
    manager links them to in that domain - the matcher's `g(r.sub, p.sub, r.dom) && r.act ==
    p.act` read from the role side, so casbin is not run once per permission (DF-406)."""
    subject = f"user:{user_id}"
    linked = {subject, *enforcer.get_implicit_roles_for_user(subject, domain(tenant_id, scope))}
    return {rule[1] for rule in enforcer.get_policy() if rule[0] in linked}


def permissions_for(
    enforcer: casbin.Enforcer,
    tenant_id: UUID,
    user_id: UUID,
    kind: str,
    repo_teams: dict[str, str],
) -> EffectivePermissions:
    """`effective_permissions` without a database: the same answer as `decide` asked for every
    permission in every scope (the scopes of `candidate_scopes`), computed once per scope.
    At 500 people per tenant asking per permission cost about 318 ms of CPU per `/v1/me`
    (M3-22)."""
    subject = f"user:{user_id}"
    prefix = f"{tenant_id}|"
    scopes = sorted(
        {
            rule[2][len(prefix) :]
            for rule in enforcer.get_grouping_policy()
            if rule[0] == subject and rule[2].startswith(prefix)
        }
    )
    at_org = held_in(enforcer, tenant_id, user_id, "org")
    org = frozenset(p for p in PERMISSIONS if p in at_org)
    teams = {}
    repos = {}
    for scope in scopes:
        if scope.startswith("team:"):
            granted = at_org | held_in(enforcer, tenant_id, user_id, scope)
            teams[scope[len("team:") :]] = frozenset(
                p for p in PERMISSIONS if PERMISSIONS[p] != "org" and p in granted
            )
        elif scope.startswith("repo:"):
            granted = at_org | held_in(enforcer, tenant_id, user_id, scope)
            repos[scope[len("repo:") :]] = frozenset(
                p for p in PERMISSIONS if PERMISSIONS[p] == "repo" and p in granted
            )
    shown = {key: team for key, team in repo_teams.items() if key in repos}
    if kind == "service":  # as `authorize` decides for a service principal
        org = org & SERVICE_PERMISSIONS
        teams = {slug: held & SERVICE_PERMISSIONS for slug, held in teams.items()}
        repos = {key: held & SERVICE_PERMISSIONS for key, held in repos.items()}
    return EffectivePermissions(org=org, teams=teams, repos=repos, repo_teams=shown)
