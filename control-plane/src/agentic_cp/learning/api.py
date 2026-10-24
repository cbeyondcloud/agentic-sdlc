"""The learning loop's routes (M3-11..M3-13), in the caller's tenant only.

  * `POST /v1/runs/{run_id}/feedback`                 - a batch of the run's feedback journal
  * `GET  /v1/learning/feedback`                      - the records the caller may read
  * `PUT  /v1/learning/feedback/{feedback_id}/attribution` - a person's correction, audited
  * `GET  /v1/learning/proposals`                     - the proposals the caller may read

Who reads what (D30): a feedback record is read by its author (the user whose CLI sent it)
and by holders of `dashboard.view.team` in its team (an org-wide grant reads every team);
a proposal by those team viewers and by the authors of its evidence. A correction needs the
author or `agent.approve.repo` in the team. A record the caller may not read - another
tenant's above all - is 404, exactly as a missing one (DR-5)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import or_, select

from agentic.learning.records import Attribution, Learner
from agentic.orgapi import (
    API_PREFIX,
    AttributionCorrection,
    FeedbackInfo,
    FeedbackIngestRequest,
    IngestResult,
    ProposalInfo,
)
from agentic_cp.auth.deps import Caller
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem, not_found
from agentic_cp.ingest.api import _checked_run_id
from agentic_cp.learning.store import ingest_feedback, set_attribution
from agentic_cp.rbac.authz import Team, authorize, effective_permissions
from agentic_cp.tables import feedback, learning_proposals, users
from agentic_cp.tenancy.context import tenant_tx

router = APIRouter(prefix=API_PREFIX, tags=["learning"], responses=PROBLEM_RESPONSES)
VIEW = "dashboard.view.team"
CORRECT = "agent.approve.repo"


@router.post("/runs/{run_id}/feedback")
async def ingest(
    run_id: str, body: FeedbackIngestRequest, request: Request, ctx: Caller
) -> IngestResult:
    """Append a batch of the run's chained feedback entries: verified, stored all or
    nothing; replayed entries are no-ops (`duplicates`). The run's own events come first:
    a run the control plane does not hold yet is 404 (the CLI sends the feedback later)."""
    state = request.app.state
    _checked_run_id(run_id)
    if len(body.entries) > state.settings.max_ingest_entries:
        raise ApiProblem(
            413,
            "ingest.too_large",
            f"a batch holds at most {state.settings.max_ingest_entries} entries",
            hint="send the feedback in smaller batches",
        )
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        return await ingest_feedback(
            conn, ctx, run_id, body.entries, cache=state.enforcers, clock=state.clock
        )


async def _viewable_teams(ctx, conn, cache) -> set[str] | None:
    """The teams whose records the caller reads (None: every team)."""
    held = await effective_permissions(ctx, conn=conn, cache=cache)
    if VIEW in held.org:
        return None
    return {team for team, permissions in held.teams.items() if VIEW in permissions}


def _visible(column_team, author_match, teams: set[str] | None):
    if teams is None:
        return None
    return or_(author_match, column_team.in_(sorted(teams)))


def _info(row) -> FeedbackInfo:
    record = row.record
    return FeedbackInfo(
        id=row.id,
        run_id=row.run_id,
        team=row.team,
        repo=row.repo,
        signal=row.signal,
        actor=row.actor,
        given_at=row.given_at,
        text=record.get("text", ""),
        source=record.get("source", ""),
        gate=record.get("gate"),
        kind=record.get("kind"),
        agent=record.get("agent"),
        path=record.get("path"),
        line=record.get("line"),
        attribution=Attribution.model_validate(row.attribution) if row.attribution else None,
        lesson=row.lesson,
        author=row.email,
        corrected_by=row.corrected_by,
    )


def _feedback_query():
    return select(feedback, users.c.email).join(
        users, (users.c.tenant_id == feedback.c.tenant_id) & (users.c.id == feedback.c.author_id)
    )


@router.get("/learning/feedback")
async def list_feedback(
    request: Request,
    ctx: Caller,
    team: Annotated[str | None, Query(max_length=64)] = None,
    run_id: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[FeedbackInfo]:
    """The newest feedback records the caller may read, optionally of one team or run."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        teams = await _viewable_teams(ctx, conn, state.enforcers)
        query = _feedback_query()
        visible = _visible(feedback.c.team, feedback.c.author_id == ctx.user_id, teams)
        if visible is not None:
            query = query.where(visible)
        if team is not None:
            query = query.where(feedback.c.team == team)
        if run_id is not None:
            query = query.where(feedback.c.run_id == run_id)
        rows = await conn.execute(query.order_by(feedback.c.given_at.desc()).limit(limit))
        return [_info(row) for row in rows]


@router.put("/learning/feedback/{feedback_id}/attribution")
async def correct_attribution(
    feedback_id: str, body: AttributionCorrection, request: Request, ctx: Caller
) -> FeedbackInfo:
    """Set which agents learn from a record (method `human`), audited as
    `learning.attribution_corrected`. The record's author, or `agent.approve.repo` in its
    team; a record the caller cannot read is 404."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        row = (await conn.execute(_feedback_query().where(feedback.c.id == feedback_id))).first()
        if row is None:
            raise not_found("feedback record")
        mine = row.author_id == ctx.user_id
        if not mine:
            teams = await _viewable_teams(ctx, conn, state.enforcers)
            if teams is not None and row.team not in teams:
                raise not_found("feedback record")
            allowed = await authorize(
                ctx, CORRECT, Team(row.team), conn=conn, cache=state.enforcers
            )
            if not allowed.allowed:
                raise ApiProblem(
                    403,
                    "rbac.forbidden",
                    f"only the record's author or a holder of {CORRECT} in team {row.team} "
                    "may correct its attribution",
                )
        attribution = Attribution(
            learners=tuple(Learner(agent=agent, confidence=1.0) for agent in body.agents),
            method="human",
            reason=body.reason,
        )
        await set_attribution(
            conn, ctx, row, attribution, by=ctx.email, clock=state.clock, reason=body.reason
        )
        updated = (await conn.execute(_feedback_query().where(feedback.c.id == feedback_id))).one()
        return _info(updated)


@router.get("/learning/proposals")
async def list_proposals(
    request: Request,
    ctx: Caller,
    team: Annotated[str | None, Query(max_length=64)] = None,
    status: Annotated[str | None, Query(pattern="^(pending|rejected)$")] = None,
) -> list[ProposalInfo]:
    """The proposals the caller may read, newest first. Proposals are created only: nothing
    has been applied because one is listed here."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        teams = await _viewable_teams(ctx, conn, state.enforcers)
        query = select(learning_proposals)
        visible = _visible(
            learning_proposals.c.team, learning_proposals.c.authors.any(ctx.user_id), teams
        )
        if visible is not None:
            query = query.where(visible)
        if team is not None:
            query = query.where(learning_proposals.c.team == team)
        if status is not None:
            query = query.where(learning_proposals.c.status == status)
        rows = await conn.execute(query.order_by(learning_proposals.c.created_at.desc()))
        return [
            ProposalInfo(
                id=row.id,
                team=row.team,
                agent=row.agent,
                scope=row.scope,
                lesson=row.lesson,
                evidence=list(row.evidence),
                status=row.status,
                created_at=row.created_at,
                decided_at=row.decided_at,
            )
            for row in rows
        ]
