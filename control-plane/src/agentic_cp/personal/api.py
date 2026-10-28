"""`/v1/personal` (M2-20): a user's personal skill and note backups, for that user only.

Both routes need `skills.personal.edit` at some scope (a developer holds it in their team,
a repo maintainer in their repo); a service principal never does. Rows are always filtered
by the caller's own user id: no other user of the tenant - an org admin included - reads
someone's personal content here. The audit chain shows each event's path and sha256."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request

from agentic.orgapi import (
    API_PREFIX,
    MAX_FILES_PAGE,
    IngestResult,
    PersonalFile,
    PersonalIngestRequest,
)
from agentic_cp.auth.deps import Caller
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem
from agentic_cp.personal.store import Quota, ingest_personal, latest_files
from agentic_cp.rbac.authz import effective_permissions
from agentic_cp.tenancy.context import tenant_tx

router = APIRouter(prefix=f"{API_PREFIX}/personal", tags=["personal"], responses=PROBLEM_RESPONSES)
PERMISSION = "skills.personal.edit"


def _device(value: str) -> str:
    """A device id as 32 lowercase hex characters (any UUID spelling is accepted)."""
    try:
        return uuid.UUID(value).hex
    except ValueError:
        raise ApiProblem(
            422, "invalid_request", "device: not a device id (32 hex characters)"
        ) from None


async def _require(ctx, conn, cache) -> None:
    held = await effective_permissions(ctx, conn=conn, cache=cache)
    scopes = [held.org, *held.teams.values(), *held.repos.values()]
    if not any(PERMISSION in permissions for permissions in scopes):
        raise ApiProblem(
            403,
            "rbac.forbidden",
            f"you do not hold {PERMISSION} anywhere: personal skills and notes are for people "
            "who run agents",
            hint="ask an org admin or your team lead for a role that grants it",
        )


@router.post("/devices/{device}/events")
async def ingest(
    device: str, body: PersonalIngestRequest, request: Request, ctx: Caller
) -> IngestResult:
    """Append a batch of one device's chained backup events: verified, stored all or
    nothing; replayed entries are no-ops (`duplicates`)."""
    state = request.app.state
    device_id = _device(device)
    if len(body.entries) > state.settings.max_ingest_entries:
        raise ApiProblem(
            413,
            "ingest.too_large",
            f"a batch holds at most {state.settings.max_ingest_entries} events",
            hint="send the events in smaller batches",
        )
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await _require(ctx, conn, state.enforcers)
        return await ingest_personal(
            conn,
            user_id=ctx.user_id,
            actor=ctx.email,
            actor_kind=ctx.actor_kind,
            device=device_id,
            entries=body.entries,
            clock=state.clock,
            quota=Quota(
                devices=state.settings.personal_max_devices,
                stored_bytes=state.settings.personal_max_bytes,
                daily_bytes=state.settings.personal_daily_bytes,
            ),
        )


@router.get("/files")
async def files(
    request: Request,
    ctx: Caller,
    after: Annotated[str, Query(max_length=1024)] = "",
    limit: Annotated[int, Query(ge=1, le=MAX_FILES_PAGE)] = MAX_FILES_PAGE,
) -> list[PersonalFile]:
    """The caller's newest backed-up copy of each personal file (for `agentic skills
    restore`), a page at a time in path order: at most `limit` paths after `after`; a page
    shorter than `limit` is the last (review F4)."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await _require(ctx, conn, state.enforcers)
        found = await latest_files(conn, ctx.user_id, after=after, limit=limit)
    return [PersonalFile(**item) for item in found]
