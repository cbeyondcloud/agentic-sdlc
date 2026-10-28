"""The learning loop in the control plane (M3-11..M3-13): one feedback batch, all or nothing,
and what it changes.

A run's feedback journal (`sdlc_commons.feedback.FeedbackJournal`) is one chain per run,
ingested like the run's events and the personal-backup journal: inside the caller's tenant
transaction the run's row is locked; the run must exist (another tenant's run, or one
whose events have not arrived yet, is 404 - the CLI's sink retries later); the caller is
the run's starter or holds `run.cancel.any` in its team (as for the run's own events);
entries at or below the stored head must equal what is stored (replays: `duplicates`) or
the batch is a fork; the rest must start right after the head, link to it, hash to their
contents and carry a valid payload - a captured record of this run whose id is derived from
this run (`feedback_id`), so no run can claim another run's record. A refusal raises before
anything is written.

The stored entries are then folded into `feedback` rows: a captured record becomes a row
(author: the caller), an analysis sets its lesson and - unless a person corrected it - its
attribution, a correction sets the attribution. Each record and each correction is on the
tenant's audit chain (`learning.feedback_captured`, `learning.attribution_corrected`).
Last, the agents the batch touched are clustered again in the run's team, under the run's
policy version's learning settings, and each new proposal is audited (`learning.proposed`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic.learning.clustering import LearningSettings, Lesson, Proposal, propose
from agentic.learning.records import (
    ANALYSED,
    CAPTURED,
    CORRECTED,
    EVENTS,
    MAX_LESSON,
    Attribution,
    FeedbackRecord,
    cap,
    feedback_id,
    record_from,
)
from agentic.orgapi import ChainHead, IngestResult
from agentic_cp.audit import AuditEvent, append
from agentic_cp.auth.tokens import TokenContext
from agentic_cp.errors import not_found
from agentic_cp.ingest.register import version_settings
from agentic_cp.ingest.store import broken, head_problem
from agentic_cp.rbac.authz import EnforcerCache, Team, require
from agentic_cp.tables import feedback, feedback_events, learning_proposals, runs
from agentic_cp.tenancy.context import require_tenant_id
from sdlc_commons.ledger import entry_hash

CHAIN = ("seq", "prev", "hash", "at", "event", "id")
MAX_AT_CHARS = 64


def learning_settings(stored: Mapping | None, team: str) -> LearningSettings:
    """The team's thresholds from a policy version's `run_settings["learning"]` (defaults
    when the version predates them)."""
    block = (stored or {}).get("learning") or {}
    own = (block.get("teams") or {}).get(team) or {}
    default = LearningSettings()
    return LearningSettings(
        threshold=int(own.get("threshold", block.get("threshold", default.threshold))),
        window_days=int(own.get("window_days", block.get("window_days", default.window_days))),
        similarity=float(block.get("similarity", default.similarity)),
    )


def _payload_problems(where: str, entry: Mapping, run_id: str) -> list[str]:
    event = entry.get("event")
    try:
        if event == CAPTURED:
            record = record_from(entry)
            if record.run_id != run_id:
                return [f"{where}: the record names run {record.run_id!r}, not {run_id!r}"]
            if record.id != feedback_id(record.run_id, record.signal, record.source):
                # the id is a primary key: one derived from another run would claim its record
                return [f"{where}: the record id {record.id} is not derived from its run"]
        elif not isinstance(entry.get("feedback"), str):
            return [f"{where}: names no feedback record"]
        if event == CORRECTED and not entry.get("attribution"):
            return [f"{where}: a correction carries an attribution"]
        if entry.get("attribution"):
            Attribution.model_validate(entry["attribution"])
    except (KeyError, TypeError, ValidationError) as error:
        return [f"{where} ({event}): not a valid record ({str(error).splitlines()[0]})"]
    return []


def entry_problems(entry: object, where: str, run_id: str) -> list[str]:
    """What is wrong with one entry's shape and payload (its links are checked later)."""
    if not isinstance(entry, Mapping):
        return [f"{where}: not an entry (a JSON object)"]
    if entry.get("event") not in EVENTS:
        return [f"{where}: event must be one of {', '.join(EVENTS)}"]
    problems = []
    seq = entry.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        problems.append(f"{where}: seq must be a positive integer")
    for name in ("prev", "hash", "id"):
        if not isinstance(entry.get(name), str):
            problems.append(f"{where}: {name} must be text")
    at = entry.get("at")
    if not isinstance(at, str) or not 0 < len(at) <= MAX_AT_CHARS:
        problems.append(f"{where}: at must be a time stamp")
    return problems or _payload_problems(where, entry, run_id)


async def ingest_feedback(
    conn: AsyncConnection,
    ctx: TokenContext,
    run_id: str,
    entries: list,
    *,
    cache: EnforcerCache,
    clock: Callable[[], datetime],
) -> IngestResult:
    problems = [
        p for n, e in enumerate(entries, 1) for p in entry_problems(e, f"entry {n}", run_id)
    ]
    run = (
        await conn.execute(select(runs).where(runs.c.run_id == run_id).with_for_update())
    ).first()
    if run is None:
        raise not_found("run")
    if ctx.user_id != run.started_by:
        await require(ctx, "run.cancel.any", Team(run.team), conn=conn, cache=cache)
    if problems:
        raise broken(problems)
    tenant_id = await require_tenant_id(conn)
    mine = feedback_events.c.run_id == run_id
    last = (
        await conn.execute(
            select(feedback_events.c.seq, feedback_events.c.hash)
            .where(mine)
            .order_by(feedback_events.c.seq.desc())
            .limit(1)
        )
    ).first()
    head = ChainHead(seq=last.seq, hash=last.hash) if last else ChainHead()
    replayed = [e for e in entries if e["seq"] <= head.seq]
    if replayed:
        stored = dict(
            (
                await conn.execute(
                    select(feedback_events.c.seq, feedback_events.c.hash).where(
                        mine, feedback_events.c.seq.in_([e["seq"] for e in replayed])
                    )
                )
            ).all()
        )
        if any(stored.get(e["seq"]) != e["hash"] for e in replayed):
            raise head_problem(
                "ingest.fork", "the batch differs from feedback already stored", head
            )
    fresh = [e for e in entries if e["seq"] > head.seq]
    if not fresh:
        return IngestResult(accepted=0, duplicates=len(replayed), head=head)
    if fresh[0]["seq"] != head.seq + 1:
        raise head_problem(
            "ingest.gap",
            f"the batch starts at {fresh[0]['seq']}, the control plane holds up to {head.seq}",
            head,
        )
    seq, prev, chain_problems = head.seq, head.hash, []
    for entry in fresh:
        seq += 1
        if entry["seq"] != seq:
            chain_problems.append(f"entry {entry['seq']}: expected seq {seq}")
        if entry["prev"] != prev:
            chain_problems.append(f"entry {entry['seq']}: does not link to the entry before it")
        if entry["hash"] != entry_hash(entry):
            chain_problems.append(f"entry {entry['seq']}: its hash does not match its contents")
        prev = entry["hash"]
    if chain_problems:
        raise broken(chain_problems)
    await conn.execute(
        feedback_events.insert().values(
            [
                {
                    "tenant_id": tenant_id,
                    "run_id": run_id,
                    "seq": e["seq"],
                    "hash": e["hash"],
                    "prev": e["prev"],
                    "event": e["event"],
                    "entry": e,
                }
                for e in fresh
            ]
        )
    )
    touched = await _fold(conn, ctx, run, fresh, clock)
    if touched:
        stored_settings = await version_settings(conn, run.policy_version)
        settings = learning_settings(stored_settings, run.team)
        await recluster(conn, ctx, run.team, touched, settings, clock)
    last_entry = fresh[-1]
    return IngestResult(
        accepted=len(fresh),
        duplicates=len(replayed),
        head=ChainHead(seq=last_entry["seq"], hash=last_entry["hash"]),
    )


def _attribution_json(value: object) -> dict | None:
    return Attribution.model_validate(value).model_dump(mode="json") if value else None


def _agents(value: object) -> list[str]:
    return Attribution.model_validate(value).agents if value else []


def _lesson(entry: Mapping, default: str) -> str:
    """The entry's lesson, cut to one lesson's length (the CLI caps it too)."""
    return cap(str(entry.get("lesson") or default), MAX_LESSON)


async def _fold(conn, ctx: TokenContext, run, entries: Iterable[Mapping], clock) -> set[str]:
    """Apply the stored entries to `feedback`; returns the agents whose lessons changed."""
    touched: set[str] = set()
    for entry in entries:
        event, feedback_id = entry["event"], str(entry.get("feedback", ""))
        attribution = _attribution_json(entry.get("attribution"))
        agents = _agents(entry.get("attribution"))
        if event == CAPTURED:
            record = record_from(entry)
            made = await conn.execute(
                insert(feedback)
                .values(
                    tenant_id=run.tenant_id,
                    id=record.id,
                    run_id=run.run_id,
                    team=run.team,
                    repo=record.repo,
                    signal=record.signal,
                    actor=record.actor,
                    given_at=record.given_at,
                    author_id=ctx.user_id,
                    record=record.model_dump(mode="json"),
                    attribution=attribution,
                    agents=agents,
                    lesson=_lesson(entry, ""),
                )
                .on_conflict_do_nothing()
            )
            if made.rowcount:
                touched.update(agents)
                await _audit(conn, ctx, run, "learning.feedback_captured", record, agents, clock)
            continue
        row = (await conn.execute(select(feedback).where(feedback.c.id == feedback_id))).first()
        if row is None or row.run_id != run.run_id:
            continue  # about a record this run never captured: kept as evidence only
        if event == ANALYSED:
            values: dict = {"lesson": _lesson(entry, row.lesson)}
            if attribution is not None and row.corrected_by is None:
                values.update(attribution=attribution, agents=agents)
            await conn.execute(update(feedback).where(feedback.c.id == row.id).values(**values))
            touched.update(row.agents, values.get("agents", []))
        elif event == CORRECTED:
            await set_attribution(
                conn,
                ctx,
                row,
                Attribution.model_validate(entry["attribution"]),
                by=str(entry.get("actor") or ctx.email),
                clock=clock,
            )
            touched.update(row.agents, agents)
    return touched


async def _audit(conn, ctx, run, event: str, record: FeedbackRecord, agents, clock) -> None:
    await append(
        conn,
        AuditEvent(
            event=event,
            actor=ctx.email,
            actor_kind=ctx.actor_kind,
            target=f"feedback:{record.id}",
            team=run.team,
            run_id=run.run_id,
            after={"signal": record.signal, "agents": list(agents), "source": record.source},
        ),
        clock=clock,
    )


async def set_attribution(
    conn: AsyncConnection,
    ctx: TokenContext,
    row,
    attribution: Attribution,
    *,
    by: str,
    clock: Callable[[], datetime],
    reason: str = "",
) -> None:
    """A person's correction of a record's attribution, audited (M3-12)."""
    await conn.execute(
        update(feedback)
        .where(feedback.c.id == row.id)
        .values(
            attribution=attribution.model_dump(mode="json"),
            agents=attribution.agents,
            corrected_by=by,
            corrected_at=clock(),
        )
    )
    await append(
        conn,
        AuditEvent(
            event="learning.attribution_corrected",
            actor=ctx.email,
            actor_kind=ctx.actor_kind,
            target=f"feedback:{row.id}",
            team=row.team,
            run_id=row.run_id,
            before={"agents": list(row.agents)},
            after={"agents": attribution.agents, "by": by},
            reason=(reason or attribution.reason)[:300],
        ),
        clock=clock,
    )


def _proposal(row) -> Proposal:
    return Proposal(
        id=row.id,
        agent=row.agent,
        scope=row.scope,
        lesson=row.lesson,
        evidence=tuple(row.evidence),
        sources=tuple(row.sources),
        actors=tuple(row.actors),
        created_at=row.created_at,
        status=row.status,
        decided_at=row.decided_at,
    )


async def recluster(
    conn: AsyncConnection,
    ctx: TokenContext,
    team: str,
    agents: set[str],
    settings: LearningSettings,
    clock: Callable[[], datetime],
) -> list[Proposal]:
    """Cluster `agents`' lessons in `team` again; store and audit what `propose` returns."""
    now = clock()
    since = now - timedelta(days=settings.window_days)
    rows = (
        await conn.execute(
            select(feedback).where(
                feedback.c.team == team,
                feedback.c.given_at >= since,
                feedback.c.agents.overlap(sorted(agents)),
            )
        )
    ).all()
    authors: dict[str, UUID] = {row.id: row.author_id for row in rows}
    lessons = [
        Lesson(
            feedback_id=row.id,
            agent=agent,
            text=row.lesson or row.record.get("text", ""),
            at=row.given_at,
            source=str(row.record.get("source", row.id)),
            actor=row.actor,
            team=team,
            repo=row.repo,
        )
        for row in rows
        for agent in row.agents
        if agent in agents
    ]
    existing = [
        _proposal(row)
        for row in (
            await conn.execute(
                select(learning_proposals).where(
                    learning_proposals.c.team == team,
                    learning_proposals.c.agent.in_(sorted(agents)),
                )
            )
        ).all()
    ]
    known = {p.id for p in existing}
    changed = propose(lessons, settings, now, existing=existing)
    for proposal in changed:
        values = {
            "evidence": list(proposal.evidence),
            "sources": list(proposal.sources),
            "actors": list(proposal.actors),
            "authors": sorted({authors[f] for f in proposal.evidence if f in authors}, key=str),
        }
        if proposal.id in known:
            await conn.execute(
                update(learning_proposals)
                .where(learning_proposals.c.id == proposal.id)
                .values(**values)
            )
            continue
        await conn.execute(
            learning_proposals.insert().values(
                tenant_id=await require_tenant_id(conn),
                id=proposal.id,
                team=team,
                agent=proposal.agent,
                scope=proposal.scope,
                lesson=proposal.lesson,
                status="pending",
                created_at=now,
                **values,
            )
        )
        await append(
            conn,
            AuditEvent(
                event="learning.proposed",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target=f"proposal:{proposal.id}",
                team=team,
                after={
                    "agent": proposal.agent,
                    "scope": proposal.scope,
                    "evidence": list(proposal.evidence),
                },
            ),
            clock=clock,
        )
    return changed


__all__ = ["entry_problems", "ingest_feedback", "learning_settings", "recluster", "set_attribution"]
