"""The learning digest (M3-15, spec §24 "Approve"): the people who may decide a team's pending
learning proposals are mailed a list of them, through the notifications outbox (M2-07).

It is queued in the transaction that creates a proposal, at most once per person, team and
day (the outbox's dedupe key), so a busy day is one mail. Who gets it, per proposal (D30):

  * **personal** - the person whose corrections it holds (its one author);
  * **repo** - the repository's CODEOWNERS maintainers and the team's holders of
    `agent.approve.repo`;
  * **team** - the team's holders of `agent.publish.team`.

Each mail lists only the proposals its recipient may decide, at most `MAX_LISTED`, with
how to see and decide them (`agentic learn show <id>`). It carries no tracking and no link
but the command to run."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic.learning.approval import APPROVE_PERMISSION
from agentic_cp.alerts.sinks import MAX_RECIPIENTS, active_person, people_holding
from agentic_cp.learning.targets import target_of
from agentic_cp.notify.outbox import SYSTEM, enqueue
from agentic_cp.notify.templates import render
from agentic_cp.tables import learning_proposals, repo_maintainers, users
from agentic_cp.tenancy.tenants import current_tenant

KIND = "learning.digest"
MAX_LISTED = 20


async def _maintainers(conn: AsyncConnection, repo_key: str) -> list[str]:
    found = await conn.execute(
        select(users.c.email)
        .select_from(repo_maintainers)
        .join(
            users,
            and_(
                users.c.tenant_id == repo_maintainers.c.tenant_id,
                users.c.id == repo_maintainers.c.user_id,
            ),
        )
        .where(
            repo_maintainers.c.repo_key == repo_key,
            users.c.status == "active",
            users.c.kind == "human",
        )
        .order_by(users.c.email)
        .limit(MAX_RECIPIENTS)
    )
    return list(found.scalars())


async def deciders(conn: AsyncConnection, row) -> list[str]:
    """The people who may decide one stored proposal."""
    target = target_of(row)
    if target == "personal":
        person = await active_person(conn, row.authors[0]) if row.authors else None
        return [person] if person else []
    holders = await people_holding(conn, row.team, permission=APPROVE_PERMISSION[target])
    if target == "repo":
        holders = [*await _maintainers(conn, row.scope.removeprefix("repo:")), *holders]
    return list(dict.fromkeys(holders))[:MAX_RECIPIENTS]


async def queue_digest(conn: AsyncConnection, team: str, *, clock: Callable[[], datetime]) -> int:
    """Queue today's digest for each person who may decide one of `team`'s pending
    proposals; returns how many mails were queued (0 for those already queued today)."""
    pending = (
        await conn.execute(
            select(learning_proposals)
            .where(learning_proposals.c.team == team, learning_proposals.c.status == "pending")
            .order_by(learning_proposals.c.created_at.desc())
        )
    ).all()
    by_person: dict[str, list] = {}
    for row in pending:
        for email in await deciders(conn, row):
            by_person.setdefault(email, []).append(row)
    if not by_person:
        return 0
    tenant = await current_tenant(conn)
    day = clock().date().isoformat()
    queued = 0
    for email, rows in sorted(by_person.items()):
        lines = "\n".join(
            f"- {row.id} ({row.agent}, {target_of(row)}): {row.lesson}" for row in rows[:MAX_LISTED]
        )
        more = len(rows) - MAX_LISTED
        if more > 0:
            lines += f"\n- ... and {more} more (agentic learn list)"
        rendered = render(
            KIND,
            {"team": team, "count": str(len(rows)), "proposals": lines},
            tenant_name=tenant.name,
        )
        made = await enqueue(
            conn,
            kind=KIND,
            recipient=email,
            rendered=rendered,
            dedupe_key=f"learning-digest:{team}:{email}:{day}",
            actor=SYSTEM,
            clock=clock,
            team=team,
        )
        queued += made is not None
    return queued


__all__ = ["KIND", "deciders", "queue_digest"]
