"""Tenant bootstrap (ND-4): a tenant, its roles, its first org admin and that admin's token,
in one owner-role transaction inside the new tenant's context. Audited as
`system:operator`: `tenant.created` then `rbac.bootstrap` open the tenant's chain."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from agentic_cp.audit import OPERATOR, AuditEvent, append, format_at
from agentic_cp.auth.tokens import issue_token, token_lifetime
from agentic_cp.auth.users import create_user
from agentic_cp.gitops import RepoUrlError, check_repo_url
from agentic_cp.rbac.defaults import ORG_ADMIN, seed_roles
from agentic_cp.settings import Settings
from agentic_cp.tables import assignments
from agentic_cp.tenancy.context import TenancyError, tenant_tx
from agentic_cp.tenancy.tenants import TenantRow, bump_rbac_revision, create_tenant


@dataclass(frozen=True)
class BootstrapResult:
    tenant: TenantRow
    admin_id: UUID
    token_id: UUID
    token: str

    def __repr__(self) -> str:  # never the token
        return f"BootstrapResult(tenant={self.tenant!r}, admin_id={self.admin_id})"


def checked_policy_repo(url: str | None, settings: Settings) -> str | None:
    """A policy repo URL a tenant may use (R12, ND-15), or `TenancyError`; the message
    never repeats the URL."""
    if url is None:
        return None
    try:
        return check_repo_url(url, allow_local=settings.allow_local_repos)
    except RepoUrlError as error:
        raise TenancyError(str(error)) from None


async def bootstrap_tenant(
    owner_engine: AsyncEngine,
    settings: Settings,
    *,
    slug: str,
    name: str,
    admin_email: str,
    policy_repo: str | None = None,
    policy_branch: str | None = None,
    gateway_tenant: str | None = None,
    clock: Callable[[], datetime],
) -> BootstrapResult:
    policy_repo = checked_policy_repo(policy_repo, settings)
    try:  # the environment's branch (M3-04) unless the tenant names one
        policy_branch = settings.policy_branch_for(policy_branch)
    except ValueError as error:
        raise TenancyError(str(error)) from None
    tenant_id = uuid.uuid4()
    async with tenant_tx(owner_engine, tenant_id) as conn:
        tenant = await create_tenant(
            conn,
            slug,
            name,
            tenant_id=tenant_id,
            policy_repo=policy_repo,
            policy_branch=policy_branch,
            gateway_tenant=gateway_tenant,
        )
        await append(
            conn,
            AuditEvent(
                event="tenant.created",
                actor=OPERATOR,
                actor_kind="system",
                target=f"tenant:{slug}",
                after={
                    "slug": slug,
                    "name": name,
                    "policy_repo": policy_repo,
                    "policy_branch": policy_branch,
                    "gateway_tenant": gateway_tenant,
                },
            ),
            clock=clock,
        )
        seeded = await seed_roles(conn, clock=clock, audit=False)
        admin = await create_user(conn, admin_email, kind="human")
        await conn.execute(
            assignments.insert().values(
                tenant_id=tenant_id,
                user_id=admin.id,
                role=ORG_ADMIN,
                scope_kind="org",
                scope_value="",
                created_by=OPERATOR,
            )
        )
        revision = await bump_rbac_revision(conn)
        await append(
            conn,
            AuditEvent(
                event="rbac.bootstrap",
                actor=OPERATOR,
                actor_kind="system",
                target=f"user:{admin.email}",
                after={
                    "roles": seeded,
                    "assigned": {"user": admin.email, "role": ORG_ADMIN, "scope": "org"},
                    "rbac_revision": revision,
                },
            ),
            clock=clock,
        )
        expires_at = clock() + token_lifetime(settings, "human", None)
        token_id, value = await issue_token(
            conn, user_id=admin.id, name="bootstrap", expires_at=expires_at, created_by=OPERATOR
        )
        await append(
            conn,
            AuditEvent(
                event="token.issued",
                actor=OPERATOR,
                actor_kind="system",
                target=f"token:{token_id}",
                after={
                    "name": "bootstrap",
                    "user": admin.email,
                    "expires_at": format_at(expires_at),
                },
            ),
            clock=clock,
        )
    return BootstrapResult(tenant=tenant, admin_id=admin.id, token_id=token_id, token=value)
