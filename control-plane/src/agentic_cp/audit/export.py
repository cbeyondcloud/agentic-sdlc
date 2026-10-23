"""Exporting a range of the tenant's audit chain with its chain proof (M2-13).

The format is `agentic.auditproof`'s (writer and verifier are one module), so an auditor
checks an export with that one stdlib file and no access to this database. The range is
every record whose `at` falls in [from, to), widened to whole seq order: the rows run
without a gap from the first such record to the last, and the anchor before them is the
first record's `prev`. In a team export, records of other teams (and of the org) inside the
range travel as `link` rows - seq, prev and hash only - so the chain stays continuous and
nothing of theirs is disclosed.

The body is produced batch by batch (keyset pages of `CHUNK_ROWS` records, each read in its
own short tenant transaction), so an export of any size needs memory for one batch only
and holds no transaction open while the client reads."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentic.auditproof import (
    CSV_COLUMNS,
    csv_row,
    entry_row,
    header_row,
    jsonl_line,
    link_row,
    proof_row,
)
from agentic_cp.audit.chain import format_at, record_from_row
from agentic_cp.tables import audit_log
from agentic_cp.tenancy.context import tenant_tx

FORMATS = ("jsonl", "csv")
MEDIA_TYPES = {"jsonl": "application/x-ndjson", "csv": "text/csv; charset=utf-8"}
#: Records per batch read and per body chunk sent.
CHUNK_ROWS = 500
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_bound(value: str | None, *, end: bool) -> str | None:
    """A range bound as the exact `at` text it compares with (UTC, milliseconds, `Z`).

    An ISO date is midnight UTC; as `to` (`end`) it means the whole day, so the bound is
    the next midnight. An ISO date-time needs its zone (`Z` or `+hh:mm`): a time without
    one would mean a different moment on every server. ValueError otherwise."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    if _DATE.fullmatch(text):
        day = date.fromisoformat(text)
        moment = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return format_at(moment + timedelta(days=1) if end else moment)
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"{text!r} has no time zone (add Z or +hh:mm)")
    return format_at(moment)


@dataclass(frozen=True)
class ExportRange:
    """The seqs an export covers (None, None: no record in the range), the anchor just
    before them and the chain's head when the export started."""

    first_seq: int | None
    last_seq: int | None
    start: tuple[int, str] | None
    head: tuple[int, str] | None


async def export_range(
    conn: AsyncConnection, start_at: str | None, end_at: str | None
) -> ExportRange:
    """The range of the transaction's tenant's chain whose `at` is in [start_at, end_at).
    `at` is fixed-width ISO text, compared byte-wise (collation "C"), never by locale."""
    head_row = (
        await conn.execute(
            select(audit_log.c.seq, audit_log.c.hash).order_by(audit_log.c.seq.desc()).limit(1)
        )
    ).first()
    head = (head_row.seq, head_row.hash) if head_row else None
    at = audit_log.c.at.collate("C")
    query = select(func.min(audit_log.c.seq), func.max(audit_log.c.seq))
    if start_at is not None:
        query = query.where(at >= start_at)
    if end_at is not None:
        query = query.where(at < end_at)
    first, last = (await conn.execute(query)).one()
    if first is None:
        return ExportRange(None, None, head or (0, ""), head)
    prev = (
        await conn.execute(select(audit_log.c.prev).where(audit_log.c.seq == first))
    ).scalar_one()
    return ExportRange(first, last, (first - 1, prev), head)


def _csv_line(cells: Sequence[str]) -> bytes:
    out = io.StringIO()
    csv.writer(out).writerow(cells)
    return out.getvalue().encode("utf-8")


async def _batch(engine: AsyncEngine, tenant_id: UUID, after: int, last: int) -> list[dict]:
    async with tenant_tx(engine, tenant_id) as conn:
        rows = await conn.execute(
            select(audit_log)
            .where(audit_log.c.seq > after, audit_log.c.seq <= last)
            .order_by(audit_log.c.seq)
            .limit(CHUNK_ROWS)
        )
        return [asdict(record_from_row(row)) for row in rows]


async def stream_export(
    engine: AsyncEngine,
    tenant_id: UUID,
    span: ExportRange,
    *,
    team: str | None,
    fmt: str,
    header: Mapping[str, object],
) -> AsyncIterator[bytes]:
    """The export's bytes, chunk by chunk: header (JSONL), records, proof (JSONL)."""
    if fmt == "jsonl":
        values = {"tenant": str(tenant_id), **header}
        yield jsonl_line(header_row(team=team, start=span.start, **values))
    else:
        yield _csv_line(CSV_COLUMNS)
    entries = links = 0
    end: tuple[int, str] | None = None
    if span.first_seq is not None and span.last_seq is not None:
        cursor = span.first_seq - 1
        while cursor < span.last_seq:
            records = await _batch(engine, tenant_id, cursor, span.last_seq)
            if not records:
                break  # records cannot vanish from an insert-only chain; stop rather than spin
            chunk = []
            for record in records:
                own = team is None or record["team"] == team
                if fmt == "jsonl":
                    chunk.append(jsonl_line(entry_row(record) if own else link_row(record)))
                else:
                    chunk.append(_csv_line(csv_row("entry" if own else "link", record)))
                entries, links = entries + own, links + (not own)
                end = (record["seq"], record["hash"])
            cursor = records[-1]["seq"]
            yield b"".join(chunk)
    if fmt == "jsonl":
        yield jsonl_line(proof_row(entries=entries, links=links, end=end, head=span.head))
