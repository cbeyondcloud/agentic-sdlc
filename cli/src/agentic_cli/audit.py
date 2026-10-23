"""What `agentic audit` checks (M2-26, M2-13): local run ledgers, the control plane's
per-tenant audit chain, and audit export files.

The verdict on a local run is Zyroflo's (`RunLedger.verify`, through `sdlc_commons`);
`agentic.auditproof` then locates each problem line by line and names its class of change
(content, hash, link, sequence, unreadable), the same code an auditor runs on an export.
The control plane's chain is pulled page by page and checked here, so the verdict never
rests on the server's own check."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agentic.auditproof import ChainChecker, ExportReport, Finding, verify_export
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from sdlc_commons.ledger import LedgerError, RunLedger, run_id_refusal, run_ids

#: Records per page when pulling the control plane's chain (the server's maximum).
PAGE = 1000


def _finding(file: str, finding: Finding) -> dict:
    return {
        "file": file,
        "entry": finding.position,
        "seq": finding.seq,
        "field_class": finding.field_class,
        "detail": finding.detail,
    }


@dataclass
class RunReport:
    """One local run: located findings per chain file, and Zyroflo's own problems."""

    run_id: str
    entries: int = 0
    findings: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def intact(self) -> bool:
        return not self.findings and not self.problems

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "intact": self.intact,
            "entries": self.entries,
            "findings": self.findings,
            "problems": self.problems,
        }


def _lines(path: Path) -> list[object]:
    """Each line of a JSONL chain file as parsed (None for a line that is not an object);
    LF splits lines, so a record's own U+2028 never does."""
    entries: list[object] = []
    for line in path.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            value = None
        entries.append(value if isinstance(value, dict) else None)
    return entries


def verify_run(repo: Path, run_id: str) -> RunReport:
    report = RunReport(run_id)
    ledger = RunLedger(repo, run_id)
    try:
        report.problems = ledger.verify()
    except LedgerError as error:
        report.problems = [str(error)]
    events, approvals = ledger.chain_files()
    for path, first_seq in ((events, 1), (approvals, None)):
        if not path.is_file():
            continue
        entries = _lines(path)
        if path == events:
            report.entries = len(entries)
        checker = ChainChecker(first_seq=first_seq)
        for entry in entries:
            checker.feed(entry)
        report.findings += [_finding(path.name, f) for f in checker.finish()]
    return report


def verify_runs(repo: Path, wanted: Sequence[str] = ()) -> list[RunReport]:
    """Every run of the repository (or the `wanted` ones), sorted by run id."""
    known = run_ids(repo)
    for run_id in wanted:
        if why := run_id_refusal(run_id):
            raise CliError(why, ExitCode.USAGE)
        if run_id not in known:
            raise CliError(
                f"no run {run_id} in this repository",
                ExitCode.USAGE,
                hint="`agentic audit verify` with no run id checks every run",
            )
    return [verify_run(repo, run_id) for run_id in (sorted(set(wanted)) or known)]


@dataclass
class ChainReport:
    """The control plane's audit chain as this CLI checked it."""

    tenant: str
    records: int = 0
    head: tuple[int, str] | None = None
    findings: list[dict] = field(default_factory=list)

    @property
    def intact(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict:
        return {
            "tenant": self.tenant,
            "intact": self.intact,
            "records": self.records,
            "head": None if self.head is None else {"seq": self.head[0], "hash": self.head[1]},
            "findings": self.findings,
        }


def verify_control_plane(client, tenant: str) -> ChainReport:
    """Pull the caller's tenant's audit chain (`audit.read.org`) and check it here."""
    report = ChainReport(tenant)
    checker = ChainChecker()
    after = 0
    while True:
        page = client.audit_records(after_seq=after, limit=PAGE)
        for record in page:
            checker.feed(record.model_dump(mode="json"))
        report.records += len(page)
        if len(page) < PAGE:
            break
        after = page[-1].seq
    report.findings = [_finding("audit chain", f) for f in checker.finish()]
    if report.records:
        seq, digest = checker.last
        report.head = (seq or 0, digest)
    return report


def verify_exports(paths: Sequence[Path]) -> list[ExportReport]:
    reports = []
    for path in paths:
        try:
            reports.append(verify_export(path))
        except OSError as error:
            raise CliError(f"cannot read {path} ({error.strerror or error})") from None
    return reports
