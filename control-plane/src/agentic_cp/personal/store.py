"""One personal-backup batch, all or nothing (M2-20), with run ingest's semantics.

Each (user, device) is one chain: the laptop's journal of `personal.saved` (with the file's
content), `personal.removed` and `personal.held` (the publish scan kept the file on the
laptop; counts only) events, chained with Zyroflo's entry hash. Inside the caller's tenant
transaction the chain is locked (a transaction advisory lock on tenant, user and device);
every entry is checked first (its fields, its path - `backup_path_problem`, the rule
`agentic skills restore` writes by - and a saved file's sha256 against its content);
entries at or below the stored head must equal what is stored (replays: `duplicates`) or
the batch is a fork; the rest must start right after the head and link to it. A refusal
raises before anything is written. Each stored event is appended to the tenant's audit
chain with its path and sha256, never its content."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic.orgapi import ChainHead, IngestResult
from agentic.skills.personal import backup_path_problem
from agentic_cp.audit import AuditEvent, append
from agentic_cp.ingest.store import broken, head_problem
from agentic_cp.tables import personal_events
from agentic_cp.tenancy.context import require_tenant_id
from sdlc_commons.ledger import entry_hash

EVENTS = ("personal.saved", "personal.removed", "personal.held")
FIELDS = frozenset({"seq", "prev", "hash", "at", "event", "kind", "path", "sha256"})
OPTIONAL = {"personal.saved": {"content"}, "personal.removed": set(), "personal.held": {"findings"}}
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
#: The largest file a backup carries (UTF-8 bytes); the CLI skips bigger ones.
MAX_CONTENT_BYTES = 256 * 1024
MAX_AT_CHARS = 64


def entry_problems(entry: object, where: str) -> list[str]:
    """What is wrong with one entry's shape (its chain links are checked later)."""
    if not isinstance(entry, Mapping):
        return [f"{where}: not an event (a JSON object)"]
    event = entry.get("event")
    if event not in EVENTS:
        return [f"{where}: event must be one of {', '.join(EVENTS)}"]
    problems = []
    allowed = FIELDS | OPTIONAL[event]
    if extra := sorted(set(entry) - allowed):
        problems.append(f"{where} ({event}): fields {extra} are not declared")
    if missing := sorted(FIELDS - set(entry)):
        problems.append(f"{where} ({event}): missing fields {missing}")
    seq = entry.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        problems.append(f"{where}: seq must be a positive integer")
    for name in ("prev", "hash"):
        if not isinstance(entry.get(name), str):
            problems.append(f"{where}: {name} must be text")
    at = entry.get("at")
    if not isinstance(at, str) or not 0 < len(at) <= MAX_AT_CHARS:
        problems.append(f"{where}: at must be a time stamp")
    if why := backup_path_problem(entry.get("kind"), entry.get("path")):
        problems.append(f"{where}: {why}")
    digest = entry.get("sha256")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        problems.append(f"{where}: sha256 must be sha256:<64 hex>")
    content = entry.get("content")
    if event == "personal.saved":
        problems += _content_problems(where, content, digest)
    if "findings" in entry and not _counts(entry["findings"]):
        problems.append(f"{where}: findings must map a detector name to a count")
    return problems


def _content_problems(where: str, content: object, digest: object) -> list[str]:
    if not isinstance(content, str):
        return [f"{where}: a saved file carries its content as text"]
    data = content.encode("utf-8", errors="surrogatepass")
    if "\x00" in content:
        return [f"{where}: the content holds a NUL character"]
    if len(data) > MAX_CONTENT_BYTES:
        return [f"{where}: the content is over {MAX_CONTENT_BYTES} bytes"]
    if digest != "sha256:" + hashlib.sha256(data).hexdigest():
        return [f"{where}: sha256 is not the content's"]
    return []


def _counts(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str)
        and 0 < len(key) <= 64
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 0
        for key, count in value.items()
    )


async def ingest_personal(
    conn: AsyncConnection,
    *,
    user_id: UUID,
    actor: str,
    actor_kind: str,
    device: str,
    entries: list,
    clock: Callable[[], datetime],
) -> IngestResult:
    problems = [p for n, e in enumerate(entries, 1) for p in entry_problems(e, f"entry {n}")]
    if problems:
        raise broken(problems)
    tenant_id = await require_tenant_id(conn)
    key = func.hashtextextended(f"agentic.personal:{tenant_id}:{user_id}:{device}", 0)
    await conn.execute(select(func.pg_advisory_xact_lock(key)))
    mine = (personal_events.c.user_id == user_id) & (personal_events.c.device == device)
    last = (
        await conn.execute(
            select(personal_events.c.seq, personal_events.c.hash)
            .where(mine)
            .order_by(personal_events.c.seq.desc())
            .limit(1)
        )
    ).first()
    head = ChainHead(seq=last.seq, hash=last.hash) if last else ChainHead()
    replayed = [e for e in entries if e["seq"] <= head.seq]
    if replayed:
        stored = dict(
            (
                await conn.execute(
                    select(personal_events.c.seq, personal_events.c.hash).where(
                        mine, personal_events.c.seq.in_([e["seq"] for e in replayed])
                    )
                )
            ).all()
        )
        if any(stored.get(e["seq"]) != e["hash"] for e in replayed):
            raise head_problem("ingest.fork", "the batch differs from events already stored", head)
    fresh = [e for e in entries if e["seq"] > head.seq]
    if not fresh:
        return IngestResult(accepted=0, duplicates=len(replayed), head=head)
    if fresh[0]["seq"] != head.seq + 1:
        raise head_problem(
            "ingest.gap", f"the batch starts at {fresh[0]['seq']}, after {head.seq}", head
        )
    seq, prev, chain_problems = head.seq, head.hash, []
    for entry in fresh:
        seq += 1
        if entry["seq"] != seq:
            chain_problems.append(f"event {entry['seq']}: expected seq {seq}")
        if entry["prev"] != prev:
            chain_problems.append(f"event {entry['seq']}: does not link to the event before it")
        if entry["hash"] != entry_hash(entry):
            chain_problems.append(f"event {entry['seq']}: its hash does not match its contents")
        prev = entry["hash"]
    if chain_problems:
        raise broken(chain_problems)
    now = clock()
    for entry in fresh:
        await conn.execute(
            personal_events.insert().values(
                tenant_id=tenant_id,
                user_id=user_id,
                device=device,
                seq=entry["seq"],
                hash=entry["hash"],
                prev=entry["prev"],
                event=entry["event"],
                kind=entry["kind"],
                path=entry["path"],
                sha256=entry["sha256"],
                content=entry.get("content"),
                entry=json.loads(json.dumps(entry)),
                received_at=now,
            )
        )
        after = {"kind": entry["kind"], "sha256": entry["sha256"], "device": device}
        if "findings" in entry:
            after["findings"] = dict(entry["findings"])
        await append(
            conn,
            AuditEvent(
                event=entry["event"],
                actor=actor,
                actor_kind=actor_kind,
                target=f"personal:{entry['path']}",
                after={**after, "seq": entry["seq"]},
            ),
            clock=clock,
        )
    tail = fresh[-1]
    return IngestResult(
        accepted=len(fresh),
        duplicates=len(replayed),
        head=ChainHead(seq=tail["seq"], hash=tail["hash"]),
    )


async def latest_files(conn: AsyncConnection, user_id: UUID) -> list[dict]:
    """The newest saved-or-removed copy of each of the user's paths (by when the control
    plane received it, across devices), each marked `held` when a newer version of the
    file was held back on a laptop."""
    newest = personal_events.c.received_at.desc(), personal_events.c.seq.desc()
    stored = await conn.execute(
        select(personal_events)
        .where(personal_events.c.user_id == user_id, personal_events.c.event != "personal.held")
        .distinct(personal_events.c.path)
        .order_by(personal_events.c.path, *newest)
    )
    held = await conn.execute(
        select(personal_events.c.path, personal_events.c.received_at, personal_events.c.seq)
        .where(personal_events.c.user_id == user_id, personal_events.c.event == "personal.held")
        .distinct(personal_events.c.path)
        .order_by(personal_events.c.path, *newest)
    )
    held_at = {row.path: (row.received_at, row.seq) for row in held}
    files = []
    for row in stored:
        newer_held = row.path in held_at and held_at.pop(row.path) > (row.received_at, row.seq)
        files.append(
            {
                "kind": row.kind,
                "path": row.path,
                "sha256": row.sha256,
                "content": row.content if row.event == "personal.saved" else None,
                "deleted": row.event == "personal.removed",
                "held": newer_held,
                "device": row.device,
                "seq": row.seq,
                "at": str(row.entry.get("at", "")),
            }
        )
    return files
