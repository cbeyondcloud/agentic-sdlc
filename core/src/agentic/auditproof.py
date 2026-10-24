"""Checking hash chains and verifying audit exports, with the Python standard library only.

This one file is the verifier an auditor runs on an audit export (M2-13): copy it anywhere
and run

    python auditproof.py audit-export.jsonl      (or .csv)

It needs no network, no database and nothing else of ours. Exit 0: the export is intact;
3: it is not (each problem is printed with the record's seq and the class of change);
1: a file could not be read; 64: wrong usage. The framework itself uses the same code for
`agentic audit verify` (M2-26), so the auditor and the CLI agree by construction.

**The chain.** Every record carries `prev` (the hash of the record before it, "" for the
first) and `hash`:

    hash = "sha256:" + hex(sha256(utf8(json(record without "hash" and "signature"),
                                       sort_keys, separators (",", ":"), no ASCII escapes))))

This is the run ledger's formula (Zyroflo `approve.entry_hash`, through
`sdlc_commons.ledger.entry_hash`; a parity test pins the two together). A record whose
contents change no longer matches its `hash`; a record removed, inserted or reordered
breaks the next record's `prev`; and `seq` counts 1, 2, 3, ... per tenant.

**JSONL export** (`agentic-audit-export/1`), one JSON object per line:

    {"kind": "header", "format": ..., "tenant", "team", "from", "to", "exported_at",
     "exported_by", "start": {"seq": N, "hash": H}}    the anchor just before the range
    {"kind": "entry", "record": {...}}                 a record, every field as hashed
    {"kind": "link", "seq", "prev", "hash"}            a record of another team (or of the
                                                       org) inside a team export: its links
                                                       only, so the chain stays continuous
    {"kind": "proof", "entries", "links", "end": {"seq", "hash"}, "head": {"seq", "hash"}}

The rows run without a gap from `start.seq + 1` to `end.seq`; the first links to
`start.hash`. Keep each export's `end` (or the `head`): the next export's `start` must
continue it, and a history rewritten in between cannot. An export with no `proof` line was
cut short.

**CSV export**: the column row (`CSV_COLUMNS`), then the same rows as the JSONL export in
the same order: a `header` row, one row per record (`kind` entry or link), and a `proof`
row. The header and proof rows carry the JSONL header and proof objects (without `kind`)
as canonical JSON in the `after` column, so a CSV is checked for completeness exactly
like JSONL: a CSV without its proof row was cut short, and one with leading rows removed
no longer continues its header's start anchor. In a record row `before`/`after` hold
canonical JSON; an empty nullable cell is null (or an empty string - the hash decides).
A cell that a spreadsheet would run as a formula (it starts with = + - @, a tab, a
carriage return, a full-width = + - @, or a quote) carries one extra leading `'`; strip
exactly one leading `'` to read the value back.
"""

from __future__ import annotations

import csv
import hashlib
import io
import itertools
import json
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

EXPORT_FORMAT = "agentic-audit-export/1"
#: The fields of an audit record, as hashed (spec §14.5).
RECORD_FIELDS = (
    "seq",
    "tenant",
    "team",
    "actor",
    "actor_kind",
    "event",
    "target",
    "before",
    "after",
    "reason",
    "run_id",
    "trace_id",
    "policy_version",
    "at",
    "prev",
    "hash",
)
NULLABLE = ("team", "run_id", "trace_id", "policy_version")
JSON_FIELDS = ("before", "after")
LINK_FIELDS = ("seq", "prev", "hash")
CSV_COLUMNS = ("kind", *RECORD_FIELDS)
#: Leading characters a spreadsheet may read as a formula (OWASP CSV injection), plus the
#: quote that escapes them, so the escape can be undone exactly.
FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r", "＝", "＋", "－", "＠", "'")

EXIT_OK, EXIT_ERROR, EXIT_BROKEN, EXIT_USAGE = 0, 1, 3, 64


def entry_hash(entry: Mapping) -> str:
    """The hash of one chain entry: every field but `hash` and `signature`."""
    body = {key: value for key, value in entry.items() if key not in ("hash", "signature")}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Finding:
    """One problem: where (1-based position in the file or stream, and the entry's seq)
    and the class of change.

    content     the entry's contents changed after it was hashed
    hash        the entry's recorded hash was replaced (its contents still match the next
                entry's link)
    link        the entry does not link to the one before it: an entry was removed,
                inserted or reordered, or an entry was changed and re-hashed
    sequence    the entry's seq is not the next one
    unreadable  the entry is not a JSON object
    anchor      an export's proof does not match its rows
    scope       an export holds a record of another tenant or team
    format      the file is not an export this verifier knows, or was cut short"""

    position: int
    seq: int | None
    field_class: str
    detail: str

    def __str__(self) -> str:
        where = f"seq {self.seq}" if self.seq is not None else f"entry {self.position}"
        return f"{where}: {self.field_class} - {self.detail}"


def _seq_of(entry: object) -> int | None:
    seq = entry.get("seq") if isinstance(entry, Mapping) else None
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else None


class ChainChecker:
    """Checks a chain entry by entry, holding one entry back so that a hash mismatch can
    be classified by the next entry's link. `first_seq` None: the entries carry no seq
    (an approvals chain); `prev` is the hash the first entry must link to. An `opaque`
    entry (an export's link row) is checked for its seq and link only."""

    def __init__(self, first_seq: int | None = 1, prev: str = "") -> None:
        self._expected = first_seq
        self._prev: str | None = prev
        self._also: str | None = None  # a recomputed hash the next entry may link to
        self._pending: tuple[int, object, bool] | None = None
        self._position = 0
        self.findings: list[Finding] = []
        self.last: tuple[int | None, str] = (
            first_seq - 1 if first_seq is not None else None,
            prev,
        )
        self.count = 0

    def feed(self, entry: object, *, opaque: bool = False) -> None:
        self._position += 1
        if self._pending is not None:
            self._settle(*self._pending, following=entry)
        self._pending = (self._position, entry, opaque)

    def finish(self) -> list[Finding]:
        if self._pending is not None:
            self._settle(*self._pending, following=None)
            self._pending = None
        return self.findings

    def _add(self, position: int, seq: int | None, field_class: str, detail: str) -> None:
        self.findings.append(Finding(position, seq, field_class, detail))

    def _settle(self, position: int, entry: object, opaque: bool, *, following: object) -> None:
        self.count += 1
        if not isinstance(entry, Mapping):
            self._add(position, None, "unreadable", "not a JSON object; nothing after it links")
            self._prev, self._also = None, None
            if self._expected is not None:
                self._expected += 1
            return
        seq = _seq_of(entry)
        if self._expected is not None:
            if seq != self._expected:
                self._add(
                    position,
                    seq,
                    "sequence",
                    f"seq {entry.get('seq')!r} where {self._expected} was due - an entry was "
                    "removed, inserted or renumbered",
                )
            self._expected = (seq if seq is not None else self._expected) + 1
        link = entry.get("prev", "")
        if self._prev is not None and link not in (self._prev, self._also):
            self._add(
                position,
                seq,
                "link",
                f"links to {str(link)[:80]!r} but the entry before it hashes to "
                f"{self._prev[:80]!r} - an entry was removed, inserted or reordered, or "
                "changed and re-hashed",
            )
        recorded = entry.get("hash")
        self._also = None
        if not opaque:
            computed = entry_hash(entry)
            if computed != recorded:
                follows = following.get("prev") if isinstance(following, Mapping) else None
                if follows == computed:
                    self._add(
                        position,
                        seq,
                        "hash",
                        "its recorded hash was replaced; its contents still match the next "
                        "entry's link",
                    )
                    self._also = computed
                else:
                    self._add(
                        position,
                        seq,
                        "content",
                        "its contents do not match its recorded hash - it was edited after "
                        "it was written",
                    )
        self._prev = recorded if isinstance(recorded, str) else ""
        self.last = (seq, self._prev)


def check_chain(
    entries: Iterable[object], *, first_seq: int | None = 1, prev: str = ""
) -> list[Finding]:
    """Every problem in `entries` ([] = intact)."""
    checker = ChainChecker(first_seq, prev)
    for entry in entries:
        checker.feed(entry)
    return checker.finish()


# ── writing exports (the control plane uses these, so writer and verifier agree) ──


def jsonl_line(row: Mapping) -> bytes:
    """One JSONL row: UTF-8, LF, no ASCII escapes (the same bytes on every OS)."""
    return json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _anchor(pair: tuple[int, str] | None) -> dict | None:
    return None if pair is None else {"seq": pair[0], "hash": pair[1]}


def header_row(
    *,
    tenant: str,
    team: str | None,
    start: tuple[int, str] | None,
    exported_by: str,
    start_at: str | None = None,
    end_at: str | None = None,
    exported_at: str | None = None,
) -> dict:
    """The first JSONL row: who exported which range, and the anchor before it."""
    return {
        "kind": "header",
        "format": EXPORT_FORMAT,
        "tenant": tenant,
        "team": team,
        "from": start_at,
        "to": end_at,
        "exported_at": exported_at or _now(),
        "exported_by": exported_by,
        "start": _anchor(start),
    }


def entry_row(record: Mapping) -> dict:
    return {"kind": "entry", "record": {name: record[name] for name in RECORD_FIELDS}}


def link_row(record: Mapping) -> dict:
    return {"kind": "link", **{name: record[name] for name in LINK_FIELDS}}


def proof_row(
    *,
    entries: int,
    links: int,
    end: tuple[int, str] | None,
    head: tuple[int, str] | None,
) -> dict:
    """The last JSONL row: the counts, the last row's anchor and the chain's head."""
    return {
        "kind": "proof",
        "entries": entries,
        "links": links,
        "end": _anchor(end),
        "head": _anchor(head),
    }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def csv_cell(value: str) -> str:
    """`value` safe in a spreadsheet: one leading `'` before a formula lead (or a `'`)."""
    return "'" + value if value[:1] in FORMULA_LEADS else value


def csv_uncell(cell: str) -> str:
    """The exact inverse of `csv_cell`."""
    return cell[1:] if cell.startswith("'") else cell


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def csv_row(kind: str, record: Mapping) -> list[str]:
    """One CSV row (`CSV_COLUMNS`) for a record; a `link` row keeps seq, prev and hash."""
    cells = [kind]
    for name in RECORD_FIELDS:
        value = record.get(name)
        if kind == "link" and name not in LINK_FIELDS:
            text = ""
        elif name in JSON_FIELDS:
            text = _canonical(value)
        elif value is None:
            text = ""
        else:
            text = str(value)
        cells.append(csv_cell(text))
    return cells


def csv_meta_row(row: Mapping) -> list[str]:
    """A CSV export's `header` or `proof` row from the JSONL row (`header_row`,
    `proof_row`): its fields but `kind` as canonical JSON in the `after` column; a header
    also shows `tenant` and `team` in theirs (the JSON is what the verifier reads)."""
    kind = row["kind"]
    cells = {"kind": kind, "after": _canonical({k: v for k, v in row.items() if k != "kind"})}
    if kind == "header":
        cells["tenant"] = str(row.get("tenant") or "")
        cells["team"] = str(row.get("team") or "")
    return [csv_cell(cells.get(name, "")) for name in CSV_COLUMNS]


# ── verifying exports ─────────────────────────────────────────────────────────


@dataclass
class ExportReport:
    """What `verify_export` found. `start`/`end`/`head` are (seq, hash) anchors."""

    path: str
    format: str = ""
    entries: int = 0
    links: int = 0
    team: str | None = None
    start: tuple[int, str] | None = None
    end: tuple[int, str] | None = None
    head: tuple[int, str] | None = None
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "format": self.format,
            "intact": self.ok,
            "entries": self.entries,
            "links": self.links,
            "team": self.team,
            "start": _anchor(self.start),
            "end": _anchor(self.end),
            "head": _anchor(self.head),
            "findings": [
                {
                    "position": f.position,
                    "seq": f.seq,
                    "field_class": f.field_class,
                    "detail": f.detail,
                }
                for f in self.findings
            ],
        }


def _pair(value: object) -> tuple[int, str] | None:
    if not isinstance(value, Mapping):
        return None
    seq, digest = value.get("seq"), value.get("hash")
    if isinstance(seq, int) and not isinstance(seq, bool) and isinstance(digest, str):
        return seq, digest
    return None


def verify_export(path: Path) -> ExportReport:
    """Verify one export file (JSONL or CSV, told apart by its first byte)."""
    data = Path(path).read_bytes()
    report = ExportReport(path=str(path))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        report.findings.append(Finding(0, None, "format", "the file is not UTF-8 text"))
        return report
    if text.lstrip("﻿").startswith("{"):
        report.format = "jsonl"
        # split on LF only: str.splitlines() would also split at U+2028, U+0085 and other
        # separators a record's text may hold raw (JSON does not escape them)
        lines = [line.removesuffix("\r") for line in text.lstrip("﻿").split("\n")]
        _verify_jsonl(lines, report)
    else:
        report.format = "csv"
        _verify_csv(text.lstrip("﻿"), report)
    return report


def _rows(lines: Sequence[str]) -> Iterator[tuple[int, object]]:
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            yield number, json.loads(line)
        except ValueError:
            yield number, None


def _verify_jsonl(lines: Sequence[str], report: ExportReport) -> None:
    _verify_rows(_rows(lines), report)


def _verify_rows(rows: Iterator[tuple[int, object]], report: ExportReport) -> None:
    """Check an export's rows (JSONL-shaped: header, entries and links, proof)."""
    first = next(rows, (0, None))[1]
    if not isinstance(first, Mapping) or first.get("kind") != "header":
        report.findings.append(Finding(1, None, "format", "the first row is not a header"))
        return
    if first.get("format") != EXPORT_FORMAT:
        report.findings.append(
            Finding(1, None, "format", f"unknown export format {first.get('format')!r}")
        )
        return
    tenant, team = first.get("tenant"), first.get("team")
    report.team = team
    report.start = _pair(first.get("start"))
    start = report.start or (0, "")
    checker = ChainChecker(start[0] + 1, start[1])
    proof = None
    for number, row in rows:
        kind = row.get("kind") if isinstance(row, Mapping) else None
        if proof is not None:
            report.findings.append(Finding(number, None, "format", "a row after the proof"))
            break
        if kind == "entry":
            record = row.get("record")
            if not isinstance(record, Mapping) or set(record) != set(RECORD_FIELDS):
                checker.feed(None)
                continue
            seq = _seq_of(record)
            if record.get("tenant") != tenant:
                report.findings.append(Finding(number, seq, "scope", "a record of another tenant"))
            if team is not None and record.get("team") != team:
                report.findings.append(
                    Finding(number, seq, "scope", f"a record of team {record.get('team')!r}")
                )
            report.entries += 1
            checker.feed(record)
        elif kind == "link":
            report.links += 1
            if team is None:
                report.findings.append(
                    Finding(number, _seq_of(row), "scope", "an org export carries every record")
                )
            checker.feed({name: row.get(name) for name in LINK_FIELDS}, opaque=True)
        elif kind == "proof":
            proof = row
        else:
            checker.feed(None)
    findings = checker.finish()
    report.findings.extend(findings)
    report.end = checker.last if checker.count else None
    if proof is None:
        report.findings.append(
            Finding(0, None, "format", "the export ends without its proof row - it was cut short")
        )
        return
    report.head = _pair(proof.get("head"))
    claimed_end = _pair(proof.get("end"))
    if (
        proof.get("entries") != report.entries
        or proof.get("links") != report.links
        or claimed_end != report.end
    ):
        report.findings.append(
            Finding(0, None, "anchor", "the proof's counts or end anchor do not match the rows")
        )
    if report.head is not None and report.end is not None and report.head[0] < report.end[0]:
        report.findings.append(Finding(0, None, "anchor", "the head is older than the rows"))


def _record_candidates(cells: Mapping[str, str]) -> list[dict]:
    """The record a CSV row stands for: null for each empty nullable cell, or failing that
    an empty string (the hash tells which)."""
    record: dict = {}
    for name in RECORD_FIELDS:
        value = csv_uncell(cells.get(name, ""))
        if name == "seq":
            record[name] = int(value) if value.lstrip("-").isdigit() else value
        elif name in JSON_FIELDS:
            record[name] = json.loads(value) if value else None
        elif name in NULLABLE and value == "":
            record[name] = None
        else:
            record[name] = value
    empty = [name for name in NULLABLE if record[name] is None]
    candidates = [record]
    for size in range(1, len(empty) + 1):
        for names in itertools.combinations(empty, size):
            candidates.append({**record, **dict.fromkeys(names, "")})
    return candidates


def _verify_csv(text: str, report: ExportReport) -> None:
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header is None or tuple(header) != CSV_COLUMNS:
        report.findings.append(Finding(1, None, "format", "the header row is not CSV_COLUMNS"))
        return
    _verify_rows(_csv_rows(reader), report)


def _csv_rows(reader: Iterable[list[str]]) -> Iterator[tuple[int, object]]:
    """A CSV export's rows in the JSONL shape (None for a row that cannot be read)."""
    for number, row in enumerate(reader, 2):
        if not row:
            continue
        yield number, _csv_as_row(row)


def _csv_as_row(row: Sequence[str]) -> dict | None:
    if len(row) != len(CSV_COLUMNS):
        return None
    cells = dict(zip(CSV_COLUMNS, row, strict=True))
    kind = csv_uncell(cells["kind"])
    try:
        if kind in ("header", "proof"):
            body = json.loads(csv_uncell(cells["after"]))
            return {**body, "kind": kind} if isinstance(body, dict) else None
        candidates = _record_candidates(cells)
    except ValueError:
        return None
    if kind == "link":
        return {"kind": "link", **{name: candidates[0][name] for name in LINK_FIELDS}}
    if kind == "entry":
        match = next((c for c in candidates if entry_hash(c) == c["hash"]), candidates[0])
        return {"kind": "entry", "record": match}
    return None


# ── command line ──────────────────────────────────────────────────────────────


def _shown(pair: tuple[int, str] | None) -> str:
    return "-" if pair is None else f"seq {pair[0]} {pair[1] or '(genesis)'}"


def main(argv: Sequence[str] | None = None) -> int:
    paths = list(sys.argv[1:] if argv is None else argv)
    if not paths or any(p in ("-h", "--help") for p in paths):
        print("usage: python auditproof.py EXPORT [EXPORT ...]  (agentic audit export files)")
        return EXIT_USAGE
    worst = EXIT_OK
    for name in paths:
        try:
            report = verify_export(Path(name))
        except OSError as error:
            print(f"{name}: cannot read ({error.strerror or error})")
            if worst == EXIT_OK:
                worst = EXIT_ERROR
            continue
        scope = f"team {report.team}" if report.team else "whole tenant"
        verdict = "intact" if report.ok else f"BROKEN ({len(report.findings)} problems)"
        print(f"{name}: {verdict} - {report.format}, {scope}, {report.entries} records")
        print(f"  start {_shown(report.start)}; end {_shown(report.end)}")
        if report.head is not None:
            print(f"  chain head when exported: {_shown(report.head)}")
        for finding in report.findings:
            print(f"  {finding}")
        if not report.ok:
            worst = EXIT_BROKEN
    return worst


if __name__ == "__main__":
    sys.exit(main())
