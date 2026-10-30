"""The learning loop's decisions (M3-15, M3-16), in the caller's tenant only.

  * `GET  /v1/learning/proposals/{id}`            - one proposal with its evidence
  * `POST /v1/learning/proposals`                 - teach one correction now (propose --from)
  * `PUT  /v1/learning/proposals/{id}/draft`      - retarget or reword it (its owner)
  * `POST /v1/learning/proposals/{id}/decision`   - approve, reject or defer (its owner)
  * `POST /v1/learning/proposals/{id}/endorse`    - back it (anyone who may read it)
  * `POST /v1/learning/proposals/{id}/applied`    - its owner's CLI made the change
  * `POST /v1/learning/proposals/{id}/reverted`   - ... and undid it

**Owner-only (D30).** A proposal's target is the narrowest scope its evidence supports: one
person's corrections, recorded by that person's CLI, are **personal** - decided by that user
and nobody else; otherwise **repo** (`agent.approve.repo` on the repository: its CODEOWNERS
maintainers and the team's leads) or **team** (`agent.publish.team`). A person's `edit` may
widen the target, never narrow it. A developer may endorse a shared proposal - that adds
weight - but never decide it.

**Nothing is applied without approve (D29).** The status moves only along
`agentic.learning.approval.TRANSITIONS`: `applied` follows `approved` and nothing else.
The change itself is made by the approver's CLI (a personal file, a pull request) and
reported here; every step is on the tenant's audit chain (`learning.edited`, `.approved`,
`.rejected`, `.deferred`, `.endorsed`, `.applied`, `.reverted`, `.proposed`).

Who may read a proposal: the team's viewers (`dashboard.view.team`), the authors of its
evidence, and whoever may decide it. Anyone else - another tenant above all - gets 404,
exactly as for a missing proposal (DR-5).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from sqlalchemy import select, update

from agentic.learning.approval import APPROVE_PERMISSION, DecisionError, next_status
from agentic.learning.changes import SCOPE_OF, SCOPE_ORDER
from agentic.learning.clustering import proposal_id
from agentic.orgapi import (
    API_PREFIX,
    ChangeReport,
    ProposalDecision,
    ProposalDetail,
    ProposalEdit,
    ProposalInfo,
    ProposeFrom,
)
from agentic_cp.audit import AuditEvent, append
from agentic_cp.auth.deps import Caller
from agentic_cp.auth.tokens import TokenContext
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem, not_found
from agentic_cp.learning.api import VIEW, _feedback_query, _info, _viewable_teams
from agentic_cp.learning.digest import queue_digest
from agentic_cp.learning.targets import narrowest_of, target_of
from agentic_cp.rbac.authz import Org, Repo, Team, authorize
from agentic_cp.tables import feedback, learning_proposals, users
from agentic_cp.tenancy.context import require_tenant_id, tenant_tx

router = APIRouter(prefix=API_PREFIX, tags=["learning"], responses=PROBLEM_RESPONSES)
DECIDED_EVENT = {"approve": "approved", "reject": "rejected", "defer": "deferred"}


async def _owner_email(conn, row) -> str | None:
    if target_of(row) != "personal" or not row.authors:
        return None
    found = await conn.execute(select(users.c.email).where(users.c.id == row.authors[0]))
    return found.scalar_one_or_none()


async def owner_emails(conn, rows) -> dict[str, str]:
    """The deciding user's email for each personal proposal among `rows` (one query)."""
    wanted = {row.id: row.authors[0] for row in rows if target_of(row) == "personal"}
    if not wanted:
        return {}
    found = await conn.execute(
        select(users.c.id, users.c.email).where(users.c.id.in_(set(wanted.values())))
    )
    emails = {item.id: item.email for item in found}
    return {pid: emails[uid] for pid, uid in wanted.items() if uid in emails}


def proposal_info(row, owner: str | None = None, cls=ProposalInfo):
    target = target_of(row)
    if target != "personal":
        owner = row.scope if target == "repo" else f"team:{row.team}"
    return cls(
        id=row.id,
        team=row.team,
        agent=row.agent,
        scope=row.scope,
        lesson=row.lesson,
        evidence=list(row.evidence),
        status=row.status,
        created_at=row.created_at,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
        target=target,
        owner=owner or row.owner,
        actors=list(row.actors),
        endorsements=list(row.endorsements),
        kind=row.kind,
        edited_lesson=row.edited_lesson,
        applied_ref=row.applied_ref,
        reverted_ref=row.reverted_ref,
        measurement=row.measurement,
    )


async def may_decide(ctx: TokenContext, conn, row, cache) -> bool:
    """Whether the caller owns what the proposal would change (D30)."""
    target = target_of(row)
    if target == "personal":
        return len(set(row.authors)) == 1 and row.authors[0] == ctx.user_id
    permission = APPROVE_PERMISSION[target]
    if target == "repo":
        resource = Repo(row.scope.removeprefix("repo:"), row.team)
    elif target == "team":
        resource = Team(row.team)
    else:
        resource = Org()
    return (await authorize(ctx, permission, resource, conn=conn, cache=cache)).allowed


async def _readable(ctx, conn, row, cache) -> bool:
    if ctx.user_id in row.authors:
        return True
    teams = await _viewable_teams(ctx, conn, cache)
    if teams is None or row.team in teams:
        return True
    return await may_decide(ctx, conn, row, cache)


async def _row_for(ctx, conn, pid: str, cache):
    row = (
        await conn.execute(
            select(learning_proposals).where(learning_proposals.c.id == pid).with_for_update()
        )
    ).first()
    if row is None or not await _readable(ctx, conn, row, cache):
        raise not_found("proposal")
    return row


async def _owned(ctx, conn, pid: str, cache, what: str):
    row = await _row_for(ctx, conn, pid, cache)
    if not await may_decide(ctx, conn, row, cache):
        target = target_of(row)
        who = (
            "the person whose corrections it holds"
            if target == "personal"
            else f"a holder of {APPROVE_PERMISSION[target]} (the owner of what it changes)"
        )
        raise ApiProblem(
            403,
            "rbac.forbidden",
            f"only {who} may {what} proposal {pid}; you may endorse it instead",
        )
    return row


def _moved(row, action: str) -> str:
    try:
        return next_status(row.status, action)
    except DecisionError as error:
        raise ApiProblem(409, "learning.status", str(error)) from None


async def _audit(conn, ctx, row, event: str, clock, *, after: dict, reason: str = "") -> None:
    await append(
        conn,
        AuditEvent(
            event=f"learning.{event}",
            actor=ctx.email,
            actor_kind=ctx.actor_kind,
            target=f"proposal:{row.id}",
            team=row.team,
            before={"status": row.status},
            after={"agent": row.agent, "target": target_of(row), **after},
            reason=reason[:300],
        ),
        clock=clock,
    )


async def _detail(conn, row, mine: bool) -> ProposalDetail:
    found = await conn.execute(_feedback_query().where(feedback.c.id.in_(list(row.evidence))))
    info = proposal_info(row, await _owner_email(conn, row), cls=ProposalDetail)
    order = {fid: n for n, fid in enumerate(row.evidence)}
    records = sorted((_info(r) for r in found), key=lambda f: order.get(f.id, 0))
    return info.model_copy(update={"feedback": records, "draft": row.draft, "may_decide": mine})


async def _updated(conn, row, values: dict):
    await conn.execute(
        update(learning_proposals).where(learning_proposals.c.id == row.id).values(**values)
    )
    return (
        await conn.execute(select(learning_proposals).where(learning_proposals.c.id == row.id))
    ).one()


@router.get("/learning/proposals/{pid}")
async def get_proposal(pid: str, request: Request, ctx: Caller) -> ProposalDetail:
    """One proposal, its evidence (the feedback records behind it), once approved the draft
    its owner approved, and whether the caller may decide it (`may_decide`: the CLI asks
    before it makes or undoes the change, so a reader who does not own it changes nothing,
    review F4)."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        row = await _row_for(ctx, conn, pid, state.enforcers)
        return await _detail(conn, row, await may_decide(ctx, conn, row, state.enforcers))


@router.put("/learning/proposals/{pid}/draft")
async def edit_proposal(
    pid: str, body: ProposalEdit, request: Request, ctx: Caller
) -> ProposalInfo:
    """Retarget (`kind`) or reword (`lesson`) a pending or deferred proposal: its owner only.
    A kind narrower than the evidence supports is refused (422)."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        row = await _owned(ctx, conn, pid, state.enforcers, "edit")
        if row.status not in ("pending", "deferred"):
            raise ApiProblem(409, "learning.status", f"a {row.status} proposal cannot be edited")
        values: dict = {}
        if body.kind is not None:
            floor = SCOPE_ORDER.index(narrowest_of(row))
            allowed = [k for k, s in SCOPE_OF.items() if SCOPE_ORDER.index(s) >= floor]
            if body.kind not in allowed:
                raise ApiProblem(
                    422,
                    "learning.kind",
                    f"{body.kind!r} is not a change kind this proposal can take",
                    hint=f"one of {', '.join(allowed)}",
                )
            values["kind"] = body.kind
        if body.lesson is not None:
            values["edited_lesson"] = " ".join(body.lesson.split())
        if not values:
            raise ApiProblem(422, "learning.edit", "say what to change: kind and/or lesson")
        await _audit(conn, ctx, row, "edited", state.clock, after=values)
        changed = await _updated(conn, row, values)
        return proposal_info(changed, await _owner_email(conn, changed))


@router.post("/learning/proposals/{pid}/decision")
async def decide_proposal(
    pid: str, body: ProposalDecision, request: Request, ctx: Caller
) -> ProposalInfo:
    """Approve, reject or defer: the owner of what it changes, only (D30). A rejected lesson
    is remembered: it is proposed again only with new evidence."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        row = await _owned(ctx, conn, pid, state.enforcers, body.action)
        status = _moved(row, body.action)
        now: datetime = state.clock()
        owner = await _owner_email(conn, row)
        values: dict = {"status": status, "decided_at": now, "decided_by": ctx.email}
        if owner:
            values["owner"] = owner
        if body.action == "approve" and body.draft is not None:
            values["draft"] = body.draft
        await _audit(
            conn,
            ctx,
            row,
            DECIDED_EVENT[body.action],
            state.clock,
            after={"status": status, "endorsements": len(row.endorsements)},
            reason=body.reason,
        )
        changed = await _updated(conn, row, values)
        return proposal_info(changed, owner)


@router.post("/learning/proposals/{pid}/endorse")
async def endorse_proposal(pid: str, request: Request, ctx: Caller) -> ProposalInfo:
    """Back a proposal (weight for its owner; never a decision). Once per person."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        row = await _row_for(ctx, conn, pid, state.enforcers)
        if ctx.email in row.endorsements:
            return proposal_info(row, await _owner_email(conn, row))
        endorsements = [*row.endorsements, ctx.email]
        await _audit(
            conn, ctx, row, "endorsed", state.clock, after={"endorsements": len(endorsements)}
        )
        changed = await _updated(conn, row, {"endorsements": endorsements})
        return proposal_info(changed, await _owner_email(conn, changed))


async def _report(request: Request, ctx, pid: str, body: ChangeReport, action: str):
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        row = await _owned(ctx, conn, pid, state.enforcers, action)
        status = _moved(row, action)
        now = state.clock()
        if action == "apply":
            values = {"status": status, "applied_at": now, "applied_ref": body.ref}
            if body.measurement is not None:
                values["measurement"] = body.measurement
            event = "applied"
        else:
            # a revert is a decision: a reverted lesson is proposed again only on corrections
            # made after it, not on those made while it was in force (review F6)
            values = {
                "status": status,
                "reverted_at": now,
                "reverted_ref": body.ref,
                "decided_at": now,
                "decided_by": ctx.email,
            }
            event = "reverted"
        ref = {k: body.ref.get(k) for k in ("kind", "url", "number", "branch") if k in body.ref}
        await _audit(conn, ctx, row, event, state.clock, after={"status": status, **ref})
        changed = await _updated(conn, row, values)
        return proposal_info(changed, await _owner_email(conn, changed))


@router.post("/learning/proposals/{pid}/applied")
async def report_applied(
    pid: str, body: ChangeReport, request: Request, ctx: Caller
) -> ProposalInfo:
    """The owner's CLI applied the approved change (a personal write, a pull request)."""
    return await _report(request, ctx, pid, body, "apply")


@router.post("/learning/proposals/{pid}/reverted")
async def report_reverted(
    pid: str, body: ChangeReport, request: Request, ctx: Caller
) -> ProposalInfo:
    """The owner's CLI reverted the applied change (Zyroflo's undo, a revert pull request)."""
    return await _report(request, ctx, pid, body, "revert")


@router.post("/learning/proposals")
async def propose_from(body: ProposeFrom, request: Request, ctx: Caller) -> ProposalInfo:
    """Teach one correction now (spec §24 step 3: an explicit request). The record's author
    or a viewer of its team; the same request again returns the same proposal."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        row = (
            await conn.execute(_feedback_query().where(feedback.c.id == body.feedback_id))
        ).first()
        if row is None:
            raise not_found("feedback record")
        if row.author_id != ctx.user_id:
            teams = await _viewable_teams(ctx, conn, state.enforcers)
            if teams is not None and row.team not in teams:
                raise not_found("feedback record")
        agents = list(row.agents)
        agent = body.agent or (agents[0] if agents else None)
        if agent is None or agent not in agents:
            raise ApiProblem(
                422,
                "learning.agent",
                f"{body.feedback_id} teaches {', '.join(agents) or 'no agent'}"
                + (f", not {body.agent}" if body.agent else ""),
                hint="correct its attribution first (PUT .../attribution)",
            )
        lesson = row.lesson or str(row.record.get("text", ""))
        scope = f"repo:{row.repo or 'local'}"
        pid = proposal_id(agent, scope, f"explicit {row.id} {lesson}")
        held = (
            await conn.execute(select(learning_proposals).where(learning_proposals.c.id == pid))
        ).first()
        if held is not None:
            return proposal_info(held, await _owner_email(conn, held))
        values = {
            "tenant_id": await require_tenant_id(conn),
            "id": pid,
            "team": row.team,
            "agent": agent,
            "scope": scope,
            "lesson": lesson[:200],
            "evidence": [row.id],
            "sources": [str(row.record.get("source", row.id))],
            "actors": [row.actor],
            "authors": [row.author_id],
            "status": "pending",
            "created_at": state.clock(),
        }
        await conn.execute(learning_proposals.insert().values(**values))
        made = (
            await conn.execute(select(learning_proposals).where(learning_proposals.c.id == pid))
        ).one()
        await append(
            conn,
            AuditEvent(
                event="learning.proposed",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target=f"proposal:{pid}",
                team=row.team,
                after={"agent": agent, "scope": scope, "evidence": [row.id], "explicit": True},
                reason="an explicit request (agentic learn propose --from)",
            ),
            clock=state.clock,
        )
        await queue_digest(conn, row.team, clock=state.clock)  # its deciders hear of it
        return proposal_info(made, await _owner_email(conn, made))


__all__ = ["VIEW", "may_decide", "owner_emails", "proposal_info", "router", "target_of"]
