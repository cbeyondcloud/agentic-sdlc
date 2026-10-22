"""One ingest batch, all or nothing (Review Focus 1, ruling R16).

Inside the caller's tenant transaction: the run's row is locked (a new run's row is inserted
first, `ON CONFLICT DO NOTHING`, so two first batches serialise on it); the caller must be
allowed to write the run (the first batch needs `run.trigger` in the run's team and binds
the run to the caller; later batches come from that starter or a holder of
`run.cancel.any`); entries at or below the stored head must equal what is stored
(duplicates, a no-op) or the batch is a fork; the rest must start right after the head
and verify with `sdlc_commons.ledger.verify_entries` from it. Any refusal raises before
anything is written, and the caller's transaction rolls back."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic.orgapi import ChainHead, IngestResult
from agentic_cp.audit import AuditEvent, append
from agentic_cp.auth.tokens import TokenContext
from agentic_cp.errors import ApiProblem
from agentic_cp.rbac.authz import EnforcerCache, Team, require
from agentic_cp.tables import policy_versions, run_events, runs
from agentic_cp.tenancy.context import require_tenant_id
from sdlc_commons.ledger import NAMESPACE, verify_entries

#: Codes of refusals that are audited in the tenant's chain (a gap is a sink catching up).
AUDITED = {"ingest.chain_broken": "ingest.chain_rejected", "ingest.fork": "ingest.fork"}


def head_problem(code: str, detail: str, head: ChainHead) -> ApiProblem:
    return ApiProblem(
        409,
        code,
        detail,
        hint=f"resend the run's events from seq {head.seq + 1}",
        extra={"head": head.model_dump()},
    )


def broken(problems: list[str]) -> ApiProblem:
    return ApiProblem(
        422,
        "ingest.chain_broken",
        f"the batch does not verify ({problems[0]})",
        extra={"problems": problems[:100]},
    )


def _checked(entries: list) -> list[dict]:
    """Each entry is a JSON object with an integer seq, holding nothing Postgres jsonb
    refuses (NaN, Infinity, a NUL character)."""
    problems = []
    for n, entry in enumerate(entries, 1):
        seq = entry.get("seq") if isinstance(entry, Mapping) else None
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            problems.append(f"entry {n}: not an event with a positive integer seq")
            continue
        try:
            text = json.dumps(entry, allow_nan=False)
        except (TypeError, ValueError):
            problems.append(f"event {seq}: holds a value JSON cannot carry (NaN or Infinity)")
            continue
        if "\\u0000" in text:
            problems.append(f"event {seq}: holds a NUL character")
    if problems:
        raise broken(problems)
    return entries


def _identity(entry: Mapping) -> tuple[str, str] | None:
    ext = entry.get("ext")
    block = ext.get(NAMESPACE) if isinstance(ext, Mapping) else None
    if not isinstance(block, Mapping):
        return None
    team, policy = block.get("team"), block.get("policy_version")
    return (team, policy) if isinstance(team, str) and isinstance(policy, str) else None


async def ingest_batch(
    conn: AsyncConnection,
    ctx: TokenContext,
    run_id: str,
    entries: list,
    *,
    cache: EnforcerCache,
    clock: Callable[[], datetime],
) -> IngestResult:
    tenant_id = await require_tenant_id(conn)
    entries = _checked(entries)
    first = entries[0]
    if first["seq"] == 1:
        identity = _identity(first)
        if identity is None:
            raise broken([f"event 1 ({first.get('event')}): carries no run identity"])
        await conn.execute(
            insert(runs)
            .values(
                tenant_id=tenant_id,
                run_id=run_id,
                team=identity[0][:64],
                started_by=ctx.user_id,
                policy_version=identity[1][:80],
                policy_known=False,
            )
            .on_conflict_do_nothing()
        )
    row = (
        await conn.execute(select(runs).where(runs.c.run_id == run_id).with_for_update())
    ).first()
    if row is None:
        raise head_problem(
            "ingest.gap", "the control plane has no events of this run yet", ChainHead()
        )
    head = ChainHead(seq=row.head_seq, hash=row.head_hash)
    new_run = row.head_seq == 0
    if new_run:
        await require(ctx, "run.trigger", Team(row.team), conn=conn, cache=cache)
    elif ctx.user_id != row.started_by:
        await require(ctx, "run.cancel.any", Team(row.team), conn=conn, cache=cache)

    old = [entry for entry in entries if entry["seq"] <= head.seq]
    rest = entries[len(old) :]
    if any(entry["seq"] <= head.seq for entry in rest):
        raise broken(["the batch is not in seq order"])
    if old:
        stored = dict(
            (
                await conn.execute(
                    select(run_events.c.seq, run_events.c.hash).where(
                        run_events.c.run_id == run_id,
                        run_events.c.seq.in_([entry["seq"] for entry in old]),
                    )
                )
            ).all()
        )
        for entry in old:
            if stored.get(entry["seq"]) != entry.get("hash"):
                raise head_problem(
                    "ingest.fork",
                    f"event {entry['seq']} differs from the one already stored - the chain forked",
                    head,
                )
    if not rest:
        return IngestResult(accepted=0, duplicates=len(old), head=head)
    if rest[0]["seq"] != head.seq + 1:
        raise head_problem(
            "ingest.gap",
            f"the batch starts at event {rest[0]['seq']}, the control plane holds up to {head.seq}",
            head,
        )
    if new_run and rest[0].get("event") != "run_opened":
        raise ApiProblem(422, "ingest.not_opened", "a run's first event must be run_opened")
    expected = None if new_run else (row.team, row.policy_version)
    if problems := verify_entries(
        rest, run_id=run_id, after=(head.seq, head.hash), identity=expected
    ):
        raise broken(problems)
    if new_run and _identity(rest[0]) != (row.team, row.policy_version):
        raise broken(["event 1: run identity differs from the run's"])

    await conn.execute(
        run_events.insert().values(
            [
                {
                    "tenant_id": tenant_id,
                    "run_id": run_id,
                    "seq": entry["seq"],
                    "hash": entry["hash"],
                    "prev": entry.get("prev", ""),
                    "event": entry["event"],
                    "step": str(entry.get("step", "")),
                    "at": str(entry.get("at", "")),
                    "entry": entry,
                }
                for entry in rest
            ]
        )
    )
    last = rest[-1]
    values: dict = {"head_seq": last["seq"], "head_hash": last["hash"], "last_event_at": clock()}
    closing = [entry for entry in rest if entry["event"] == "run_closed"]
    if closing:
        values["status"] = str(closing[-1].get("status") or "closed")[:32]
    if new_run:
        values["policy_known"] = await _policy_known(conn, row.policy_version)
    await conn.execute(update(runs).where(runs.c.run_id == run_id).values(**values))
    if new_run and not values["policy_known"]:
        await append(  # ND-7: evidence is kept, the run is flagged
            conn,
            AuditEvent(
                event="ingest.unknown_policy_version",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target=f"run:{run_id}",
                team=row.team,
                run_id=run_id,
                reason="the run names a policy version this tenant never accepted",
                after={"policy_version": row.policy_version},
            ),
            clock=clock,
        )
    return IngestResult(
        accepted=len(rest), duplicates=len(old), head=ChainHead(seq=last["seq"], hash=last["hash"])
    )


async def _policy_known(conn: AsyncConnection, version: str) -> bool:
    found = await conn.execute(
        select(policy_versions.c.version).where(
            policy_versions.c.version == version, policy_versions.c.status == "accepted"
        )
    )
    return found.first() is not None


async def audit_rejection(
    conn: AsyncConnection,
    ctx: TokenContext,
    run_id: str,
    problem: ApiProblem,
    clock: Callable[[], datetime],
) -> None:
    """The refused batch's audit record, written in its own transaction after the
    rollback (nothing of the batch itself is stored)."""
    await append(
        conn,
        AuditEvent(
            event=AUDITED[problem.code],
            actor=ctx.email,
            actor_kind=ctx.actor_kind,
            target=f"run:{run_id}",
            run_id=run_id,
            reason=problem.detail[:300],
            after={
                key: value for key, value in problem.extra.items() if key in ("head", "problems")
            },
        ),
        clock=clock,
    )
