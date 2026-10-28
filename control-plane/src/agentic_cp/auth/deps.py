"""The `current_token` dependency: every authenticated route's only source of the tenant.

The tenant is taken from the token, never from a header, path, query or body. Unknown,
malformed, revoked, disabled-user and suspended-tenant tokens all get the same 401 (no
oracle); an expired token says so, with the login hint."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agentic_cp.auth.tokens import TokenContext, resolve_token, touch_token, well_formed
from agentic_cp.errors import ApiProblem
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.ratelimit import NoLimit

LOGIN_HINT = "run `agentic login --for control-plane` to get a new token"
_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="agentic-control-plane"'}

bearer = HTTPBearer(
    auto_error=False,
    description="A control-plane token (`acp_...`) from `agentic login --for control-plane`",
)


def _invalid() -> ApiProblem:
    return ApiProblem(
        401, "auth.invalid_token", "the token is not valid", hint=LOGIN_HINT, headers=_CHALLENGE
    )


async def current_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> TokenContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiProblem(
            401,
            "auth.missing_token",
            "this route needs a control-plane token",
            hint=LOGIN_HINT,
            headers=_CHALLENGE,
        )
    value = credentials.credentials
    if not well_formed(value):
        raise _invalid()
    state = request.app.state
    async with state.engine.begin() as conn:  # no tenant yet: the definer function only
        resolved = await resolve_token(conn, value)
    if (
        resolved is None
        or resolved.revoked_at is not None
        or resolved.user_status != "active"
        or resolved.tenant_status != "active"
    ):
        raise _invalid()
    now: datetime = state.clock()
    if resolved.expires_at <= now:
        raise ApiProblem(
            401, "auth.token_expired", "the token has expired", hint=LOGIN_HINT, headers=_CHALLENGE
        )
    ctx = TokenContext(
        tenant_id=resolved.tenant_id,
        tenant_slug=resolved.tenant_slug,
        user_id=resolved.user_id,
        email=resolved.email,
        kind=resolved.kind,
        token_id=resolved.token_id,
        rbac_revision=resolved.rbac_revision,
        expires_at=resolved.expires_at,
    )
    # Routes open every transaction with `tenant_tx(engine, ctx.tenant_id)`; the context
    # variable is not set here, so it can never outlive the request (a transaction opened
    # without tenant_tx has no tenant: no rows, no writes).
    limiter = state.rate_limiter if state.rate_limiter is not None else NoLimit()
    route = getattr(request.scope.get("route"), "path", request.url.path)
    decision = await limiter.check(ctx.tenant_id, ctx.user_id, route)
    if not decision.allowed:
        headers = {"Retry-After": str(decision.retry_after)} if decision.retry_after else None
        raise ApiProblem(429, "rate.limited", "too many requests; try again later", headers=headers)
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await touch_token(conn, ctx.token_id, now=now)
    return ctx


#: The authenticated caller, for route signatures: `ctx: Caller`.
Caller = Annotated[TokenContext, Depends(current_token)]
