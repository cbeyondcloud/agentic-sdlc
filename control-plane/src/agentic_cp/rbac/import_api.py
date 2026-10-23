"""`POST /v1/rbac/import` (M2-14, ruling P7): role assignments from CSV, in the caller's
tenant. A dry run (the default) answers what applying would do, row by row; applying with
any error row refuses the whole file (422, each problem naming its line). Applying an
unchanged file writes nothing and records nothing, so an import is idempotent."""

from __future__ import annotations

from fastapi import APIRouter, Request

from agentic.orgapi import API_PREFIX, RoleImportRequest, RoleImportResult
from agentic_cp.audit import AuditEvent, append
from agentic_cp.auth.deps import Caller
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem
from agentic_cp.rbac.authz import effective_permissions
from agentic_cp.rbac.importer import apply_import, parse, plan_import
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import bump_rbac_revision, lock_rbac

router = APIRouter(prefix=f"{API_PREFIX}/rbac", tags=["rbac"], responses=PROBLEM_RESPONSES)


@router.post("/import")
async def import_roles(body: RoleImportRequest, request: Request, ctx: Caller) -> RoleImportResult:
    """Org admins import any row; team leads rows of their own team (each row is checked
    like a single assignment)."""
    state = request.app.state
    rows, errors = parse(body.csv)
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await lock_rbac(conn)
        held = await effective_permissions(ctx, conn=conn, cache=state.enforcers)
        if "rbac.assign" not in held.org and not any(
            "rbac.assign.team" in permissions for permissions in held.teams.values()
        ):
            raise ApiProblem(
                403,
                "rbac.forbidden",
                "you do not hold rbac.assign for the org or rbac.assign.team for a team",
                hint="ask an org admin or your team lead for a role that grants it",
            )
        planned = await plan_import(conn, ctx, state.enforcers, rows)
        shown = sorted(errors + planned, key=lambda row: row.line)
        result = RoleImportResult(
            applied=False,
            added=sum(row.action == "add" for row in shown),
            unchanged=sum(row.action == "unchanged" for row in shown),
            errors=sum(row.action == "error" for row in shown),
            rows=shown,
        )
        if body.dry_run:
            return result
        if result.errors:
            raise ApiProblem(
                422,
                "rbac.import_invalid",
                f"{result.errors} row(s) have errors; nothing was imported",
                hint="run the import as a dry run, fix the rows it names, then apply it",
                extra={
                    "problems": [
                        f"line {row.line}: {row.error}" for row in shown if row.action == "error"
                    ][:100]
                },
            )
        if result.added:
            revision = await bump_rbac_revision(conn)
            adding = {row.line for row in planned if row.action == "add"}
            await apply_import(conn, ctx, state, [r for r in rows if r.line in adding], revision)
            await append(
                conn,
                AuditEvent(
                    event="rbac.imported",
                    actor=ctx.email,
                    actor_kind=ctx.actor_kind,
                    target="assignments",
                    after={
                        "added": result.added,
                        "unchanged": result.unchanged,
                        "rbac_revision": revision,
                    },
                ),
                clock=state.clock,
            )
    return result.model_copy(update={"applied": True})
